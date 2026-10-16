# cs 独立项目需求池

真相源：本文件（Pro `~/code/coding-scheduler/BACKLOG.md`）。改代码前看 `DESIGN.md`、`cs-fix-board.md`。

## 待办
2. worktree 能力：submit 时可选 `--worktree`，cs 自动给每单开独立 git worktree（分支名=job id），跑完保留供合并；文件锁只用于明确改同一段核心代码的几单。
3. 文件锁收窄：拒绝/告警整目录锁（如 `tests/`），要求写到具体文件。
4. 失败自动续跑（之前 wave1 未做）。
5. 生产状态根目录约 142 个续跑 / bak / tgz 文件需清理（本次不删除）。

## 已完成
- Observability：终态 JSONL 事件（轮转）、`cs stats`（默认近 2h、过滤、分组、耗时/prompt 均值、旧记录只读补齐）、只读 `/api/stats` 与 Command Center 近 2h 成功率/受阻/取消/耗时/三池面板。结果判定保持原语义；实现、隔离验证、合并及 dashboard 部署证据分别见任务外部 conclusion。
- OpenCode 第三池：L 级低风险优先、满池回退原双池；显式 pin 不外溢，Codex/CC 不向 OpenCode spill。Deepseek 免费模型默认、variant=max、quota 豁免、caps 默认 32、doctor/CLI/dashboard 三池支持。实现与隔离测试交付；真实模型验收未执行，部署以任务结论为准。
- wave1：status 回收僵尸、exit0 但无结论→unknown（已修正早期 blocked 规则）、doctor 预检、cancel --superseded-by。
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
