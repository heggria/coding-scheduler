# cs — coding scheduler CLI

纯 CLI，无守护进程。兼容 `/usr/bin/python3` 3.9。设计见 `DESIGN.md`。

```bash
export PATH="$PWD/bin:$PATH"
cs doctor --cwd /tmp --pool codex
cs submit --project mm --area backend --tier L --cwd /tmp --prompt examples/prompt-l.md \
  --files src/task.py --lock-shared src/core.py,src/credits.py --tick --json
cs status --json
```

装到 Pro：拷到 `~/code/coding-scheduler`，把 `bin` 加进 PATH。
`--files` 仅锁同 worktree；`--lock-shared` 在同仓库跨 worktree 互斥。
目录锁会警告但仍接受。旧任务惰性解析；原命令行、默认模型和默认状态路径保留。

## 只读派发建议

```bash
cs advise --cwd "$PWD" --prompt-text '将 README.md 中的一处拼写改正' --files README.md
cs advise --cwd "$PWD" --area research --read-only --prompt-text '<调研 Brief>' --json
```

`advise` 共用 [cs-dispatch DoR](skills/cs-dispatch/SKILL.md) 决策规则，纯启发式，
不调用模型，不初始化状态、不加锁、不入队、不 tick。默认打印可复制的文本，`--json` 为单行 JSON；
包含 `recommendation`（inline|plan|submit）、`reasons[]`、`missing[]`、`brief_draft`、
`suggested_command`、`capabilities` 和 `required_capabilities`。小改/当前会话工具闭环倾向 inline，方案未定或 DoR 缺项先 plan，
多文件或明确可后台且 DoR 齐才建议 submit。缺项提供补齐建议及模板，命令为 null，
不会伪装 ready；建议本身不拒绝后续人工处理，也不更改 submit 的原有门禁。

默认 `--area backend`。支持标准 Brief 的 Goal / Current / Desired / Out of scope /
Acceptance Criteria / Verify / Files / Decisions（或 Already tried）/ Unknowns / MR 标题及
常见中文别名；AC 分行至少两条，Unknowns 没有就写「无」。research/review 用 Scope +
Deliverables 替换 Current/Desired，不要求 MR 字段，Verify 可为只读证据核对命令。
仅 research + `--read-only`（或 Brief 中 `Read-only: true`）可空 files。
files 可从 Brief 读取或通过 `--files a.py,b.py` 覆盖；目录/通配符会要求补成具体文件。

就绪命令使用 pin=codex、tier=M、project=cwd 目录名和由请求/范围生成的稳定幂等键，
可用 `--tier`、`--project`、`--idempotency-key` 覆盖。仅供复制的命令显式 `--no-tick`，
不带 `--force`；预检不执行 Verify、不证明 AC、也不探测队列锁或 worker 工具是否可用。

能力预检使用 `ok|unavailable|unknown` 和 `detail`，**unknown 不等于 available**。
默认没有能力证据，因此即使 Brief 齐全，多文件任务也先 plan。可显式复用 doctor 快照：

```bash
cs doctor --cwd "$PWD" --pool codex --json > /tmp/cs-doctor.json
cs advise --cwd "$PWD" --prompt-text '<完整 Brief>' --doctor-report /tmp/cs-doctor.json --json
```

doctor 保留原检查、退出码和探测行为，JSON 增加 `cwd`、`pool`、`capabilities`。
advise 只读取报告，不运行 doctor、不联网、不启动子进程；报告必须匹配 cwd 和建议池 codex。
快照不自动刷新，环境变化后应重新生成；它是建议证据，不是执行时保证。

能力项包括 `cli_pool_ready`（所选池的 CLI）、`cwd_git_writable`（复用 cwd/Git 元数据检查）、
`resume_supported`（调度器池协议支持）、`network_dns`（github.com DNS）、
`network_access`、`browser_or_mcp`、`auth`。后三项 doctor 未实际验证，默认 unknown；
DNS 成功不能证明网络连通或登录有效，resume 协议支持也不能证明原单/session 可续接。
未选池、未检查目录或缺失结果同样保持 unknown。

CLI/cwd 是派发基础需求；Brief 中提到 browser/MCP/Figma、auth/登录、网络或 resume 时，
规则会增加对应需求，输出缺失能力和原因；browser/MCP 未确认倾向 inline，其他未确认倾向 plan，
不生成 submit 命令。需求识别是文本启发式，请核对 `required_capabilities`。
有独立证据时，可在同上下文报告的 `capabilities` 中填写 `{ "status": "ok", "detail": "实际验证证据" }`，
或调用 `advise(..., capabilities=...)` 注入已验证结果；随后重新评估即可，不按 area 或工具标签永久拒派。
这些建议不改变默认池、队列或 submit 的既有门禁，也不实现浏览器自动化。

## Bot 与低 token 用法

所有命令可在命令前或后加 `--json`，单个 JSON 对象带 `schema_version: "1"`；
`tick`、`wait` 默认就输出 JSON。JSON 模式错误也返回同一版本字段和非零退出码。

```bash
cs submit --project cs --area research --read-only --tier M --cwd "$PWD" \
  --prompt-text '分析排队瓶颈，给出结论' --idempotency-key issue-123 --model gpt-6.1-sol --tick --json
printf '%s\n' '目标与验收条件' | cs submit --project cs --area test --tier L \
  --cwd "$PWD" --prompt - --files tests_cs/test_scheduler.py --tick --json
cs wait job-a job-b --tick --timeout 600 --lines 10
cs status job-a --json --lines 20
cs status --no-reclaim --json
cs board --json
```

- `--prompt 文件`、`--prompt -`（stdin）、`--prompt-text 文本` 三选一。
- 同一非空 `--idempotency-key` 重试返回 queue/running/done 中已有任务，不覆盖 prompt，
  不重复启动；key 在状态根目录内全局唯一。显式 `--id` 重复则报错，自动 ID 带随机尾缀。
- `--files` 必须逐一列出输出/可写文件的准确路径（包括要写的评审文档），不要填目录。
  仅同时指定 `--area research --read-only` 时允许为空；review/backend 等即使
  指定 `--read-only` 也必须列出文件。非空目录锁仍只警告，不拒绝。
- `--scope-policy strict|companion|flex` 默认 **companion**，详见下节。范围偏窄仅警告，
  stderr 打印提示，`submit --json` 顶层 `warnings` 返回同一列表。
  通用测试/lock/前端构建提示仅在 strict 下出现；Git 中已知候选文件及其冲突在所有策略下提示。
  所有策略在 `--lock-shared` 超过 3 个路径时提示同仓库兄弟任务的串行化影响。
- `--model` 透传给所选 codex/claude/opencode（OpenCode 要求 `opencode/` 或 `opencode-go/` 前缀），省略沿用默认模型；`research` 和显式 `--read-only`
  的单可交付分析结论；这不豁免上述 `--files` 要求。结论提到“未提交/未推送/只读”等不再误判 blocked。
  所有任务统一使用下述退出证据与结构化状态规则，不扫描自然语言未交付短语。
- submit 默认只排队，并立即返回全部已知阻塞原因和 `awaiting_tick`；`--tick` 才尝试启动。
  `submit --tick --json` 只返回本单的状态、原因和摘要，不包含其他单或全局 schedule。
- 每次 tick 更新 `last_checked`（ISO）、全部 `queue_reasons`，board 的“原因”列显示短句。
  原因包括锁冲突（谁/文件/锁类型）、池满、spill 分数不足、额度未知/耗尽、启动错误。
- wait 等全部任务进入终态（done/failed/blocked/cancelled/unknown），默认只回收；
  可选 `--tick` 在等待每轮运行完整调度，推进排队单（也可能启动其他符合条件的单）。
  wait 成功退出 0 表示等待完成，任务是否成功看各单状态；超时退出 124，保留当前状态。
- wait / 单单 status 返回状态、退出码、耗时秒、产物 log/conclusion/worktree 路径、
  conclusion 前 N 行（默认 20）及从完整 conclusion 提取的 GitHub PR 链接。
  Git 任务 start 时记 `base_sha`，摘要在 cwd 用 `git diff --stat <base_sha> -- .` 及文件列表，
  运行期间包括起点后的已提交和未提交跟踪文件改动；回收后 `changed_files` 固定为结束时
  的快照，并加入未忽略的未跟踪文件。`diff_stat` 仍为读取时的 Git 跟踪文件统计。
  非 Git、旧单缺少 base_sha 或 Git 查询失败时省略 diff 字段。
- status 默认回收任务并重写 board；`--no-reclaim`（别名 `--read-only`）只读取当前记录，
  不回收、不初始化目录、不创建锁文件、不重写 board。并发 tick 时读取结果可能跨越状态转换。
- worker 消失且无有效 exit 文件、或 exit=124 超时记 `unknown`，不当成功/失败；
  wait 自身超时不改任务状态。exit=0 但缺少/空 conclusion 记 unknown（missing_conclusion）。
- exit=0 且结论非空时，仅独立行 `STATUS: BLOCKED` 记 blocked；逐行去除首尾空白后
  区分大小写精确匹配，不接受 Markdown 包装、尾随原因、正文提及或 FAIL。
  其余记 done，适用于编码、research 和 read-only 单；成功结论应写 `STATUS: DONE`。
  自然语言“未合并/只读/未提交”等不参与判定；其他非零退出（除 124）记 failed。
- 交付例外：exit=0 的 BLOCKED 结论若含 PR URL、已创建/draft 的 PR #编号，或提交/推送/HEAD
  附近的完整 40 位 SHA，记 done，并保留 `raw_status=blocked` 和 `delivered_but_blocked` warning。
  仅在“未改代码/未提交/未开 PR”等否定说明后报告 HEAD，不算交付。
- doctor 的非 Git、DNS、信任提示及与所选池无关的 CLI 检查失败为 WARN；
  `--pool codex|cc|opencode` 指定的 CLI 缺失、cwd 无效、Git 元数据不可写仍为 FAIL。
  `claude_trusted_dir_hint` 仅检查 `.claude.json` 中 cwd 的准确项目路径及
  `hasTrustDialogAccepted: true`；普通目录、worktree 未确认信任时为 WARN，
  不阻止启动，也不代表实际信任失败（worker 已使用 skip-permissions）。

### 接手卡

`cs status id` 默认显示一屏接手卡；`cs wait id --text` 显示同一卡片。
wait 保留默认 JSON 输出，单任务 status / wait 的 JSON 摘要新增 `handoff`，
`schema_version: "1"` 及原有字段保持兼容；全局 status 保留池与队列列表。
`--lines` 仍控制旧 conclusion 预览行数，卡片从完整结论提取。

卡片包含短摘要、artifacts 路径、`verification[]`、`risks`、仅供复制的 `next_steps`，
以及独立的 `scheduler_status` / `worker_exit`。`acceptance.status` 固定为 `unproven`
（验收未证明）：即使验证项通过，也没有自动证明全部 AC；done 只表示调度成功。
`attribution` 与 `changed.attribution` 均保守标为 `shared_cwd_or_unknown`，
changed 文件快照和 diff_stat 不代表本单独占改动。

验证项的 status 为 `pass|fail|not_run|unknown`，含 command、exit、log（缺失为 null）与
source 来源。确定性抽取支持 job 的 `verification` 列表，以及 conclusion 中如下独立行：

```text
VERIFICATION: {"command":"python3 -m pytest tests_cs/test_handoff.py -q","status":"pass","exit":0,"log":"/tmp/tests.log"}
```

同一格式也可写在 worker log 中（只读末尾 64 KiB）。明确的每项 exit 优先于 status；
不会用 worker exit 推断验证结果。旧结论支持验证/测试段落中单行反引号命令及明确结果，
如 ``验证: `pytest` passed``；多命令共用结果、代码块、缺结果或旧字段缺失时保持 unknown。
普通日志中的 passed、计划执行的建议和 `STATUS: DONE` 不作为通过证据。
JSON 保留全部提取项，人读卡片最多显示 4 项，其余指向 JSON 和原始产物供审阅。

### 终态本地通知

通知默认关闭；仅本地终端/文件 inbox，不接飞书、不选择收件人，也不自动 resume。

```bash
cs tick                         # 或 cs status / cs wait，先发现 worker 终态
cs notify drain --channel terminal
cs notify drain --channel inbox --json
export CS_NOTIFY_CHANNEL=inbox  # 显式默认渠道：off | terminal | inbox
cs notify drain
```

无 daemon：只有 tick/status/wait 等回收调用才会发现新终态。`notify drain` 只消费已记录的
终态事件，不启动任务、不回收 worker；需要主动调用，可在现有定时 tick 后串行运行。
blocked/unknown 优先，随后 failed、cancelled、done；每条包含 job ID、status、
接手卡查看命令、内联短摘要、结论路径及下一步。done 不代表 AC 通过。

复用 `{CS_HOME}/logs/events.jsonl` 及轮转备份，以 job ID + status + finished_at 去重；
同一状态的重复事件不再投递，重新分类到另一状态可投递新通知。首次启用会消费所有仍保留的
终态事件，不补发已轮转删除的历史。每条成功单独通知，暂不合并成功消息。
投递记录在 `notify/deliveries.json`，扫描游标在 `notify/cursor.json`，不修改 job 终态。
为兼容日志轮转、重新分类和失败重试，每次重扫保留日志，游标只记录扫描位置，不作为投递确认。
失败返回非零，下次 drain 重试；若事件在成功投递前已被日志轮转清除，需人工核对原 job。

inbox 为 `notify/inbox/<稳定事件key>.json`（含短卡和下一步），同一事件重复写入也只保留一份。
terminal 在输出成功后记录投递；进程在输出和确认之间崩溃，重试可能再次打印。
去重跨渠道共享：切换渠道不会重新投递已成功的事件。`--json` 保持单个 JSON 结果，
terminal 渠道的卡片写 stderr。投递状态损坏或不可写时明确报错，不改任务结果。

接手入口为 `cs status <id> --no-reclaim`。可 resume 的 Codex/CC 单只提示使用现有
`cs submit --resume-of <id>` 并补齐参数；仍校验终态、review、原 cwd、pool 和 session ID。
OpenCode、未知池、缺 session ID 或 review 单明确提示「从接手卡新开」，不提供虚假一键续聊。

## 改动范围与重试

`--files` 和 `--lock-shared` 仍用于原有锁调度，companion 许可不会自动加锁。
声明文件包含本单这两份列表；默认 companion 策略覆盖模板、dispatcher 或目标中通用的
“strictly within --files; STOP BLOCKED”措辞，允许无需询问地修改以下伴随文件：

- 同 stem 测试：`tests/**/test_<stem>*.py`、`<stem>_test.py`、`<stem>.test.ts(x)`、
  `<stem>.spec.ts(x)`，以及同 feature 目录 `e2e/` 下同 stem 的 JS/TS spec、cy、e2e 文件。
- 任意位置的 `fixtures/`、`__snapshots__/`、`testdata/` 内容及 `*.snap`。
- 声明文件祖先包目录中的 `__init__.py`。
- 同目录 manifest 的 lock：`package.json` 对应 package-lock/pnpm-lock/yarn.lock；
  `pyproject.toml` 对应 uv.lock/poetry.lock；Cargo.toml 对应 Cargo.lock；go.mod 对应 go.sum。
- 任意位置 `dist/`、`build/`（含 `static/dist/`）、`*.min.js`、`*.map`；
  i18n/locales/translations 中位于 `generated/` 下或名含 `.generated.` 的生成 bundle。

其他文件仍须范围受阻；`strict` 保留旧措辞，清单外一律先申请。
`flex` 允许任务所需的任何非 shared/core 文件；未声明的共享/核心保护路径在所有策略下仍严格。
声明显示同仓库其他运行任务共享锁的保护快照，项目模板中的核心保护也仍有效。
read-only 不因范围策略而变为可写。规则是简单路径启发式，不是权限执行器。

每个清单外改动（含 companion）必须在结论中列出：

```text
OUT_OF_SCOPE_FILES:
- tests/test_x.py: 验证模块修改
```

因范围受阻时写独立 `STATUS: BLOCKED`，并另写 `NEED_FILES: path1, path2`。
回收将其去重保存为 `need_files`，统计归入 `files_narrow`。

`files_narrow` 保留原主类，另存 `narrow_reason_class`，stats JSON 的
`narrow_reason_classes`（含 pool/project 分组）、终态事件与 `cs board` 的近 2h 汇总展示六种子因：

- `companion_miss`：companion 策略仍请求追加文件，含未识别已许可伴随文件的情况。
- `strict_companion_known`：strict 下所请求的清单外文件全部属于已知 companion。
- `shared_or_core`：命中已记录的共享保护路径/范围违规，或结论明确指出共享/核心限制。
- `permission_or_lock`：只读任务、操作系统权限或运行时锁阻碍。
- `unclear_input`：路径/输入不明，缺少待追加路径，或所请求路径已在声明中。
- `other`：其余范围不足。以上为观测启发式，保护/权限/输入原因优先，不改变任务结果。

submit 从 Git 已知文件（含未忽略的新文件）提示具体的同 stem 测试、祖先 `__init__.py`、
manifest 对应 lock；每条候选包含理由及与 queued/running 任务的锁冲突（复用同 worktree/
跨 worktree shared 规则）。提示进入 stderr 与 JSON `warnings`；非 Git/清单读取失败时不猜测候选。
候选不写入 `files`，不增加任何锁，也不覆盖 shared/core 保护；需要扩权时仍须显式声明。

dispatcher 可显式重试：

```bash
cs retry <id> --use-need-files --add-files extra.py --scope-policy companion --tick --json
cs retry <id> --add-shared shared/core.py --idempotency-key retry-123 --json
cs retry <id> --run-delta '新增决定：保留接口；已试 A 失败；pytest 结果见原结论' --json
```

retry 只接受终态单，创建新 ID 并保存 `retry_of`；克隆项目、area/tier/cwd/risk/pin/model/read_only、
文件与共享锁、模板开关、范围策略及原始用户目标，重新生成声明。新增 files 不会自动升级共享锁；
原单不变。未给 `--tick` 只排队。新增单保存 `user_prompt`，旧单缺少原始目标时
retry 明确拒绝，需手工重新 submit；旧单无策略按 strict 继承。

retry 另存并附加 `run_delta`：来源 job ID/状态、结论路径、结论摘录、NEED_FILES 和失败信息；
结论中的运行新增决定、已试失败和验证结果因此可追溯，`--run-delta` 可补充人工执行增量。
原 `user_prompt`/Brief Decisions 和 `context_text` 沿用，不再复制一份 Brief。

历史证据不授予新权限；最多保留最近五次，每次结论摘录最多保留 8 KiB 原文，过长时保留首尾并标记截断，
完整内容见来源路径。缺失结论会明确标记，不把 worker exit 当验证通过。status/wait JSON 暴露该字段。
一次性瞬态自动 retry 同样保存增量；旧单只有完整 prompt 时仍按原样复用 prompt，增量保存在新单 JSON。

`submit --context path[:start-end]` 仍限制预读文本合计 64KB，并保存 `context_snapshot`：
`captured_at`、`base_sha`（Git HEAD，若可读）、`paths`（绝对来源路径）、`workspace_fingerprint`。
指纹包含 cwd 的 Git 脏状态/差异、未跟踪文件内容及来源文件内容；已有脏工作区未变化也可为 fresh。
显式 retry、瞬态自动 retry、显式 resume 保留原快照；retry/resume 提交时以及 status/wait 摘要读取时
比较当前 HEAD 和工作区，`freshness` 为 `unknown|fresh|stale`，接手卡 JSON/文本同步显示。
HEAD 变化或工作区/来源文件漂移时为 stale，警告写入 `warnings`，提示重新读取来源；不会阻止显式 retry。
旧单缺元数据、非 Git 或查询失败为 unknown，不把执行开始时的 `base_sha` 当作上下文版本。
这些检查是读取时的观测，不能保证读取期间或排队后的工作区不再变化；不会自动 resume。

回收 failed 单时，Codex transport 断开、模型 capacity、HTTP 502/503/504、API Error 50x、
Bad Gateway/Service Unavailable/connection reset 会触发一次自动排队，保存
`failure_reason=upstream_transient`、原单 `retried_as` 和新单 `retry_of`；由 tick 调度（可同轮启动）。
已有 retry_of 的单不再自动重试；done/blocked/cancelled/unknown 和非瞬态错误不重试。
重建声明沿用显式 retry 的提交字段和当前模板；旧单可复用保存的完整 prompt，缺失时记录 retry_error。
worker prompt 要求在 base/main 复跑确认已有失败，结论列 `Pre-existing failures`，继续交付。

启动时保存最后一次非空排队原因为 `last_queue_reasons` 及 `queue_wait_s`；终态事件保留这些字段，
`queue_reason_types` 从留存原因提取。submit 的 files/lock-shared 命中已有 running/queued 单的
shared_files 时，stderr 和 JSON warnings 列出任务 ID、路径并建议串行，不拒绝提交。

回收保存 `changed_files` 和 `scope`（declared/companion/out_of_scope/shared_violations），
共享保护集合来自结束时同仓库其他仍运行任务。只观测、不改结果状态；flex 的 out_of_scope
仍记录非伴随文件，不表示受阻。非 Git/缺 base_sha/查询失败保存空报告，不能据此证明没有改动。
快照可能含起点前的脏文件和同 cwd 并行任务改动，不是独占归因。
dispatcher 若要保留旧范围行为，请显式传 `--scope-policy strict`。

tick 默认输出一行 JSON 计数摘要；`tick --json` 返回完整调度明细。
`<状态根>/logs/tick.log` 每轮仅一行摘要，不重复输出排队原因。
默认按 1 MiB 轮转，另保留 3 份（`.1` 最新）；可用 `CS_TICK_LOG_MAX_BYTES`
（最小 256）和 `CS_TICK_LOG_BACKUPS`（可为 0）配置。
launchd 将 stdout 追加重定向到同一 tick.log 时只记 stdout 一份，轮转保留 inode；
定时调用使用 `cs tick`，避免把 `--json` 的完整明细重定向到该日志。
目录锁按解析后的目录在每单提交时只警告一次，tick 不重复警告；
锁原因按占用 job、当前文件和 kind 去重，board 对历史重复原因也只显示一次。

## 执行统计与只读看板

```bash
cs stats                         # 默认近 2h，按 finished_at 计入全部终态
cs stats --since 30m --project cs --pool codex --json
cs stats --since 2026-10-17T00:00:00+08:00
cs dashboard                     # 默认 http://127.0.0.1:8787
```

成功率 = done / (done + blocked + failed + unknown + cancelled)，空窗口返回 null；
耗时 avg/p50/p95 使用所有有 started_at/finished_at 的终态，百分位采用 nearest-rank。
同时输出状态分布、按池/项目分组、平均 prompt 字节数（缺失不计样本）。JSON 版本为 `"1"`。
`--since` 接受整数 m/h/d 或 ISO 时间；不带时区的 ISO 按 UTC+8 解释。

回收和取消时向 `{CS_HOME}/logs/events.jsonl` 追加一条终态事件，不改既有结果判定。
默认 1 MiB / 3 份备份，环境变量 `CS_EVENT_LOG_MAX_BYTES`（最小 256）、
`CS_EVENT_LOG_BACKUPS`（可为 0）控制轮转，事件备份只保留完整 JSON 行。
统计优先读取当前与轮转事件，对缺失 ID 扫描 done/*.json 只读补齐，按 ID 去重；
不会迁移旧记录、回填事件文件、回收任务、创建状态目录或锁文件。

看板新增近 2h 全局成功率、受阻/取消、耗时与三池切片，独立轮询只读
`GET /api/stats?since=2h`；API 同样支持 project/pool 过滤，非法 since 返回 400。
统计请求失败时明确显示旧数据/不可用，空窗口显示「—」，不会伪装成 0% 成功率。

## 池上限（caps）

Codex/CC/OpenCode 并发上限默认 24/6/32，运行时读取 `{CS_HOME}/caps.json`（`{"codex":24,"cc":6,"opencode":32}`，正整数），
其次环境变量 `CS_CODEX_CAP` / `CS_CC_CAP` / `CS_OPENCODE_CAP`，最后用默认值。tick、status、board、dashboard `/api/state`
每次重读，改 caps.json 无需改代码。该文件是运行时状态，不提交。

## OpenCode L 级路由

未 pin 的 L 级低风险任务优先走 `opencode`，池满回退原 Codex/CC 亲和选池。
低风险为 research + read-only，或 files ≤3、无 shared_files、risk≠high 且无高风险路径；
仓库相对路径前缀 `lib/core.py`、`lib/affinity.py`、`bin/`、`launchd/` 为高风险。
`--pin opencode` 只等待此池，`--pin codex|cc` 保持旧逻辑；OpenCode 不参与 spill。
默认模型 `opencode-go/deepseek-v4.1-flash`，variant 固定 `max`（2.x CLI 以 `model#max` 传入）；此池为订阅，豁免 quota.json 门禁。
JSONL text parts 提取为外部 conclusion.md，原始事件留在 worker 的 output.jsonl / log。
opencode 非零退出时 run.sh 仍把 text parts 提取到 last-message.md，并保留原退出码（结论归属见下文）。

**空闲判定**：reclaim/tick 检查 OpenCode 的 log 与 output.jsonl 的 mtime/size，并采样 worker 进程树
（run.sh 及全部后代）的 CPU。输出与 CPU（占用 ≥ `CS_OPENCODE_CPU_ACTIVE_RATIO`，默认 0.05 核）都静默
`CS_OPENCODE_IDLE_S`（默认 600）秒，或仅输出静默达 `CS_OPENCODE_MAX_SILENT_S`（默认 5400）秒硬上限，
才清理进程组并记 failed / `no_output_timeout`。跑长测试/构建的静默工具命令因此不再被误杀。

**结果判定**：`opencode run` 只要会话中出现过错误事件就以 1 退出，即使已自行重试恢复。若错误全是可自愈的
`provider.invalid-output`（流未带 finish_reason 截断）、最后一轮正常结束（`step_finish` stop，或截断前的收尾 text，
其后无待执行工具调用），且结论含整行 `STATUS: DONE|BLOCKED`，按 exit=0 判定，记 `recovered_exit` 与
`opencode_nonzero_exit_recovered` 警告；`exit` 保留真实值。

**失败原因**只取自 JSON 错误事件与非 JSON 的 stderr 行，不再扫描结论、正文或工具输出（避免
“forbidden ports”、`*-rate-limit.test.ts` 之类误判）：insufficient_funds、model_unavailable、auth、rate_limit（含
`provider.quota` / 429）、`stream_truncated`（只有可自愈错误但未交付）、`abrupt_exit`（停在一轮中途：
step_start / tool_use / step_finish tool-calls 后退出）、opencode_error；exit=124 仍为 unknown。

**额度暂停**：出现真实额度证据（rate_limit）时写 `<状态根>/pool_pause.json` 暂停 opencode 池
`CS_OPENCODE_QUOTA_PAUSE_S`（默认 1800）秒；暂停到期后的试探再失败则翻倍，上限 5 小时，2 小时内无复发重置。
暂停期间 `--pin opencode` 排队（原因 `pool_paused`），未 pin 的 L 级任务改走 Codex/CC 亲和选池；
同一波（±10 分钟）内 `abrupt_exit` / `stream_truncated` 的 OpenCode 失败归因为 rate_limit（`quota_attributed`）。
`cs pool` 查看，`cs pool --resume opencode` 手动解除；`cs status --json` 与看板 `/api/state` 带 `paused`。

**历史重判**：`cs reclassify [--since 24h] [--apply]` 用上述规则重放 OpenCode failed 记录，默认只预演；
`--apply` 写回 done 记录（`reclassified_from` 保留原状态）并追加事件，使 `cs stats` 采用新结果。重放不触发暂停。

可选真实冒烟命令（不属于自动测试；显式执行才调用真实模型）：

```bash
CS_HOME="$(mktemp -d /tmp/cs-opencode-real-XXXXXX)" bin/cs submit --project cs \
  --area research --read-only --tier L --pin opencode --cwd /tmp \
  --prompt-text 'Reply with STATUS: DONE and a short hello.' --no-template --tick --json
```

## 项目模板与额度快照

模板放在状态根目录 `projects/<project>.md`，默认 `~/.coding-scheduler/projects/<project>.md`。
submit 自动按“模板 + 本单声明 + 目标”生成 prompt；声明列出 cwd、files、shared_files、
area 和 read-only，空文件列表写“无”。`--no-template` 只关闭项目模板，仍保留声明。
可复制 [examples/projects/cs.md](examples/projects/cs.md) 后按项目调整。
worker prompt 仍会添加禁止子 agent 的硬规则。
任务进入 done / failed / blocked / cancelled 时，将 queue 中的原始 prompt 归档到
`~/.remote-jobs/<id>/prompt.md`（隔离时为 `<状态根>/workers/<id>/prompt.md`），
更新任务记录的 `prompt_file` 并删除 queue 副本；归档原文替换 worker 运行时添加规则的版本。
最终结论统一在外部 worker 目录 `~/.remote-jobs/<id>/conclusion.md`，隔离时为
`<状态根>/workers/<id>/conclusion.md`；prompt 中明确禁止在仓库创建、暂存、提交
`conclusion*.md`，根目录同名文件也由 `.gitignore` 排除。旧 worker 的 conclusion.txt 仍可回收。

**结论归属**：三个池的 CLI 都把最后一条消息写到同目录的 `last-message.md`（codex `-o`、claude stdout、
OpenCode text parts）。worker 按 prompt 自己写的 conclusion.md 不再被覆盖；worker 没写时才用最后一条消息补上。
过去 `codex exec -o conclusion.md` / `claude -p > conclusion.md` 会在退出时用简短摘要覆盖完整结论，
丢失 `NEED_FILES` / `OUT_OF_SCOPE_FILES`，导致 `cs retry --use-need-files` 失效。两份文件的最后 STATUS
不一致时记 `conclusion_status_mismatch` 警告（以 conclusion.md 为准）；`cs status ID --json` 的 artifacts 带 `last_message`。

**外部终止**：退出码 129–159（被信号杀死，非 cs cancel、非空闲超时）记 failure_reason `signaled`，不自动重试。

**CC 额度**：`claude -p` 整个输出为 `API Error: ... (429)` / “credit is used up” 时记 rate_limit，并按同样的
`pool_pause.json` 规则暂停 cc 池（时长同 `CS_OPENCODE_QUOTA_PAUSE_S`）：`--pin cc` 排队，未 pin 任务按亲和度改走 codex；
503 等仍走 upstream_transient 自动重试一次。`cs reclassify --pool codex|cc|opencode` 可重放历史。

目前无远程 quota 探测。可选 `<状态根>/quota.json` 为池级本地快照，例如：

```json
{"codex": 10, "cc": null}
```

正数表示已知有额度，0 表示 `quota_exhausted`，null/缺失/非法表示 `quota_unknown`。
OpenCode 池始终豁免。存在快照时阻止选中的 Codex/CC 池在额度未知/耗尽时启动；无文件沿用旧行为，不启用额度门禁。
快照由外部维护，不自动扣减、刷新或按额度切换副池；池选择仍按容量与亲和度规则。

## 隔离测试

`CS_HOME` 优先于原 `CODING_SCHEDULER_HOME`，都不设置时保留生产路径
`~/.coding-scheduler` 和 `~/.remote-jobs/<id>`。设置任一变量后 worker 产物改为
`<状态根>/workers/<id>`，项目模板也从该根读取。

```bash
/usr/bin/python3 -m unittest discover -s tests_cs
/usr/bin/python3 tests_cs/smoke_isolated.py
```

单测使用临时状态、Git 仓库、fake worker 或 stub CLI，不调用真实模型。
冒烟脚本只读复制 `CS_HOME`（兼容旧变量，未设置则 `~/.coding-scheduler`）
到 `/tmp/cs-AD-smoke-*`（源不存在则初始化空状态），
归档副本中旧任务/额度配置，设置隔离 `CS_HOME` 和仅测试用 `CS_TEST_FAKE_WORKER=1`，
执行 submit → tick → wait → status → board。它检查新任务状态和产物均写入临时 CS_HOME，
并保留 JSON 证据；源目录可由生产 launchd 并发更新，不比较源目录指纹。
假 worker 同步生成 log/conclusion/exit，拒绝默认生产状态根，不执行真实 codex/claude/opencode。

## 嵌套保护

worker 的 run.sh 导出 `CS_JOB_ID=<job id>`。设置了该变量时 `cs submit` / `cs retry` 默认拒绝（exit 2），需显式 `--allow-nested`。
