// Isolated browser acceptance for the v3 Command Center page.
// Serves the built lib/dashboard_dist plus a fixture API from a tiny local
// server bound to 127.0.0.1. Headless chromium only; never starts workers,
// never touches ~/.coding-scheduler or launchd.
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const dist = fileURLToPath(new URL("../../lib/dashboard_dist/", import.meta.url));
const iso = (minutes) => new Date(Date.now() - minutes * 60000).toISOString();
const base = { tier: "M", area: "frontend", cwd: "/tmp/cs-smoke", project: "coding-scheduler" };

// ---- fixture -------------------------------------------------------------
const running = [
  ["r-alpha", "codex", "gpt-6.1-sol", 30, "alpha working"],
  ["r-beta", "cc", "claude-sonnet-5-5", 18, "beta working"],
  ["r-gamma", "codex", "gpt-6.1-sol", 6, "gamma working"],
].map(([id, pool, model, age, line]) => ({
  ...base, id, pool, model, status: "running",
  created_at: iso(age + 5), started_at: iso(age), log: `/tmp/${id}/log`,
  log_last_line: line, elapsed_seconds: age * 60,
}));
const queued = [
  {
    ...base, id: "q-lock", status: "queued", pin: "codex", created_at: iso(9),
    queue_reasons: [
      { type: "lock", job: "r-alpha", mine: "lib/dashboard.py", theirs: "lib/dashboard.py", kind: "write" },
      { type: "awaiting_tick" },
    ],
    last_checked: iso(1),
  },
  {
    ...base, id: "q-blocked", status: "queued", pin: "cc", created_at: iso(4), project: "open-design",
    queue_reasons: [{ type: "pool_full", pool: "cc" }],
    blocked_by: [{ job: "r-gamma", mine: "src/App.tsx", theirs: "src/App.tsx", kind: "write" }],
    last_checked: iso(1),
  },
];
const done = [
  ["d-ok", "done", 0, 50],
  ["d-fail", "failed", 1, 90],
].map(([id, status, exit, age]) => ({
  ...base, id, status, exit, pool: "codex", model: "gpt-6.1-sol",
  created_at: iso(age + 20), started_at: iso(age + 12), finished_at: iso(age),
  elapsed_seconds: 720,
}));
const completed = done;
let statsMode = "normal";
let statsHits = 0;
const statsGroup = (total, ok, blocked, cancelled, failed = 0) => ({
  total, success_rate: total ? ok / total : null,
  distribution: { done: ok, blocked, cancelled, failed, unknown: 0 },
  elapsed_s: { avg: total ? 120 : null, p50: total ? 60 : null, p95: total ? 300 : null, samples: total },
  mean_prompt_bytes: total ? 4096 : null, cancelled_after_done: cancelled ? 1 : 0,
});
const statsBody = () => JSON.stringify({
  schema_version: "1", since: iso(120), until: iso(0),
  ...(statsMode === "empty" ? statsGroup(0, 0, 0, 0) : statsGroup(5, 2, 1, 1, 1)),
  by_pool: {
    codex: statsMode === "empty" ? statsGroup(0, 0, 0, 0) : statsGroup(2, 1, 1, 0),
    cc: statsMode === "empty" ? statsGroup(0, 0, 0, 0) : statsGroup(2, 1, 0, 1),
    opencode: statsMode === "empty" ? statsGroup(0, 0, 0, 0) : statsGroup(1, 0, 0, 0, 1),
  },
});
const stateBody = () =>
  JSON.stringify({
    schema_version: "1",
    counts: { codex: 2, cc: 1 },
    caps: { codex: 3, cc: 2 },
    reclaimed: 0,
    running, queued,
    dashboard: {
      running, queued, done, completed,
      timeline: [...running, ...queued, ...completed],
      updated_at: new Date().toISOString(),
      tick: {
        last_at: iso(0.2), time_source: "fixture", age_seconds: 12, healthy: true, error: false,
        launchd: { loaded: true, running: false, pid: null, last_exit_status: 0 },
      },
    },
  });

const logHits = new Map();
const hits = (id) => logHits.get(id) || 0;
function logBody(id) {
  const n = (logHits.get(id) || 0) + 1;
  logHits.set(id, n);
  if (id.startsWith("r-"))
    return { id, files: { log: `${id} line 1\n${id} line 2\npoll #${n}`, stderr: "warn: fixture stderr" } };
  if (id.startsWith("d-"))
    return { id, files: { log: `${id} finished`, "conclusion.md": "# 完成\nSTATUS: DONE" } };
  return null; // queued jobs have no worker directory yet
}

const MIME = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml" };
const server = createServer(async (req, res) => {
  const url = new URL(req.url, "http://127.0.0.1");
  const send = (status, body, type) => {
    res.writeHead(status, { "Content-Type": type, "Cache-Control": "no-store" });
    res.end(body);
  };
  if (req.method !== "GET") return send(405, "", "text/plain");
  if (url.pathname === "/api/state") return send(200, stateBody(), "application/json");
  if (url.pathname === "/api/stats") {
    statsHits++;
    assert.equal(url.searchParams.get("since"), "2h");
    return statsMode === "error"
      ? send(503, '{"error":"fixture unavailable"}', "application/json")
      : send(200, statsBody(), "application/json");
  }
  const m = url.pathname.match(/^\/api\/jobs\/([^/]+)\/log$/);
  if (m) {
    const payload = logBody(decodeURIComponent(m[1]));
    return payload
      ? send(200, JSON.stringify(payload), "application/json")
      : send(404, '{"error":"job not found"}', "application/json");
  }
  const rel = url.pathname === "/" ? "index.html" : url.pathname.slice(1);
  const target = path.resolve(dist, rel);
  if (!target.startsWith(dist)) return send(404, "Not found", "text/plain");
  try {
    send(200, await readFile(target), MIME[path.extname(target)] || "application/octet-stream");
  } catch {
    send(404, "Not found", "text/plain");
  }
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const origin = `http://127.0.0.1:${server.address().port}`;

// ---- checks --------------------------------------------------------------
const diagnostics = [];
const steps = [];
const step = (name) => steps.push(name);
let browser, page;
const row = (id) => page.getByTestId(`job-row-${id}`);
const rowHeight = async (id) => (await row(id).boundingBox()).height;
const logText = (file = "log") => page.locator(`[data-testid="log-file-${file}"] pre`).textContent();

try {
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 }, colorScheme: "light", reducedMotion: "reduce",
  });
  page = await context.newPage();
  page.on("pageerror", (e) => diagnostics.push(e.message));
  page.on("console", (m) => {
    // The fixture intentionally answers 404 for the queued job's log.
    if (m.type() === "error" && !/404|503/.test(m.text())) diagnostics.push(m.text());
  });
  page.on("dialog", (d) => {
    diagnostics.push(`Unexpected dialog: ${d.type()}`);
    void d.dismiss();
  });

  await page.goto(origin);
  await page.getByTestId("job-table").waitFor();
  await row("r-alpha").waitFor();

  // metrics bar + pool slots
  const metrics = page.getByTestId("metrics-bar");
  await metrics.waitFor();
  const codex = page.getByTestId("pool-slots-codex");
  assert.equal(await codex.getAttribute("data-used"), "2");
  assert.equal(await codex.getAttribute("data-cap"), "3");
  assert.equal(await codex.getByTestId("pool-slot").count(), 3);
  assert.equal(await codex.locator(".slot.used").count(), 2);
  const cc = page.getByTestId("pool-slots-cc");
  assert.equal(await cc.getAttribute("data-used"), "1");
  assert.equal(await cc.getByTestId("pool-slot").count(), 2);
  assert.match(await page.getByTestId("metric-queued").textContent(), /2/);
  assert.match(await page.getByTestId("metric-queued").textContent(), /文件锁冲突/);
  assert.match(await page.getByTestId("metric-done").textContent(), /2/);
  step("metrics bar + pool slots");

  const stats = page.getByTestId("stats-panel");
  await stats.waitFor();
  await page.waitForFunction(() => document.querySelector('[data-testid="stats-success-rate"]')?.textContent === "40.0%");
  assert.match(await stats.getByTestId("stats-distribution").textContent(), /成功 2 · 受阻 1 · 取消 1 · 失败 1 · 未知 0/);
  assert.match(await stats.getByTestId("stats-latency").textContent(), /P50.*P95/);
  assert.match(await stats.getByTestId("stats-pool-codex").textContent(), /50.0%/);
  assert.match(await stats.getByTestId("stats-pool-cc").textContent(), /50.0%/);
  assert.match(await stats.getByTestId("stats-pool-opencode").textContent(), /0.0%/);
  statsMode = "error";
  await page.waitForFunction(() => document.querySelector('[data-testid="stats-health"]')?.textContent.includes("显示旧数据"));
  assert.equal(await stats.getByTestId("stats-success-rate").textContent(), "40.0%");
  statsMode = "empty";
  await page.waitForFunction(() => document.querySelector('[data-testid="stats-panel"]')?.textContent.includes("窗口内无终态"));
  assert.equal(await stats.getByTestId("stats-success-rate").textContent(), "—");
  statsMode = "normal";
  await page.waitForFunction(() => document.querySelector('[data-testid="stats-success-rate"]')?.textContent === "40.0%");
  assert.ok(statsHits >= 4, "stats endpoint is polled and recovers independently");
  step("2h stats: distribution, pools, polling, stale error, empty, recovery");

  // job table: running + queued, default selection = oldest running job
  assert.equal(await page.locator('[data-testid^="job-row-"]').count(), 5);
  await page.locator('[data-testid="job-inspector"][data-job-id="r-alpha"]').waitFor();
  assert.equal(await row("r-alpha").getAttribute("aria-selected"), "true");
  assert.equal(await row("q-lock").getAttribute("data-kind"), "queued");
  step("job table + default selection");

  // queue reasons: table chips + inspector cards (queue_reasons and blocked_by)
  const lockChips = row("q-lock").getByTestId("reason-chip");
  assert.equal(await lockChips.count(), 2);
  assert.match(await row("q-lock").textContent(), /← r-alpha/);
  assert.match(await row("q-blocked").textContent(), /← r-gamma/);
  await row("q-lock").click();
  const inspector = page.locator('[data-testid="job-inspector"][data-job-id="q-lock"]');
  await inspector.waitFor();
  assert.equal(await inspector.getByTestId("inspector-tab-reasons").getAttribute("aria-selected"), "true");
  assert.equal(await inspector.locator('[data-testid="reason-card"][data-reason="lock"]').count(), 1);
  assert.equal(await inspector.locator('[data-testid="reason-card"][data-reason="awaiting_tick"]').count(), 1);
  assert.match(await inspector.getByTestId("reason-list").textContent(), /r-alpha/);
  await row("q-blocked").click();
  const blocked = page.locator('[data-testid="job-inspector"][data-job-id="q-blocked"]');
  await blocked.waitFor();
  assert.equal(await blocked.locator('[data-testid="reason-card"][data-reason="lock"]').count(), 1);
  assert.equal(await blocked.locator('[data-testid="reason-card"][data-reason="pool_full"]').count(), 1);
  // log viewer on a queued job: 404 is explained, not fatal
  await blocked.getByTestId("inspector-tab-log").click();
  await page.getByTestId("log-error").waitFor();
  step("queue reasons (chips, cards, blocked_by)");

  // log viewer: content + 3s polling while running, none for finished jobs
  await row("r-alpha").click();
  const alpha = page.locator('[data-testid="job-inspector"][data-job-id="r-alpha"]');
  await alpha.getByTestId("inspector-tab-log").click();
  await page.getByTestId("log-file-log").waitFor();
  assert.match(await logText(), /r-alpha line 2/);
  assert.match(await logText("stderr"), /fixture stderr/);
  const first = Number((await logText()).match(/poll #(\d+)/)[1]);
  await page.waitForFunction(
    (n) => {
      const m = document.querySelector('[data-testid="log-file-log"] pre')?.textContent.match(/poll #(\d+)/);
      return m && Number(m[1]) > n;
    },
    first,
    { timeout: 8000 },
  );
  await page.getByRole("tab", { name: /已结束/ }).click();
  assert.equal(await page.locator('[data-testid^="job-row-"]').count(), 2);
  await row("d-ok").click();
  const finished = page.locator('[data-testid="job-inspector"][data-job-id="d-ok"]');
  await finished.getByTestId("inspector-tab-log").click();
  await page.getByTestId("log-file-conclusion.md").waitFor();
  assert.match(await logText("conclusion.md"), /STATUS: DONE/);
  const before = hits("d-ok");
  await page.waitForTimeout(3600);
  assert.equal(hits("d-ok"), before, "finished job log must not be polled");
  step("log viewer (content, polling, no poll when finished)");

  await page.getByRole("tab", { name: /进行中/ }).click();
  await row("r-alpha").waitFor();

  // timeline tab switch
  await page.getByTestId("view-timeline").click();
  await page.getByTestId("timeline-tab").waitFor();
  assert.equal(await page.getByTestId("view-timeline").getAttribute("aria-pressed"), "true");
  await page.getByTestId("tl-row-r-alpha").waitFor();
  assert.equal(await page.getByTestId("job-table").count(), 0);
  await page.getByTestId("view-work").click();
  await page.getByTestId("job-table").waitFor();
  assert.equal(await page.getByTestId("timeline-tab").count(), 0);
  step("timeline tab switch");

  // density toggle
  const shell = page.locator(".shell");
  assert.equal(await shell.getAttribute("data-density"), "comfortable");
  const tall = await rowHeight("r-alpha");
  await page.getByTestId("density-toggle").click();
  assert.equal(await shell.getAttribute("data-density"), "compact");
  assert.equal(await page.evaluate(() => localStorage.getItem("cs-density")), "compact");
  assert.ok((await rowHeight("r-alpha")) < tall, "compact rows must be shorter");
  await page.getByTestId("density-toggle").click();
  assert.equal(await shell.getAttribute("data-density"), "comfortable");
  step("density toggle");

  // narrow viewport: inspector becomes a drawer opened on row select
  await page.setViewportSize({ width: 800, height: 900 });
  await page.getByTestId("job-table").waitFor();
  await page.getByTestId("job-inspector").waitFor({ state: "detached" }); // no inline inspector below 900px
  assert.equal(await page.getByTestId("inspector-drawer").count(), 0, "drawer closed until a row is selected");
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  assert.ok((await page.getByTestId("stats-panel").boundingBox()).width <= 800);
  await row("q-lock").click();
  const drawer = page.getByTestId("inspector-drawer");
  await drawer.waitFor();
  const drawerInspector = drawer.locator('[data-testid="job-inspector"][data-job-id="q-lock"]');
  await drawerInspector.waitFor();
  assert.equal(await drawer.getByTestId("reason-card").count() >= 2, true);
  const box = await drawer.boundingBox();
  assert.ok(box.width <= 800 && box.x + box.width <= 801, "drawer fits the viewport");
  await page.keyboard.press("Escape");
  await drawer.waitFor({ state: "hidden" });
  await row("r-beta").click();
  await drawer.waitFor();
  await drawer.getByTestId("inspector-tab-log").click();
  await drawer.getByTestId("log-file-log").waitFor();
  assert.match(await drawer.locator('[data-testid="log-file-log"] pre').textContent(), /r-beta line 1/);
  await drawer.getByRole("button", { name: "关闭详情" }).click();
  await drawer.waitFor({ state: "hidden" });
  step("narrow 800x900 drawer");

  // back to desktop: inline inspector returns, drawer is gone
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByTestId("job-inspector").waitFor();
  assert.equal(await page.getByTestId("inspector-drawer").count(), 0);
  step("desktop layout restored");

  assert.deepEqual(diagnostics, []);
  console.log(`Browser smoke passed (${origin}):\n - ${steps.join("\n - ")}`);
} catch (error) {
  process.exitCode = 1;
  console.error("Browser smoke FAILED:", error);
  console.error(`Steps passed: ${steps.join(", ") || "none"}`);
  if (diagnostics.length) console.error("Page diagnostics:", diagnostics);
  if (page) {
    const shot = path.join(tmpdir(), "cs-dashboard-smoke-failure.png");
    await page.screenshot({ path: shot }).then(() => console.error(`Screenshot: ${shot}`), () => {});
  }
} finally {
  if (browser) await browser.close();
  await new Promise((resolve) => server.close(resolve));
}
