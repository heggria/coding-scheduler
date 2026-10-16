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

## Bot 与低 token 用法

所有命令可在命令前或后加 `--json`，单个 JSON 对象带 `schema_version: "1"`；
`tick`、`wait` 默认就输出 JSON。JSON 模式错误也返回同一版本字段和非零退出码。

```bash
cs submit --project cs --area research --tier M --cwd "$PWD" \
  --prompt-text '分析排队瓶颈，给出结论' --idempotency-key issue-123 --model gpt-6.1-sol --tick --json
printf '%s\n' '目标与验收条件' | cs submit --project cs --area test --tier L \
  --cwd "$PWD" --prompt - --tick --json
cs wait job-a job-b --timeout 600 --lines 10
cs status job-a --json --lines 20
cs board --json
```

- `--prompt 文件`、`--prompt -`（stdin）、`--prompt-text 文本` 三选一。
- 同一非空 `--idempotency-key` 重试返回 queue/running/done 中已有任务，不覆盖 prompt，
  不重复启动；key 在状态根目录内全局唯一。显式 `--id` 重复则报错，自动 ID 带随机尾缀。
- `--model` 透传给所选 codex/claude，省略沿用默认模型；`research` 为新增 area。
- submit 默认只排队，并立即返回全部已知阻塞原因和 `awaiting_tick`；`--tick` 才尝试启动。
- 每次 tick 更新 `last_checked`（ISO）、全部 `queue_reasons`，board 的“原因”列显示短句。
  原因包括锁冲突（谁/文件/锁类型）、池满、spill 分数不足、额度未知/耗尽、启动错误。
- wait 等全部任务进入终态（done/failed/blocked/cancelled/unknown），只回收、不启动 queued 任务；
  启动调度需另用 `tick`、`submit --tick` 或现有 launchd。
  wait 成功退出 0 表示等待完成，任务是否成功看各单状态；超时退出 124，保留当前状态。
- wait / 单单 status 返回状态、退出码、耗时秒、产物 log/conclusion/worktree 路径、
  conclusion 前 N 行（默认 20）及从完整 conclusion 提取的 GitHub PR 链接。
  Git 任务 start 时记 `base_sha`，摘要在 cwd 用 `git diff --stat <base_sha> -- .` 及文件列表，
  包括起点后的已提交和未提交跟踪文件改动；未跟踪文件不在 git diff 中。
  非 Git、旧单缺少 base_sha 或 Git 查询失败时省略 diff 字段。
- worker 消失且无有效 exit 文件、或 exit=124 超时记 `unknown`，不当成功/失败；
  wait 自身超时不改任务状态。exit=0 但缺少/空 conclusion 记 blocked。
- doctor 的非 Git、DNS、信任提示及与所选池无关的 CLI 检查失败为 WARN；
  `--pool codex|cc` 指定的 CLI 缺失、cwd 无效、Git 元数据不可写仍为 FAIL。

## 项目模板与额度快照

模板放在状态根目录 `projects/<project>.md`，默认 `~/.coding-scheduler/projects/<project>.md`。
submit 自动按“模板 + 空行 + 目标”生成 prompt，`--no-template` 可关闭。
可复制 [examples/projects/cs.md](examples/projects/cs.md) 后按项目调整。
worker prompt 仍会添加禁止子 agent 的硬规则。

目前无远程 quota 探测。可选 `<状态根>/quota.json` 为池级本地快照，例如：

```json
{"codex": 10, "cc": null}
```

正数表示已知有额度，0 表示 `quota_exhausted`，null/缺失/非法表示 `quota_unknown`。
存在快照时阻止选中池在额度未知/耗尽时启动；无文件沿用旧行为，不启用额度门禁。
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
冒烟脚本只读复制 `~/.coding-scheduler` 到 `/tmp/cs-AD-smoke-*`（源不存在则初始化空状态），
归档副本中旧任务/额度配置，设置隔离 `CS_HOME` 和仅测试用 `CS_TEST_FAKE_WORKER=1`，
执行 submit → tick → wait → status → board。它检查源文件指纹不变并保留 JSON 证据。
假 worker 同步生成 log/conclusion/exit，拒绝默认生产状态根，不执行真实 codex/claude。
