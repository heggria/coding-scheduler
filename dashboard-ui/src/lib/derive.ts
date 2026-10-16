// Pure, render-free helpers: labels, formatting, indexing and aggregation.
// Everything here must tolerate old job records with missing fields.
import type { Bucket, Job, Pool, Reason, Row, State } from "@/types";

export const POOLS: Pool[] = ["codex", "cc"];
export const STATUS_LABEL: Record<string, string> = {
  running: "运行中",
  queued: "排队中",
  done: "成功",
  failed: "失败",
  blocked: "受阻",
  cancelled: "已取消",
  unknown: "结果未知",
};
export const TERMINAL = ["done", "failed", "blocked", "cancelled", "unknown"];
export const ATTENTION = ["failed", "blocked", "unknown"];
export const AREA_LABEL: Record<string, string> = {
  backend: "后端",
  frontend: "前端",
  protocol: "协议",
  test: "测试",
  research: "调研",
  review: "审查",
  "ui-copy": "界面文案",
  "ops-script": "运维脚本",
};

export const poolName = (pool?: string | null) =>
  pool === "codex" ? "codex" : pool === "cc" ? "cc" : pool || "待分配";
export const areaName = (area?: string | null) =>
  area ? AREA_LABEL[area] || area : "—";
export const projectName = (p?: string | null) => p || "未分类";
export const list = <T,>(v: T[] | null | undefined): T[] =>
  Array.isArray(v) ? v : [];
export const num = (v: unknown): number | null =>
  typeof v === "number" && Number.isFinite(v) ? v : null;

/** Epoch ms or NaN; never turns a missing value into 1970. */
export function ms(v?: string | null): number {
  if (!v) return NaN;
  const t = new Date(v).getTime();
  return Number.isFinite(t) ? t : NaN;
}
const pad = (n: number) => String(n).padStart(2, "0");
export function clock(v?: string | number | null, withDate = false): string {
  const t = typeof v === "number" ? v : ms(v);
  if (!Number.isFinite(t)) return "—";
  const d = new Date(t);
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return withDate ? `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hm}` : hm;
}
export function dur(seconds?: number | null): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  const s = Math.max(0, Math.floor(seconds));
  if (s >= 86400) return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
  if (s >= 3600) return `${Math.floor(s / 3600)}h ${pad(Math.floor((s % 3600) / 60))}m`;
  if (s >= 60) return `${Math.floor(s / 60)}m ${pad(s % 60)}s`;
  return `${s}s`;
}

export function kindOf(job: Job, bucket: Bucket): string {
  if (bucket === "running") return "running";
  if (bucket === "queued") return "queued";
  const s = job.status || "";
  return TERMINAL.includes(s) ? s : "unknown";
}
export const tone = (kind: string) =>
  kind === "running" || kind === "done"
    ? "ok"
    : kind === "queued" || kind === "blocked"
      ? "warn"
      : kind === "cancelled"
        ? "mute"
        : "bad";

export interface Snapshot {
  running: Row[];
  queued: Row[];
  completed: Row[];
  done: Row[];
  timeline: Row[];
  all: Row[];
  byId: Map<string, Row>;
  projects: string[];
  updatedAt: number;
}
/** Index every collection by id; terminal record wins, matching the server. */
export function buildSnapshot(state: State | null): Snapshot {
  const d = state?.dashboard || {};
  const valid = (j: unknown): j is Job =>
    !!j && typeof j === "object" && typeof (j as Job).id === "string";
  const rows = (jobs: Job[] | undefined, bucket: Bucket) =>
    list(jobs)
      .filter(valid)
      .map((job) => ({ job, bucket, kind: kindOf(job, bucket) }));
  const running = rows(d.running, "running");
  const queued = rows(d.queued, "queued");
  const completed = rows(d.completed, "completed");
  const byId = new Map<string, Row>();
  for (const r of [...completed, ...running, ...queued])
    if (!byId.has(r.job.id)) byId.set(r.job.id, r);
  const pick = (jobs: Job[] | undefined) =>
    list(jobs)
      .filter(valid)
      .map((j) => byId.get(j.id) || { job: j, bucket: "completed" as Bucket, kind: kindOf(j, "completed") });
  const all = [...running, ...queued, ...completed].filter(
    (r) => byId.get(r.job.id) === r,
  );
  const projects = [...new Set(all.map((r) => projectName(r.job.project)))].sort();
  return {
    running: running.filter((r) => byId.get(r.job.id) === r),
    queued: queued.filter((r) => byId.get(r.job.id) === r),
    completed,
    done: pick(d.done),
    timeline: pick(d.timeline),
    all,
    byId,
    projects,
    updatedAt: ms(d.updated_at),
  };
}

/** Signature used to flash rows whose visible state changed between polls. */
export const signature = (r: Row) =>
  `${r.kind}|${r.job.pool || ""}|${r.job.log_last_line || ""}|${list(r.job.queue_reasons).length}`;

export function haystack(r: Row): string {
  const j = r.job;
  return [j.id, j.project, j.model, j.area, j.pool, j.pin, j.cwd, j.tier,
    STATUS_LABEL[r.kind], j.reason_text, j.log_last_line]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
}

/** Seconds a running job has executed: snapshot value + time since that sync. */
export function execSeconds(r: Row, drift: number, now: number): number | null {
  const j = r.job;
  const base = num(j.elapsed_seconds);
  if (r.bucket === "running") {
    if (base != null) return base + drift;
    const s = ms(j.started_at);
    return Number.isFinite(s) ? Math.max(0, (now - s) / 1000) : null;
  }
  if (base != null) return base;
  const s = ms(j.started_at), f = ms(j.finished_at);
  return Number.isFinite(s) && Number.isFinite(f) ? (f - s) / 1000 : null;
}
/** Seconds between creation and start (or `now` while still queued). */
export function waitSeconds(r: Row, now: number): number | null {
  const c = ms(r.job.created_at);
  if (!Number.isFinite(c)) return null;
  const s = ms(r.job.started_at);
  const end = Number.isFinite(s) ? s : r.bucket === "queued" ? now : NaN;
  return Number.isFinite(end) ? Math.max(0, (end - c) / 1000) : null;
}

// ---------- queue reasons ----------
export const REASON_LABEL: Record<string, string> = {
  lock: "文件锁冲突",
  pool_full: "执行池已满",
  spill_insufficient: "副池亲和分不足",
  quota_unknown: "额度未知",
  quota_exhausted: "额度耗尽",
  awaiting_tick: "等待 tick 调度",
  launch_error: "启动失败",
};
export const reasonLabel = (r: Reason) => {
  const base = REASON_LABEL[r.type] || r.type || "未知原因";
  return r.pool && r.type !== "lock" && r.type !== "awaiting_tick"
    ? `${base} · ${r.pool}`
    : base;
};
export const reasonTone = (r: Reason) =>
  r.type === "quota_exhausted" || r.type === "launch_error"
    ? "bad"
    : r.type === "lock" || r.type === "pool_full" || r.type === "spill_insufficient"
      ? "warn"
      : "mute";
/** queue_reasons plus blocked_by locks, de-duplicated by blocker+file+kind. */
export function reasonsOf(job: Job): Reason[] {
  const out: Reason[] = [];
  const seen = new Set<string>();
  const key = (r: Reason) =>
    r.type === "lock"
      ? `lock|${r.job || ""}|${r.mine || ""}|${r.theirs || ""}|${r.kind || ""}`
      : `${r.type}|${r.pool || ""}|${r.detail || ""}`;
  const add = (r: Reason) => {
    if (!r || typeof r !== "object" || typeof r.type !== "string") return;
    const k = key(r);
    if (!seen.has(k)) {
      seen.add(k);
      out.push(r);
    }
  };
  list(job.queue_reasons).forEach(add);
  list(job.blocked_by).forEach((b) => add({ ...b, type: "lock" }));
  return out;
}
/** Unique blocking job ids, in reason order. */
export const blockersOf = (job: Job) => [
  ...new Set(reasonsOf(job).filter((r) => r.type === "lock" && r.job).map((r) => r.job as string)),
];

// ---------- heatmap ----------
export interface HeatCell {
  project: string;
  from: number;
  to: number;
  total: number;
  byStatus: Record<string, number>;
  ids: string[];
}
export interface Heat {
  projects: string[];
  cells: HeatCell[][];
  max: number;
  start: number;
  end: number;
}
/** One O(n) pass: project rows × 24 rolling one-hour buckets ending at `end`. */
export function heatmap(rows: Row[], end: number): Heat {
  const hour = 3600000;
  const start = end - 24 * hour;
  const map = new Map<string, HeatCell[]>();
  let max = 0;
  for (const r of rows) {
    const f = ms(r.job.finished_at);
    if (!Number.isFinite(f) || f < start || f >= end) continue;
    const p = projectName(r.job.project);
    let line = map.get(p);
    if (!line) {
      line = Array.from({ length: 24 }, (_, i) => ({
        project: p,
        from: start + i * hour,
        to: start + (i + 1) * hour,
        total: 0,
        byStatus: {},
        ids: [],
      }));
      map.set(p, line);
    }
    const cell = line[Math.min(23, Math.floor((f - start) / hour))];
    cell.total += 1;
    cell.byStatus[r.kind] = (cell.byStatus[r.kind] || 0) + 1;
    cell.ids.push(r.job.id);
    max = Math.max(max, cell.total);
  }
  const projects = [...map.keys()].sort();
  return { projects, cells: projects.map((p) => map.get(p)!), max, start, end };
}
export const heatLevel = (n: number, max: number) =>
  n <= 0 || max <= 0 ? 0 : Math.min(4, Math.max(1, Math.ceil((n / max) * 4)));
