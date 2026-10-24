---
name: cs-dispatch
description: |
  用本机 coding-scheduler（`cs` / `~/.local/bin/cs`）高效派发编码与调研任务，并查询/等待结果。
  Use PROACTIVELY when: 多文件/跨模块实现、可后台不挡对话、方案已定后的「同步实现 / 提 MR / 迁移」、
  后端字段+API+测、可列清 `--files` 且 AC 可测的编码/调研。
  Do NOT use when: 一句话/单文件小改、主会话已读完且 ≤~3 步、方案未定 /「先定方案」、
  Figma/浏览器/AskUser 闭环、files 或 AC 未知、仅靠「派活」口令但 DoR 未过。
  禁止用本会话大块 Write/Edit 代替已 DoR 的大块实现；禁止直接 `codex exec` / `claude -p` / `opencode`。
---

# cs 派活（高效，而非尽量多派）

高效派活 ≠ 尽量多 `cs submit`。先凑齐 Definition of Ready（DoR），再对「可独立、可验收、文件边界清」的活做厚交接；小改、方案未定、GUI 闭环留主会话。少而准好过无效派活。

## 决策树（每次动手前走一遍）

```text
方案未定 / 「先定方案」/ 需 Explore→Plan？
  → 主会话 plan，禁止把探索对话丢进 cs
一句话 diff / 单文件小改 / 主会话已读完且 ≤~3 步 /
Figma·浏览器·AskUser 闭环 / worker 无所需 MCP？
  → inline（当前会话改），不 submit
DoR 全部通过 AND（多文件/跨模块 OR 可后台不挡对话）？
  → cs submit；禁止用本会话大块 Write/Edit 代替
未获用户「就在这改 / 不用 cs」时，对已 DoR 的大块实现 → 默认 cs
```

用户明确说「就在这个会话里改 / 不用 cs」时，尊重口令，即使 DoR 已过也不强派。

## 强制派（正例）

出现 ≥2 条且 DoR 已过时，**必须** `cs submit`，不要在当前对话里直接大改：

- 多文件迁移 / 跨模块重构（如分镜脚本迁仓、多目录搬迁）
- 后端字段 + API + 测试（或前端多文件「同步实现」）
- 方案已定后的「提 MR」「按方案落地」「后台跑」
- 可并行、可用 `--files` 列清、不挡当前对话的编码 / 调研 / review

## 禁止派（负例）

任一命中则**不要** submit（先 inline 或先凑 DoR）：

- 单文件样式/文案小改、一句话 diff
- 「先定方案」「先对齐再改」、方案/接口语义还在争
- Figma / 浏览器点选 / AskUser 来回（worker 通常无该闭环）
- files 列不清、AC 不可测、Unknowns 未写明
- 纯口头解释、读一段代码讲含义（短、同步即可）
- 仅为「用上 cs」而拆的 ≤~3 步小活（无效派活）

## submit 前 DoR checklist

**任一为否 → 不 submit**，回主会话补齐或改 inline：

1. **Goal**：一句话目标（用户要什么）
2. **Current → Desired**：现在什么样、改完什么样
3. **Out of scope**：明确不做什么
4. **≥2 条可测 AC**：能用命令或可观察结果判定
5. **可写 `--files`**：具体文件路径（非目录），且与并行单**无重叠**
6. **Decisions / Already tried**：已拍板决策、试过但无效的路径
7. **Unknowns**：未知项（没有就写「无」；有未知且会挡实现 → 先主会话澄清）
8. **idempotency**：唯一 `--idempotency-key`（建议 `日期-主题-短哈希`）
9. **≥1 条 verify 命令**：worker 结束前应跑的检查（测/类型/lint/构建）
10. **是否允许开 MR**：允许 / 禁止 / 仅准备分支（写进 prompt；写操作仍须用户授权）

并行额外约束：独占 files + 输入已在 base + 能独自证明（测/类型/lint）。共享类型、迁移、注册表入口 → 先 foundation 串行，再 fan-out。

## 硬规则（池与绕过）

1. 走决策树：该 cs 则必须 `cs submit`，禁止本会话大块 Write/Edit 代替；该 inline / plan 则禁止硬派。
2. **不要**直接跑 `codex exec`、`claude -p`、`opencode`（绕过队列与锁）。
3. 本机默认池：**只使用 `codex` 或 `opencode`**。不要 `--pin cc`（除非用户明确改口）。
4. 写操作（改代码、commit、push、开 MR、发飞书）仍须用户明确授权；`cs` 只负责任务调度。
5. worker 结论必须有独立一行：`STATUS: DONE` 或 `STATUS: BLOCKED`（受阻再写 `NEED_FILES: ...`）。

## 池与模型

| 池 | 怎么 pin | 模型 |
|---|---|---|
| **codex**（默认优先） | `--pin codex` | 一般不传 `--model`，用池默认 |
| **opencode** | `--pin opencode` | 调度器默认 `opencode-go/deepseek-v4.1-flash`，variant 固定 `max`；一般**不用**再传 `--model`。若显式指定：`--model opencode-go/deepseek-v4.1-flash`（直接跑时默认 `#high`） |
| cc | 不用 | — |

L 档若不指定池，也要显式 `--pin codex`（或用户指定的 opencode），避免误分到不可用池。

## prompt-text 最小模板（Agent Brief）

transcript ≠ 合同。把下面填进 `--prompt-text`（或 `--prompt` 文件）；不要整段粘贴探索对话。

```text
Goal: <一句话目标>

Current:
- <现状 1>
- <现状 2>

Desired:
- <改完样貌 1>
- <改完样貌 2>

Out of scope:
- <不做的事>

Acceptance Criteria:
1. <可测 AC>
2. <可测 AC>
Verify: <至少一条命令，如 pnpm test / go test / rg ...>

Files (writable only; for location, not a hunting license):
- path/a.ts
- path/b.ts

Decisions:
- <已拍板>

Already tried:
- <试过且失败的路径，或「无」>

Unknowns:
- <无 | 剩余未知>

Constraints:
- 只改 --files（及 companion 策略允许的伴随文件）
- 结论末行必须 STATUS: DONE 或 STATUS: BLOCKED
- MR: <禁止 | 仅本地提交 | 允许开 MR（仍须用户授权后再 push/开）>
```

## 派发模板

```bash
cs submit \
  --project <短项目名> \
  --area <backend|protocol|frontend|ui-copy|test|review|ops-script|research> \
  --tier <L|M|H> \
  --cwd <仓库或 worktree 绝对路径> \
  --prompt-text '<按上方 Agent Brief 填好>' \
  --files <可写文件1>,<可写文件2> \
  --pin codex \
  --idempotency-key <唯一键> \
  --tick --json
```

- 只读调研：`--area research --read-only`，此时 `--files` 可空。
- 非 research 的 `--read-only` 仍必须列 `--files`（例如要写的评审文档路径）。
- `--files` 必须是**具体文件路径**，不要填目录。
- `--idempotency-key` 建议：`日期-主题-短哈希`，同一 key 不会重复开工。

opencode 示例：把 `--pin codex` 换成 `--pin opencode`（模型/effort 由调度器默认：`opencode-go/deepseek-v4.1-flash` + `max`；直接 `opencode run` 时默认 `-m opencode-go/deepseek-v4.1-flash#high`）。

## 查状态 / 等待 / 重试

```bash
cs status <id> --json --lines 20
cs wait <id> --tick --timeout 600
cs cancel <id> --json
cs retry <id> --use-need-files --tick --json   # 或 --add-files a,b
cs board --json
cs doctor --cwd <路径> --pool codex
```

看板：`http://127.0.0.1:8787`（只读查看）。

## 当前会话还做什么

- 跑决策树 + DoR checklist；该 plan 先 plan，该 inline 就短改
- 帮用户写清 Goal / AC / `--files` / pool/tier；用 Agent Brief 填 `--prompt-text`
- 执行 `submit` / `status` / `wait`，把 JSON 里的 id、状态、结论摘要译成中文
- 决策、浏览器点选、是否 commit/push/开 MR：留在当前会话，等用户拍板

## 与其它 skill

- 开干前用 `session-split` 确认泳道
- 契约对照用 `contract-align`（可 `--area protocol --read-only` 派调研）
- 验收用 `verify-loop`；重活 `--area review`
- 同步同事用 `mr-card`（cs 不管发消息）

## 安装（本 skill 在仓库内的权威副本）

权威路径：`coding-scheduler/skills/cs-dispatch/SKILL.md`（本文件）。

链到 Claude Code skills 目录（任选其一）：

```bash
# 推荐：符号链接，改仓库即生效
ln -sfn "$PWD/skills/cs-dispatch" ~/.claude/skills/cs-dispatch

# 或复制
mkdir -p ~/.claude/skills/cs-dispatch
cp skills/cs-dispatch/SKILL.md ~/.claude/skills/cs-dispatch/SKILL.md
```

全局偏好里应点名本 skill，并用同一决策树（勿写「永远必须派」）。
