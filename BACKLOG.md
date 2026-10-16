# cs 独立项目需求池

真相源：本文件（Pro `~/code/coding-scheduler/BACKLOG.md`）。改代码前看 `DESIGN.md`、`cs-fix-board.md`。

## 待办
1. worktree 能力：submit 时可选 `--worktree`，cs 自动给每单开独立 git worktree（分支名=job id），跑完保留供合并；文件锁只用于明确改同一段核心代码的几单。
2. 文件锁收窄：拒绝/告警整目录锁（如 `tests/`），要求写到具体文件。
3. tick 解释：`cs status` 对每条 queued 标明卡住原因（文件锁撞了谁 / 池满 / 亲和度不足）。
4. 失败自动续跑（之前 wave1 未做）。

## 已完成
- wave1：status 回收僵尸、exit0 但无结论→blocked、doctor 预检、cancel --superseded-by。
- launchd 每 60s 自动 tick（`com.heggria.cs-tick`）。
- Codex/CC 完全访问权限（bypass sandbox / skip-permissions）。
