# cs-AD 交付结论

2026-10-16；分支 `cs/AD`，基线 `cf564b4`。
实现提交：`371559a2221cfe6a2cdef0525eee9cb0f5382569`。
本结论另作文档提交，最终提交可用 `git log -1 --format=%H` 查询。

## 改动摘要

- A：tick 检查全部独立原因，持久化 `queue_reasons` 与 ISO `last_checked`。
  锁项复用 B 的 `blocked_by`（挡路单号、双方文件、write/shared 类型、repo），
  池满与 spill 分数原因含主副池、亲和分、空位/最低分；额度未知与耗尽分开。
  board 增加人话“原因”列；submit 立即返回自己的原因，不再仅给 started 空列表。
- D：全命令支持版本化 `--json`，错误也有 schema_version；新增全根目录幂等 key，
  queue/running/done 均可复用；状态查询支持历史单与耗时、退出码、结论、产物路径。
  无有效 exit 且 worker 已消失、或 exit=124 时为 unknown，不当成功/失败。
- 低 token：wait 支持多单、超时、限行结论、完整结论中的 PR 链接去重，
  start 记录 base_sha，摘要包含 cwd 相对该起点的 diff stat 与文件列表。
  新增 prompt-text/stdin、模型透传、research area、项目模板与 no-template 开关。
- 隔离与兼容：保留原默认状态/产物路径、老环境变量、默认模型和锁语义；
  CS_HOME 可隔离状态/模板/worker 产物；新增同步测试假 worker，拒绝默认生产根。
  doctor 将非 Git、无关池 CLI、DNS/信任提示降为 WARN；支持 --pool 选择必需 CLI。
- 根级 flock 与原子 JSON 替换保护 bot 重复提交；worker EXIT trap 原子保存退出码，
  解决 set -e 导致非零退出没有 exit 文件的问题。模型与路径按 shell 参数引用。
- README、DESIGN、BACKLOG 和 `examples/projects/cs.md` 已更新。

## 测试证据

环境：`/usr/bin/python3` = Python 3.9.6（macOS）。

```bash
/usr/bin/python3 -m unittest discover -s tests_cs
```

结果：49 tests，全部 OK（最终一次 1.702s）。日志：`/tmp/cs-AD-tests.log`。
覆盖多锁与多原因、池满/spill、额度未知/耗尽/正常/缺失/坏数据、原因清除，
全命令 JSON 与错误、幂等跨三种目录、重复 ID、unknown/终态分类、历史状态查询，
wait 多单逐步结束/释放锁/超时/限行、完整 PR 提取、起点后提交与工作区 diff，
三种 prompt 来源、模板开关、默认与自定义模型、安全引用和非零 exit、doctor WARN，
老状态、旧环境变量、原 B 锁语义。

真实启动脚本仅使用隔离 PATH 下的 stub codex/claude 验证；没有运行真实模型 CLI。
`git diff --check` 通过。

## 冒烟步骤与结果

```bash
/usr/bin/python3 tests_cs/smoke_isolated.py
```

1. 只读复制现有 `~/.coding-scheduler` 到 `/tmp/cs-AD-smoke-ynjppzik/state`，
   将副本 queue/running/done 与 quota 移到 inherited-snapshot，避免操作历史生产 PID。
2. 在同一临时目录创建独立 Git fixture，设置 CS_HOME 指向该副本、CS_TEST_FAKE_WORKER=1。
3. submit（仅入队）→ tick（同步假 worker）→ wait → status <id> → board。
4. 五步均退出 0、输出 schema_version="1"；任务 done、exit=0、changed_files=[]，
   conclusion/log/board 产物校验通过。
5. 比对复制前后源状态文件指纹，`source_exists=true`、`source_unchanged=true`。

最终证据：`/tmp/cs-AD-smoke-ynjppzik/report.json`，同目录保存五条命令 JSON；
汇总日志：`/tmp/cs-AD-smoke.log`。
只在指定 worktree 修改与提交；未改主目录，未写生产状态，未 merge，未 push；
未启动子 agent 或真实 codex/claude worker。

## 新用法示例

以下为实际调度用法，执行会按所选状态根调度；纯测试应运行上述隔离冒烟脚本。

```bash
# 模板可从 examples/projects/cs.md 复制到 <CS_HOME>/projects/cs.md；此路径默认在用户状态根。
cs submit --project cs --area research --tier M --cwd "$PWD" \
  --prompt-text '分析指定问题，给出证据和结论' --model gpt-6.1-sol \
  --idempotency-key issue-123 --tick --json

printf '%s\n' '只修改目标文件，完成后写结论' | cs submit --project cs --area test \
  --tier L --cwd "$PWD" --prompt - --no-template --tick --json

cs wait job-a job-b --timeout 600 --lines 10
cs status job-a --json
cs doctor --cwd /tmp --pool codex --json
cs board --json
```

已有 `--prompt examples/prompt-l.md` 用法保留。`--json` 可放在命令前或后。
wait 超时退出 124 并返回当前状态；全部进入终态退出 0，各单成功与否看 status/exit。

## 遗留与边界

- 基线没有真实 quota 数据源。可选 `<状态根>/quota.json` 使用池→非负数字/null 的
  本地快照；无文件保持旧行为，有文件才对未知/耗尽门禁。未实现远端探测、自动刷新、
  扣减或按额度 spill。此处没有真实上游额度或模型调用验收。
- wait 只回收、不启动 queued 单；仍需 tick / submit --tick / 现有 launchd 推进。
  wait 超时不代表 worker 超时；仅退出码 124 等 worker 结果不明证据映射 unknown，
  本次未新增 worker 执行时限配置或强杀策略。
- Git 统计是查询时 cwd 的快照，不是独占归因；同 cwd 其他单/起点前脏改动也可能出现。
  untracked 不在 git diff 中；非 Git、旧单没有 base_sha、Git 不可查询时省略统计。
- 临时冒烟副本/日志留在 /tmp 供复核；含复制的本地状态，不作为公开提交物。
