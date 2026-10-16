# cs — coding scheduler CLI

纯 CLI，无守护进程。设计见 `DESIGN.md`。

```bash
export PATH="$PWD/bin:$PATH"
cs doctor
cs submit --project mm --area backend --tier L --cwd /tmp --prompt examples/prompt-l.md \
  --files src/task.py --lock-shared src/core.py,src/credits.py --tick
cs status
```

装到 Pro：拷到 `~/code/coding-scheduler`，把 `bin` 加进 PATH。

`--files` 仅锁同 worktree；`--lock-shared` 在同仓库跨 worktree 互斥。
目录锁会警告但仍接受。旧任务会惰性解析，迁移说明见 `DESIGN.md`。

本地测试：`python3 -m unittest discover -s tests_cs`（临时 Git 仓库与调度状态，不启动 worker）。
