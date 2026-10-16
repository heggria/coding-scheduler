# 轻量编码调度 CLI（cs）

不做常驻守护。Pro 上一个 CLI；bot 交单后调 `cs tick`，或 `cs submit --tick`。

## 命令

```bash
cs submit --project mm --area backend --tier H \
  --cwd ~/code/worktrees/x --prompt ./job.md --files src/foo/ [--pin codex] [--tick]
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
- 同文件前缀不并行

## 选池

亲和分（与 skill local-coding-dispatch 一致）→ 主池有空位就放；否则副池分 ≥45 才 spill；否则留 queue。

## 目录

`~/.coding-scheduler/{queue,running,done,board.md}`
`~/.remote-jobs/<id>/{pid,log,exit,conclusion.txt}`

## Bot 用法

只 `cs submit` / `cs tick` / `cs status`，不要自己直接 `codex exec` 抢槽。
