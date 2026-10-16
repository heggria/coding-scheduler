# 轻量编码调度 CLI（cs）

不做常驻守护。Pro 上一个 CLI；bot 交单后调 `cs tick`，或 `cs submit --tick`。

## 命令

```bash
cs submit --project mm --area backend --tier H \
  --cwd ~/code/worktrees/x --prompt ./job.md --files src/foo.py \
  [--lock-shared src/core.py,src/credits.py] [--pin codex] [--tick]
cs tick
cs status [<id>] [--json]
cs wait <id> [<id>...] [--tick] [--timeout SEC] [--lines N]
cs cancel <id>
cs board
cs doctor
```

## 硬限制

- Codex ≤ 24 · CC ≤ 6 · OpenCode ≤ 32（上限放在 `{CS_HOME}/caps.json`，如 `{"codex":24,"cc":6,"opencode":32}`；其次环境变量 `CS_CODEX_CAP` / `CS_CC_CAP` / `CS_OPENCODE_CAP`；默认 24/6/32。tick/status/board/dashboard `/api/state` 每次重读，改文件即生效）
- 禁止子 Agent（prompt 注入）
- L 档 CC = haiku-5.5
- 同 worktree 的重叠写锁不并行；跨 worktree 的核心资源用共享锁

## 锁语义 / Lock semantics

每条锁解析为 `(repo_id, worktree_root, relpath)`：`repo_id` 为绝对 Git
common-dir，`worktree_root` 为 `git rev-parse --show-toplevel`。相对路径先按
任务 `cwd` 解析，再转为 worktree 相对路径。worktree 内的绝对路径同样归一化；
外部路径从最近的已存在父目录查找其所属仓库。非 Git 路径或 Git 查询失败时，
`repo_id=None`，使用解析后的绝对路径匹配。Git 子进程有超时，每次 tick 独立缓存查询。

- `--files a,b`（默认写锁）：仅同一 `(repo_id, worktree_root)` 中的路径重叠会互斥。
- `--lock-shared a,b`（核心资源锁）：保存为 `shared_files`；同一 `repo_id` 内跨
  worktree 与写锁、共享锁均互斥，只需冲突的一方声明共享锁。
- 路径重叠保留前缀语义：相等，或一方是另一方按 `/` 分隔的祖先。worktree 根锁覆盖
  整个 worktree；不同仓库的同名路径不冲突。非 Git 路径按绝对路径前缀互斥。
- 提交目录锁（已存在目录、以 `/` 结尾、或解析到 cwd / worktree 根）时向 stderr
  打印 `WARNING`，同单同解析目录只在提交时警告一次，tick 不重复；仍保留目录前缀锁。

submit 保存含原始输入与解析结果的 `lock_ctx`。旧任务只有 `cwd` / `files` 也可在
tick 时惰性解析，缺少 `shared_files` 视为空；无需改写历史任务。锁输入变化后重新解析。
`file_conflict` 返回结构化列表（无冲突为空），含 `job`、`mine`、`theirs`、`kind`
（任一方共享则为 `shared`，否则 `write`）与 `repo`。排队任务的 `blocked_by`
内容只在冲突变化时改变，解锁后清除；每次检查仍保存 last_checked。
同占用 job + 当前规范文件 + kind 的多条重叠锁仅保留首条冲突；不同 job/kind 保留。
board 渲染时再按原因短句去重，兼容历史记录。

**mm / OD 提交方迁移**：同仓库不同 worktree 修改同一文件，现在默认可以并行。
如核心文件在合并时仍需串行，请显式增加 `--lock-shared`；已有 `--files` 不会自动
升级为共享锁。检查整目录 / cwd 锁的警告，尽量声明实际文件范围。

## 选池

### Open Code / Deepseek（已决定）

第三池 `opencode` 默认模型 `opencode/deepseek-v4.1-flash-free`，所有 tier 的 effort/variant 固定 `max`。
`--pin opencode` 仅等待 OpenCode 空位，不 spill；`--pin codex|cc` 保持既有双池逻辑。
未 pin 的 L 级低风险任务有 OpenCode 空位时优先进入该池；池满则回到原 Codex/CC 选池。
其余任务不进入 OpenCode，Codex/CC 的副池永远不包含 OpenCode。

低风险 = `area=research AND read_only`，或同时满足 files ≤3、无 shared_files、risk≠high、
无高风险文件。文件按仓库相对路径归一化（支持绝对路径、子目录 cwd 和 `..`）；
高风险前缀为 `lib/core.py`、`lib/affinity.py`、`bin/`、`launchd/`，覆盖这些路径的目录锁也不走低风险通道。
research + read_only 是独立豁免。非 Git 路径按 cwd 相对路径检查。

原 `pick_pool` 保持双池：亲和分 → 主池有空位就放；否则副池分 ≥45 才 spill；否则留 queue。
锁仍阻止任何池启动。OpenCode 免费模型跳过 quota 门禁（缺失、null、0 均不阻塞）；
额度不足不触发跨池回退。OpenCode 的 `--model` override 必须以 `opencode/` 开头，提交选池和启动时均校验。

启动：`opencode run -m <model> --variant max --auto --pure --format json --dir <cwd>`，
从 worker 外部 prompt.md 读取完整 prompt 作为消息参数。stdout JSONL 保存在 output.jsonl 和 log，
仅 `type=text` 事件的 `part.text` 顺序写入外部 conclusion.md；命令/解析失败保留非零退出码。
所有真实 worker 使用 `start_new_session=True`，cancel 沿用进程组清理；fake worker 同样支持第三池。
status/board/dashboard 暴露三池 counts/caps，doctor 的 `--pool opencode` 要求 opencode CLI。

## 目录

`~/.coding-scheduler/{queue,running,done,board.md}`
`~/.remote-jobs/<id>/{pid,log,exit,conclusion.md}`（旧 conclusion.txt 只读兼容）

## Bot 用法

只 `cs submit` / `cs tick` / `cs status`，不要自己直接 `codex exec` 抢槽。

## A / D 与低 token 扩展（2026-10-16）

本次真相源：任务 A（排队原因）、D（bot 接口）及低 token 功能要求，基线 `cf564b4`。
范围不含自动 worktree、自动续跑、自动部署和远端额度探测。

| 行为 | 实现 | 验证 |
|---|---|---|
| A：每次完整检查锁/池/spill/额度，保存 ISO 检查时间 | `check_queue`、`pick_pool`、`tick`；board 原因列 | `test_tick_all_lock_capacity_spill_and_quota_reasons`、quota/清除原因测试 |
| D：版本化 JSON、幂等、unknown、历史单摘要 | CLI 统一 emit/errors；根级 flock；reclaim；job_summary | schema/idempotency/unknown/status 测试 |
| 等待多单，限时精简摘要 | wait 持锁回收后释放锁等待，返回终态或 124 | wait 多单、完成信号、超时、旧单测试 |
| prompt 来源、模板与模型 | submit 持久化组合 prompt，launch 注入硬规则和模型参数 | 三种输入/模板开关/模型与 shell stub 测试 |
| 可隔离状态与产物、doctor 降级 | CS_HOME 与旧环境变量；同步 fake worker；检查 level | 隔离保护/旧变量/doctor 测试；隔离复制冒烟 |

### 状态与写入

所有命令支持 `--json`（含参数/运行错误），响应版本为 `schema_version: "1"`。
根级 `.lock` 串行化 CLI 的提交/回收/调度/取消；JSON 用临时文件替换，避免半写。
wait 默认每次仅在回收和读取时持锁；可选 --tick 在每轮等待时持锁运行全局 tick，
也可能启动未列入等待列表的排队单。终态或缺失 ID 不发起调度。
等待完成退出 0，不代表各单成功；
等待超时退出 124，附当前摘要，不取消任务也不强改 unknown。

queue/running/done 目录保留，done 下可保存 done/failed/blocked/cancelled/unknown。
没有有效 exit 且 PID 已消失，或 exit=124 表示 worker 超时，结果为 unknown；
exit=0 无有效 conclusion 为 unknown，outcome_reason=missing_conclusion。
其他非零退出为 failed；退出码判定优先于结论。旧记录缺少新增字段按空/None 读取，不强制迁移。
所有 area/read_only 统一规则：exit=0 且非空结论只在存在独立 `STATUS: BLOCKED` 行时为 blocked，
block_reasons=["STATUS: BLOCKED"]；否则 done。逐行 strip 后区分大小写精确匹配，
不接受 Markdown 包装、尾随原因或 FAIL 标记，也不扫描任何自然语言短语。
模板要求成功写独立 `STATUS: DONE` 行、受阻写独立 `STATUS: BLOCKED` 行；
旧非空结论无需补写状态行即可按相同规则回收。
真实 worker 脚本用 EXIT trap 写退出码并原子替换 exit，非零退出不再丢失结果文件。

### 排队解释与额度边界

每个 queued 单无论先遇到什么阻塞，都评估全部独立约束并写 queue_reasons。
lock 原因直接携带 B 的 blocked_by 项（job/mine/theirs/kind/repo）；blocked_by 仅在
冲突变化时改变内容，但每次仍保存 last_checked。running 在回收检查时也更新该时间。
pick_pool 返回 `(pool, reasons)`，失败原因含 primary/secondary/scores，spill 最低分仍为 45。
submit 未指定 --tick 保留不启动语义，追加 awaiting_tick；指定时仍运行全局调度，
但响应仅含自己的状态/原因与摘要，不携带其他单或 schedule。

tick 默认 stdout 仅一行计数 JSON（reclaimed/started_count/counts/queued_count），
--json 才返回完整明细。tick.log 每轮只追加同样的计数摘要及时间，不存整份 queue_reasons。
CS_TICK_LOG_MAX_BYTES 默认 1048576（最小 256），CS_TICK_LOG_BACKUPS 默认 3（0 不留备份）。
达到大小上限时滚动 .1 至 .N；旧大日志仅保留末尾上限字节，防止历史日志突破容量。
轮转复制后原 inode 截断，兼容 launchd 已打开的追加 stdout；若 stdout 指向该日志，
内部不重复写入摘要。launchd 应运行默认 tick；显式 --json 重定向不受单行摘要大小保证。

代码基线没有 quota 数据源，因此使用可选状态根 quota.json 的池→剩余额度快照。
无快照保持旧行为；有快照时选中池的 0 / null或缺失分别对应耗尽 / 未知，阻止启动。
容量无可选池时报告所有亲和度合格池的额度问题，以免池满隐藏额度原因。
不自动刷新/扣减，不新增因额度而 spill 的策略。无远端真实额度验收声明。

### 摘要与模板

start 记录 cwd HEAD 为 base_sha；wait / status <id> 在 cwd 对比该 SHA 到当前工作区，
提供 diff_stat 和 changed_files，包含之后提交的改动（不是只比较当前 HEAD）。
非 Git 或旧单无 SHA 时省略。统计是读取时该 cwd 的实时快照，非每单独占的改动归因；
同 cwd 并行单和任务开始前已有的脏改动也可能出现在统计内，不含 untracked。
conclusion 仅输出前 N 行，PR 链接扫描全文并去重，支持 https://github.com/.../pull/N。

模板从 `<状态根>/projects/<project>.md` 读取并在 submit 时快照到 prompt_file，
后改模板不影响已排单；--no-template 可关闭。模型参数按 shell 参数引用传给 worker，
未指定保持默认；禁止子 agent 的硬规则仍统一置于最终 prompt 开头。
prompt 末尾统一追加结论输出规则，覆盖模板内旧路径；Codex -o / Claude stdout / OpenCode JSONL text 提取均写入
worker 外部目录 conclusion.md，不把结论作为代码仓库交付物。仓库根 conclusion*.md 忽略。

### 路径和检查

CS_HOME > CODING_SCHEDULER_HOME > ~/.coding-scheduler。未指定环境变量时产物仍在
~/.remote-jobs；显式隔离根则使用该根 workers/，绝不复用生产 worker 目录。
CS_TEST_FAKE_WORKER=1 仅在非默认的显式根启用，完全同步，不执行模型 CLI。
冒烟副本先归档 queue/running/done 与 quota，避免操作历史生产 PID 或启动旧任务。

doctor 增加 level=OK/WARN/FAIL；非 Git、无关池 CLI、DNS、信任提示只警告。
--pool 选中 CLI 缺失、无效 cwd 或 Git 元数据写入失败为硬失败。
Claude 信任提示原先已为 WARN；修正原始 JSON 子串匹配为 projects 中 cwd 准确规范路径
且 hasTrustDialogAccepted=true。缺失、未接受、格式错误均只 WARN；worktree 无条目不会 FAIL。
