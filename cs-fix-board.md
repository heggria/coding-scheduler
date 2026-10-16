# cs 小改板（真实痛点 → 改动）

更新：2026-10-15 23:52 UTC+8
权威状态：Pro `~/.coding-scheduler/board.md`（`cs status` 按需拉取）；bot 侧不镜像。

## 目标（本波）

| # | 痛点 | 改动 | 状态 |
|---|---|---|---|
| 1 | 假完工：exit=0 但结论写未推/未开 PR/沙箱挡 | reclaim 仅认独立 `STATUS: BLOCKED` 行 → `blocked` | 做 |
| 2 | 尸位 running：死进程还占板 | `cs status` 先 `reclaim` 再展示 | 做 |
| 3 | 环境反复踩坑 | `cs doctor` 预检 git 可写 / github / trusted-dir / CLIs | 做 |
| 4 | cancelled 看不清谁替谁 | `cs cancel --superseded-by`；submit 同 id 前可标 | 做 |

## 不做（本波）

- 守护进程 / 远程推板到 bot
- 自动续跑（仍由监督席决定）
- 改亲和分/池硬顶

## 验收

1. `cs status` 后，已有 exit 文件的 running 进 done，板计数下降。
2. exit=0 且非空结论含独立 `STATUS: BLOCKED` 行 → status=`blocked`；自然语言不匹配，空结论为 unknown。
3. `cs doctor` 输出 ok/fail 项。
4. `cs cancel old --superseded-by new` 在 done 记录里能看到字段。
