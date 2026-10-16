# cs 独立项目需求池

真相源：本文件（Pro `~/code/coding-scheduler/BACKLOG.md`）。改代码前看 `DESIGN.md`、`cs-fix-board.md`。

## 待办
1. worktree 能力：submit 时可选 `--worktree`，cs 自动给每单开独立 git worktree（分支名=job id），跑完保留供合并；文件锁只用于明确改同一段核心代码的几单。
2. 文件锁收窄：拒绝/告警整目录锁（如 `tests/`），要求写到具体文件。
3. 失败自动续跑（之前 wave1 未做）。
4. 生产状态根目录约 142 个续跑 / bak / tgz 文件需清理（本次不删除）。

## 已完成
- wave1：status 回收僵尸、exit0 但无结论→blocked、doctor 预检、cancel --superseded-by。
- launchd 每 60s 自动 tick（`com.heggria.cs-tick`）。
- Codex/CC 完全访问权限（bypass sandbox / skip-permissions）。
- B：repo/worktree/file 锁、blocked_by、跨 worktree `--lock-shared`（基线 cf564b4）。
- A：每单全部结构化 queue_reasons、last_checked、池满/spill/额度未知与耗尽、board 原因列、submit 即时解释。
- D：全命令版本化 --json、--idempotency-key、unknown、status <id> 查询历史单摘要。
- 低 token：wait 多单/超时/限行摘要、base_sha diff、PR 链接、prompt-text/stdin、--model、research area。
- 项目模板：projects/<project>.md 自动前置、--no-template、examples/projects/cs.md。
- 隔离：CS_HOME（兼容 CODING_SCHEDULER_HOME）、隔离 worker 产物、同步假 worker、复制状态的冒烟脚本。
- doctor：非 Git 与无关池检查 WARN，--pool 指定必需 CLI。

## 后续边界
- quota.json 为可选外部维护快照；真实额度 API、自动刷新/扣减/按额度 spill 尚未实现。
- wait 默认只回收；可选 --tick 在等待期间推进全局队列，或由现有 launchd 负责。
