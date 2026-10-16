# cs — coding scheduler CLI

纯 CLI，无守护进程。设计见 `DESIGN.md`。

```bash
export PATH="$PWD/bin:$PATH"
cs doctor
cs submit --project mm --area backend --tier L --cwd /tmp --prompt examples/prompt-l.md --tick
cs status
```

装到 Pro：拷到 `~/code/coding-scheduler`，把 `bin` 加进 PATH。
