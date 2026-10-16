// Isolated browser acceptance. Never starts workers or modifies launchd.
import assert from "node:assert/strict";
import { spawn, execFileSync } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, mkdir, writeFile, rm } from "node:fs/promises";
import { tmpdir, homedir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const repo = fileURLToPath(new URL("../../", import.meta.url));
const stateRoot = await mkdtemp(path.join(tmpdir(), "cs-dashboard-v2-"));
const iso = (minutes) => new Date(Date.now() - minutes * 60000).toISOString();
let server, browser;
const diagnostics = [];
async function record(folder, job) {
  await writeFile(
    path.join(stateRoot, folder, `${job.id}.json`),
    JSON.stringify(job),
  );
}
try {
  for (const folder of ["running", "queue", "done", "logs", "workers"])
    await mkdir(path.join(stateRoot, folder));
  const common = {
    tier: "M",
    area: "frontend",
    cwd: repo,
    project: "coding-scheduler",
  };
  for (const [id, project, pool, model, age, line] of [
    [
      "cs-dashboard-v2",
      "coding-scheduler",
      "codex",
      "gpt-6.1-sol",
      32,
      "✓ 前端构建完成，正在验证静态资源",
    ],
    [
      "od-invitation-entry",
      "open-design",
      "cc",
      "claude-sonnet-5-5",
      18,
      "正在补充邀请入口的组件状态…",
    ],
    [
      "mm-billing-detail",
      "murmurant",
      "codex",
      "gpt-6.1-sol",
      9,
      "✓ 请求阶段耗时展示校验通过",
    ],
  ]) {
    const log = path.join(stateRoot, "workers", `${id}.log`);
    await writeFile(log, `${line}\n`);
    await record("running", {
      ...common,
      id,
      project,
      pool,
      model,
      status: "running",
      created_at: iso(age + 15),
      started_at: iso(age),
      log,
    });
  }
  await record("queue", {
    ...common,
    id: "cs-queue-explanations",
    status: "queued",
    created_at: iso(14),
    pin: "codex",
    queue_reasons: [
      {
        type: "lock",
        job: "cs-dashboard-v2",
        mine: "lib/dashboard.py",
        theirs: "lib/dashboard.py",
        kind: "write",
      },
      { type: "awaiting_tick" },
    ],
  });
  await record("queue", {
    ...common,
    id: "od-analytics-audit",
    project: "open-design",
    area: "research",
    status: "queued",
    created_at: iso(6),
    pin: "cc",
    queue_reasons: [
      { type: "quota_unknown", pool: "cc" },
      { type: "spill_insufficient", pool: "codex", score: 32, minimum: 45 },
    ],
  });
  const jobs = [
    ["cs-status-contract", "coding-scheduler", "codex", "done", 54, 8],
    ["od-component-review", "open-design", "cc", "done", 132, 24],
    ["mm-request-timing", "murmurant", "codex", "failed", 284, 14],
    ["cs-lock-regression", "coding-scheduler", "codex", "done", 391, 11],
    ["od-theme-alignment", "open-design", "cc", "done", 572, 31],
    ["mm-credit-audit", "murmurant", "codex", "blocked", 763, 18],
    ["cs-board-polish", "coding-scheduler", "cc", "done", 1052, 27],
    ["od-release-check", "open-design", "codex", "done", 1310, 16],
  ];
  for (const [id, project, pool, status, age, spent] of jobs)
    await record("done", {
      ...common,
      id,
      project,
      pool,
      status,
      model: pool === "codex" ? "gpt-6.1-sol" : "claude-sonnet-5-5",
      created_at: iso(age + spent + 18),
      started_at: iso(age + spent),
      finished_at: iso(age),
      exit: status === "done" ? 0 : 1,
    });
  await writeFile(
    path.join(stateRoot, "logs/tick.log"),
    '{"started": [], "queued": []}\n',
  );
  server = spawn(
    "/usr/bin/python3",
    [path.join(repo, "bin/cs"), "dashboard", "--port", "0"],
    {
      cwd: repo,
      env: { ...process.env, CS_HOME: stateRoot },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  let stderr = "";
  server.stderr.on("data", (data) => {
    stderr += data;
  });
  const url = await new Promise((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error(`Server readiness timeout: ${stderr}`)),
      15000,
    );
    server.once("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    server.once("exit", (code) => {
      clearTimeout(timer);
      reject(new Error(`Server exited ${code}: ${stderr}`));
    });
    server.stdout.on("data", (data) => {
      const match = data.toString().match(/http:\/\/127\.0\.0\.1:\d+/);
      if (match) {
        clearTimeout(timer);
        resolve(match[0]);
      }
    });
  });
  for (const endpoint of ["/", "/api/state"]) {
    const code = execFileSync(
      "/usr/bin/curl",
      [
        "--silent",
        "--show-error",
        "--output",
        "/dev/null",
        "--write-out",
        "%{http_code}",
        `${url}${endpoint}`,
      ],
      { encoding: "utf8" },
    );
    assert.equal(code, "200");
    console.log(`curl ${endpoint}: ${code}`);
  }
  browser = await chromium.launch({ channel: "chrome", headless: true });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
    colorScheme: "light",
    reducedMotion: "reduce",
  });
  const page = await context.newPage();
  page.on("pageerror", (e) => diagnostics.push(e.message));
  page.on("console", (message) => {
    if (message.type() === "error") diagnostics.push(message.text());
  });
  page.on("dialog", (dialog) => {
    diagnostics.push(`Unexpected dialog: ${dialog.type()}`);
    void dialog.dismiss();
  });
  await page.goto(url);
  await page.getByRole("heading", { name: "cs-dashboard-v2" }).waitFor();
  assert.equal(await page.locator(".running-card").count(), 3);
  assert.equal(await page.locator(".timeline-job").count(), 13);
  assert.equal(await page.locator(".duration-summary rect").count(), 8);
  assert.match(await page.locator(".delivery-count").textContent(), /8/);
  assert.equal(
    await page
      .locator('[data-slot="table-body"] [data-slot="table-row"]')
      .count(),
    8,
  );
  const timerBefore = await page.locator(".job-timer").first().textContent();
  await page.waitForFunction(
    (previous) =>
      document.querySelector(".job-timer")?.textContent !== previous,
    timerBefore,
  );
  await page.getByRole("button", { name: "文件锁冲突，查看原因" }).hover();
  await page.getByRole("tooltip").waitFor();
  assert.match(
    await page.getByRole("tooltip").textContent(),
    /cs-dashboard-v2/,
  );
  assert.match(
    await page.getByRole("tooltip").textContent(),
    /lib\/dashboard.py/,
  );
  await page.mouse.move(0, 0);
  await page.getByRole("combobox", { name: "按项目筛选" }).click();
  await page
    .getByRole("option", { name: "coding-scheduler", exact: true })
    .click();
  assert.equal(await page.locator(".running-card").count(), 1);
  assert.equal(
    await page
      .locator('[data-slot="table-body"] [data-slot="table-row"]')
      .count(),
    3,
  );
  await page.getByRole("combobox", { name: "按项目筛选" }).click();
  await page.getByRole("option", { name: "全部项目", exact: true }).click();
  await page.getByRole("combobox", { name: "按结果筛选" }).click();
  await page.getByRole("option", { name: "失败", exact: true }).click();
  assert.equal(
    await page
      .locator('[data-slot="table-body"] [data-slot="table-row"]')
      .count(),
    1,
  );
  assert.match(
    await page.locator('[data-slot="table-body"]').textContent(),
    /mm-request-timing/,
  );
  await page.getByRole("combobox", { name: "按结果筛选" }).click();
  await page.getByRole("option", { name: "全部结果", exact: true }).click();
  await page.getByRole("textbox", { name: "搜索完成单" }).fill("cs-lock");
  assert.equal(
    await page
      .locator('[data-slot="table-body"] [data-slot="table-row"]')
      .count(),
    1,
  );
  await page.getByRole("textbox", { name: "搜索完成单" }).fill("不存在的任务");
  assert.equal(await page.getByText("没有匹配的完成记录").count(), 1);
  await page.getByRole("textbox", { name: "搜索完成单" }).fill("");
  assert.equal(
    await page.evaluate(
      () =>
        getComputedStyle(document.querySelector(".heartbeat")).animationName,
    ),
    "none",
  );
  await page.getByRole("textbox", { name: "搜索完成单" }).blur();
  await page.mouse.move(0, 0);
  await page.evaluate(() => window.scrollTo(0, 0));
  const light = path.join(homedir(), "cs-dashboard-v2-light.png");
  await page.screenshot({ path: light, fullPage: true });
  await page.getByRole("button", { name: "切换为暗色" }).click();
  await page.reload();
  await page.getByRole("heading", { name: "cs-dashboard-v2" }).waitFor();
  assert.equal(await page.locator("html").getAttribute("class"), "dark");
  assert.equal(
    await page.evaluate(() => localStorage.getItem("cs-theme")),
    "dark",
  );
  const dark = path.join(homedir(), "cs-dashboard-v2-dark.png");
  await page.mouse.move(0, 0);
  await page.screenshot({ path: dark, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
    true,
  );
  // Verify an unavailable API keeps the previous snapshot and recovers.
  await page.route("**/api/state", (route) =>
    route.fulfill({ status: 503, body: "{}" }),
  );
  await page.getByRole("alert").waitFor({ timeout: 10000 });
  assert.equal(await page.locator(".running-card").count(), 3);
  await page.unroute("**/api/state");
  await page.getByRole("alert").waitFor({ state: "hidden", timeout: 10000 });
  assert.deepEqual(
    diagnostics.filter((item) => !item.includes("503")),
    [],
  );
  console.log(
    `Browser checks passed: filters, tooltip, timer, theme persistence, reduced motion, mobile width, API recovery\n${light}\n${dark}`,
  );
  await context.close();
} finally {
  if (browser) await browser.close();
  if (server && server.exitCode === null) {
    const closed = once(server, "exit");
    server.kill("SIGTERM");
    await closed;
    console.log("Dashboard server stopped");
  }
  await rm(stateRoot, { recursive: true, force: true });
}
