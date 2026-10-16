# 轻量编码调度 CLI（cs）

不做常驻守护。Pro 上一个 CLI；bot 交单后调 `cs tick`，或 `cs submit --tick`。

## 命令

```bash
cs submit --project mm --area backend --tier H \
  --cwd ~/code/worktrees/x --prompt ./job.md --files src/foo.py \
  [--lock-shared src/core.py,src/credits.py] [--pin codex] [--tick]
cs tick
cs status [--json]
cs cancel <id>
cs board
cs doctor
```

## 硬限制

- Codex ≤ 16 · CC ≤ 6
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
  打印 `WARNING`，仍接收任务并保留目录前缀锁。

submit 保存含原始输入与解析结果的 `lock_ctx`。旧任务只有 `cwd` / `files` 也可在
tick 时惰性解析，缺少 `shared_files` 视为空；无需改写历史任务。锁输入变化后重新解析。
`file_conflict` 返回结构化列表（无冲突为空），含 `job`、`mine`、`theirs`、`kind`
（任一方共享则为 `shared`，否则 `write`）与 `repo`。排队任务的 `blocked_by`
只在冲突变化时写入，解锁后清除。

**mm / OD 提交方迁移**：同仓库不同 worktree 修改同一文件，现在默认可以并行。
如核心文件在合并时仍需串行，请显式增加 `--lock-shared`；已有 `--files` 不会自动
升级为共享锁。检查整目录 / cwd 锁的警告，尽量声明实际文件范围。

## 选池

亲和分（与 skill local-coding-dispatch 一致）→ 主池有空位就放；否则副池分 ≥45 才 spill；否则留 queue。

## 目录

`~/.coding-scheduler/{queue,running,done,board.md}`
`~/.remote-jobs/<id>/{pid,log,exit,conclusion.txt}`

## Bot 用法

只 `cs submit` / `cs tick` / `cs status`，不要自己直接 `codex exec` 抢槽。
