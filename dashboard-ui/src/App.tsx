import { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  ChartGantt,
  LayoutGrid,
  Moon,
  Rows3,
  Rows4,
  Search,
  Sun,
  TriangleAlert,
  X,
} from "lucide-react";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { cn } from "@/lib/utils";
import {
  ATTENTION,
  buildSnapshot,
  clock,
  dur,
  haystack,
  heatmap,
  ms,
  num,
  projectName,
  signature,
  type HeatCell,
} from "@/lib/derive";
import { MetricsBar } from "@/components/dashboard/MetricsBar";
import { JobTable } from "@/components/dashboard/JobTable";
import { JobInspector } from "@/components/dashboard/JobInspector";
import { ActivityHeatmap } from "@/components/dashboard/ActivityHeatmap";
import { TimelineTab } from "@/components/dashboard/TimelineTab";
import type { Row, State } from "./types";

const POLL_MS = 4000;
const HIDDEN_POLL_MS = 15000;
const REQUEST_TIMEOUT_MS = 10000;
const TICK_STALE_S = 180;
const PAGE = 50;

type View = "work" | "timeline";
type Tab = "active" | "ended" | "attention" | "all";
const TABS: [Tab, string][] = [
  ["active", "进行中"],
  ["ended", "已结束"],
  ["attention", "历史需关注"],
  ["all", "全部"],
];

function readParams() {
  const p = new URLSearchParams(window.location.search);
  const pick = <T extends string>(key: string, allowed: readonly T[], fallback: T): T => {
    const v = p.get(key) as T | null;
    return v && allowed.includes(v) ? v : fallback;
  };
  return {
    view: pick<View>("view", ["work", "timeline"], "work"),
    tab: pick<Tab>("tab", ["active", "ended", "attention", "all"], "active"),
    project: p.get("project") || "all",
    pool: pick("pool", ["all", "codex", "cc"], "all"),
    q: p.get("q") || "",
    job: p.get("job"),
  };
}
function stored(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}
function store(key: string, value: string) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* private mode */
  }
}

export default function App() {
  const initial = useMemo(readParams, []);
  const [state, setState] = useState<State | null>(null);
  const [error, setError] = useState("");
  const [failures, setFailures] = useState(0);
  const [fetchedAt, setFetchedAt] = useState(0);
  const [now, setNow] = useState(Date.now());
  const [view, setView] = useState<View>(initial.view);
  const [tab, setTab] = useState<Tab>(initial.tab);
  const [project, setProject] = useState(initial.project);
  const [pool, setPool] = useState<string>(initial.pool);
  const [q, setQ] = useState(initial.q);
  const [selectedId, setSelectedId] = useState<string | null>(initial.job);
  const [closed, setClosed] = useState(false);
  const [page, setPage] = useState(0);
  const [heatPick, setHeatPick] = useState<HeatCell | null>(null);
  const [flash, setFlash] = useState<{ ids: Set<string>; phase: number }>({ ids: new Set(), phase: 0 });
  const [density, setDensity] = useState(() =>
    stored("cs-density") === "compact" ? "compact" : "comfortable",
  );
  const [dark, setDark] = useState(() => stored("cs-theme") !== "light");
  const searchRef = useRef<HTMLInputElement>(null);
  const sigRef = useRef<Map<string, string> | null>(null);

  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    store("cs-theme", dark ? "dark" : "light");
  }, [dark]);
  useEffect(() => store("cs-density", density), [density]);

  // Polling: next request only after the previous one settles (never overlaps).
  useEffect(() => {
    let active = true;
    let inFlight = false;
    let errors = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | null = null;
    const schedule = () => {
      clearTimeout(timer);
      const base = document.hidden ? HIDDEN_POLL_MS : POLL_MS;
      const delay = errors ? Math.min(30000, POLL_MS * 2 ** (errors - 1)) : base;
      timer = setTimeout(refresh, delay);
    };
    async function refresh() {
      if (inFlight || !active) return;
      inFlight = true;
      controller = new AbortController();
      const timeout = setTimeout(() => controller?.abort(), REQUEST_TIMEOUT_MS);
      try {
        const response = await fetch("./api/state", { signal: controller.signal, cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const next = (await response.json()) as State;
        if (!next || typeof next !== "object") throw new Error("bad payload");
        if (active) {
          errors = 0;
          setState(next);
          setFetchedAt(Date.now());
          setFailures(0);
          setError("");
        }
      } catch (e) {
        if (active) {
          errors += 1;
          setFailures(errors);
          setError(e instanceof Error && e.name === "AbortError" ? "请求超时" : "状态接口不可用");
        }
      } finally {
        clearTimeout(timeout);
        inFlight = false;
        if (active) schedule();
      }
    }
    const onVisible = () => {
      if (!document.hidden && !inFlight) {
        clearTimeout(timer);
        void refresh();
      }
    };
    document.addEventListener("visibilitychange", onVisible);
    void refresh();
    const clockTimer = setInterval(() => setNow(Date.now()), 1000);
    return () => {
      active = false;
      controller?.abort();
      clearTimeout(timer);
      clearInterval(clockTimer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);

  const snap = useMemo(() => buildSnapshot(state), [state]);

  // Flash rows whose visible state changed since the previous successful poll.
  useEffect(() => {
    if (!state) return;
    const next = new Map<string, string>();
    for (const r of snap.byId.values()) next.set(r.job.id, signature(r));
    const prev = sigRef.current;
    sigRef.current = next;
    if (!prev) return;
    const changed = new Set<string>();
    for (const [id, sig] of next) if (prev.get(id) !== sig) changed.add(id);
    if (changed.size) setFlash((f) => ({ ids: changed, phase: f.phase + 1 }));
  }, [snap, state]);

  // Persist view/filter/selection in the URL (replaceState: no history spam).
  useEffect(() => {
    const p = new URLSearchParams();
    if (view !== "work") p.set("view", view);
    if (tab !== "active") p.set("tab", tab);
    if (project !== "all") p.set("project", project);
    if (pool !== "all") p.set("pool", pool);
    if (q) p.set("q", q);
    if (selectedId) p.set("job", selectedId);
    const search = p.toString();
    window.history.replaceState(null, "", search ? `?${search}` : window.location.pathname);
  }, [view, tab, project, pool, q, selectedId]);

  useEffect(() => setPage(0), [tab, project, pool, q, heatPick]);

  // Keyboard: "/" focuses search, Esc leaves the input.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const typing = e.target instanceof HTMLElement && /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);
      if (e.key === "/" && !typing) {
        e.preventDefault();
        searchRef.current?.focus();
      } else if (e.key === "Escape" && typing) {
        (e.target as HTMLElement).blur();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const drift = fetchedAt ? Math.max(0, (now - fetchedAt) / 1000) : 0;
  const snapEnd = Number.isFinite(snap.updatedAt) ? snap.updatedAt : now;
  // Estimated "now" on the snapshot clock: snapshot time + time since that sync.
  const liveNow = Number.isFinite(snap.updatedAt) ? snap.updatedAt + drift * 1000 : now;

  const byStart = (a: Row, b: Row) => (ms(a.job.started_at) || 0) - (ms(b.job.started_at) || 0);
  const byCreate = (a: Row, b: Row) => (ms(a.job.created_at) || 0) - (ms(b.job.created_at) || 0);
  const active = useMemo(
    () => [...[...snap.running].sort(byStart), ...[...snap.queued].sort(byCreate)],
    [snap],
  );
  const ended = snap.completed.filter((r) => snap.byId.get(r.job.id) === r);
  const attention = ended.filter((r) => ATTENTION.includes(r.kind));
  const tabRows: Record<Tab, Row[]> = { active, ended, attention, all: [...active, ...ended] };

  const needle = q.trim().toLowerCase();
  const filtered = tabRows[tab].filter((r) => {
    if (project !== "all" && projectName(r.job.project) !== project) return false;
    if (pool !== "all" && (r.job.pool || r.job.pin) !== pool) return false;
    if (needle && !haystack(r).includes(needle)) return false;
    if (heatPick && tab !== "active") {
      const f = ms(r.job.finished_at);
      if (projectName(r.job.project) !== heatPick.project || !(f >= heatPick.from && f < heatPick.to))
        return false;
    }
    return true;
  });
  const pages = Math.max(1, Math.ceil(filtered.length / PAGE));
  const current = Math.min(page, pages - 1);
  const visible = filtered.slice(current * PAGE, (current + 1) * PAGE);
  const heat = useMemo(() => heatmap(snap.completed, snapEnd), [snap, snapEnd]);

  // Default selection: first active row, once, unless the user closed it.
  useEffect(() => {
    if (state && !selectedId && !closed) {
      const first = active[0] || ended[0];
      if (first) setSelectedId(first.job.id);
    }
  }, [state, selectedId, closed, active, ended]);

  const selectedRow = selectedId ? snap.byId.get(selectedId) || null : null;
  const select = (id: string) => {
    setClosed(false);
    setSelectedId(id);
  };
  const jump = (id: string) => {
    select(id);
    const row = snap.byId.get(id);
    if (row && !tabRows[tab].includes(row)) setTab(row.bucket === "completed" ? "all" : "active");
  };
  const filtersOn = project !== "all" || pool !== "all" || !!needle || !!heatPick;
  const clearFilters = () => {
    setProject("all");
    setPool("all");
    setQ("");
    setHeatPick(null);
  };
  const pickHeat = (cell: HeatCell | null) => {
    setHeatPick(cell);
    if (cell) {
      setView("work");
      if (tab === "active") setTab("ended");
    }
  };

  // Scheduler health (tick) and API health are shown independently.
  const tick = state?.dashboard?.tick || null;
  const loaded = tick?.launchd?.loaded;
  const baseAge = num(tick?.age_seconds);
  const lastTick = ms(tick?.last_at);
  const tickAge =
    baseAge != null ? baseAge + drift : Number.isFinite(lastTick) ? Math.max(0, (now - lastTick) / 1000) : null;
  const tickHealthy = loaded === true && tick?.error === false && tickAge != null && tickAge <= TICK_STALE_S;
  const [tickTone, tickLabel] = !state
    ? ["mute", "等待快照"]
    : loaded === false
      ? ["bad", "调度未加载"]
      : tickAge == null
        ? ["mute", "等待首次 tick"]
        : tickAge > TICK_STALE_S
          ? ["bad", `tick 超时 · ${dur(tickAge)}`]
          : tick?.error === true
            ? ["bad", "上次 tick 异常"]
            : loaded == null || tick?.error == null
              ? ["warn", `tick 状态未知 · ${dur(tickAge)}`]
              : ["ok", `tick 正常 · ${dur(tickAge)}`];
  const stale = !!error && !!state;
  const breathing = tickHealthy && !error;

  const emptyNode = !state ? (
    <div className="empty-note">{error ? `${error}，正在重试…` : "正在读取快照…"}</div>
  ) : filtersOn && tabRows[tab].length ? (
    <div className="empty-note">
      没有匹配的任务。
      <button type="button" className="link" onClick={clearFilters}>
        清空筛选
      </button>
    </div>
  ) : tab === "active" ? (
    <div className="empty-note">当前没有运行或排队任务（队列为空不等于整体健康）。</div>
  ) : (
    <div className="empty-note">当前记录中没有此类任务。</div>
  );

  return (
    <div className={cn("shell", density === "compact" && "compact")}>
      <nav className="rail" aria-label="视图">
        <span className="logo">cs</span>
        <button
          type="button"
          className={cn(view === "work" && "active")}
          aria-label="工作台"
          aria-pressed={view === "work"}
          onClick={() => setView("work")}
        >
          <LayoutGrid size={17} />
        </button>
        <button
          type="button"
          className={cn(view === "timeline" && "active")}
          aria-label="时间轴"
          aria-pressed={view === "timeline"}
          onClick={() => setView("timeline")}
        >
          <ChartGantt size={17} />
        </button>
        <span className="rail-bottom">LOCAL / SNAPSHOT</span>
      </nav>
      <div className="main">
        <header className="topbar">
          <div className="crumb">
            <strong>coding scheduler</strong>
            <span className="slash">/</span>
            <span>本机工作台</span>
            <span className="badge">只读</span>
          </div>
          <div className="statusline">
            <span className={cn("api", stale ? "bad" : state ? "muted" : "mute")}>
              {stale ? "旧快照 · 接口断开" : state ? "接口正常" : "连接中…"}
            </span>
            <span className={cn("tick", tickTone)} title={`tick 时间源：${tick?.time_source || "未知"}；last_at 来自日志 mtime，不是任务活性证明`}>
              <i className={cn("dot", breathing && "breathing")} />
              {tickLabel}
            </span>
            <span className="mono muted" title="快照时间">
              {Number.isFinite(snap.updatedAt) ? clock(snap.updatedAt) : "—"}
            </span>
            <button
              type="button"
              className="icon-btn"
              aria-label={density === "compact" ? "切换为舒适密度" : "切换为紧凑密度"}
              title={density === "compact" ? "舒适密度" : "紧凑密度"}
              onClick={() => setDensity(density === "compact" ? "comfortable" : "compact")}
            >
              {density === "compact" ? <Rows3 size={15} /> : <Rows4 size={15} />}
            </button>
            <button
              type="button"
              className="icon-btn"
              aria-label={dark ? "切换为亮色" : "切换为暗色"}
              onClick={() => setDark(!dark)}
            >
              {dark ? <Sun size={15} /> : <Moon size={15} />}
            </button>
          </div>
        </header>
        <main className="body">
          <div className="heading">
            <div>
              <div className="kicker">COMMAND CENTER</div>
              <h1>每一单的现场，一眼可见。</h1>
            </div>
            <div className="seg" role="group" aria-label="视图切换">
              <button type="button" aria-pressed={view === "work"} onClick={() => setView("work")}>
                工作台
              </button>
              <button type="button" aria-pressed={view === "timeline"} onClick={() => setView("timeline")}>
                执行分析
              </button>
            </div>
          </div>
          {stale && (
            <div role="alert" className="banner bad">
              <TriangleAlert size={14} />
              连接中断（{error}，已连续 {failures} 次）· 显示 {clock(fetchedAt)} 的旧快照，计时为估算，正在退避重试
            </div>
          )}
          {state && tickTone === "bad" && (
            <div role="status" className="banner warn">
              <TriangleAlert size={14} />
              调度健康异常：{tickLabel}（接口本身{stale ? "也已断开" : "正常"}）
            </div>
          )}
          <MetricsBar state={state} snap={snap} poolFilter={pool} onPool={setPool} />
          <div className="workgrid">
            <div className="workcol">
              <section className="panel explorer">
                <div className="panel-head">
                  {view === "work" ? (
                    <div className="tabs" role="tablist">
                      {TABS.map(([id, label]) => (
                        <button
                          key={id}
                          type="button"
                          role="tab"
                          aria-selected={tab === id}
                          className={cn(tab === id && "active")}
                          onClick={() => setTab(id)}
                        >
                          {label} <small>{state ? tabRows[id].length : "—"}</small>
                        </button>
                      ))}
                    </div>
                  ) : (
                    <h2>执行分析 · 等待与执行时间轴</h2>
                  )}
                  <small className="mono">{view === "work" ? "JOB EXPLORER" : "TIMELINE"}</small>
                </div>
                {view === "work" ? (
                  <>
                    <div className="toolbar">
                      <div className="search">
                        <Search size={13} aria-hidden />
                        <Input
                          ref={searchRef}
                          aria-label="搜索任务"
                          placeholder="搜索任务、项目、模型、区域…（/）"
                          value={q}
                          onChange={(e) => setQ(e.target.value)}
                        />
                      </div>
                      <Select value={project} onValueChange={setProject}>
                        <SelectTrigger aria-label="按项目筛选" className="filter-select">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="all">全部项目</SelectItem>
                          {snap.projects.map((p) => (
                            <SelectItem key={p} value={p}>
                              {p}
                            </SelectItem>
                          ))}
                          {project !== "all" && !snap.projects.includes(project) && (
                            <SelectItem value={project}>{project}</SelectItem>
                          )}
                        </SelectContent>
                      </Select>
                      {pool !== "all" && (
                        <button type="button" className="chip" onClick={() => setPool("all")}>
                          池 {pool} <X size={11} />
                        </button>
                      )}
                      {heatPick && tab !== "active" && (
                        <button type="button" className="chip" onClick={() => setHeatPick(null)}>
                          {heatPick.project} {clock(heatPick.from)}–{clock(heatPick.to)} <X size={11} />
                        </button>
                      )}
                      <small className="mono muted count">{filtered.length} 单</small>
                    </div>
                    <JobTable
                      rows={visible}
                      selectedId={selectedId}
                      onSelect={select}
                      now={liveNow}
                      drift={drift}
                      flash={flash.ids}
                      flashPhase={flash.phase}
                      empty={emptyNode}
                    />
                    <div className="table-foot">
                      <span className="muted">
                        容量为全局口径，不随筛选变化 · 快照 {clock(snap.updatedAt, true)}
                      </span>
                      <span className="pager">
                        <button
                          type="button"
                          aria-label="上一页"
                          disabled={current === 0}
                          onClick={() => setPage(current - 1)}
                        >
                          ←
                        </button>
                        <span className="mono">
                          {current + 1} / {pages}
                        </span>
                        <button
                          type="button"
                          aria-label="下一页"
                          disabled={current >= pages - 1}
                          onClick={() => setPage(current + 1)}
                        >
                          →
                        </button>
                      </span>
                    </div>
                  </>
                ) : (
                  <TimelineTab
                    rows={snap.timeline.filter(
                      (r) => project === "all" || projectName(r.job.project) === project,
                    )}
                    end={snapEnd}
                    selectedId={selectedId}
                    onSelect={select}
                  />
                )}
              </section>
              {view === "work" && <ActivityHeatmap heat={heat} picked={heatPick} onPick={pickHeat} />}
            </div>
            <AnimatePresence mode="wait" initial={false}>
              <motion.div
                key={selectedId || "none"}
                className="inspector-wrap"
                initial={{ opacity: 0, x: 8 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.16 }}
              >
                <JobInspector
                  row={selectedRow}
                  missingId={state && selectedId && !selectedRow ? selectedId : null}
                  now={liveNow}
                  drift={drift}
                  exists={(id) => snap.byId.has(id)}
                  onJump={jump}
                  onClose={() => {
                    setClosed(true);
                    setSelectedId(null);
                  }}
                  counts={state?.counts}
                />
              </motion.div>
            </AnimatePresence>
          </div>
        </main>
      </div>
    </div>
  );
}
