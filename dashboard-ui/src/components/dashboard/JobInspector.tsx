import { useEffect, useRef, useState, type ReactNode } from "react";
import { Copy, X } from "lucide-react";
import { cn } from "@/lib/utils";
import {
  areaName,
  clock,
  dur,
  execSeconds,
  list,
  ms,
  poolName,
  projectName,
  reasonsOf,
  waitSeconds,
} from "@/lib/derive";
import { StatusBadge } from "./JobTable";
import { ReasonList } from "./ReasonList";
import type { Row } from "@/types";

type Tab = "overview" | "log" | "reasons";

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="field">
      <span>{label}</span>
      <b>{children}</b>
    </div>
  );
}
function PathLine({ value, note }: { value?: string | null; note?: string }) {
  if (!value) return <p className="muted">未记录</p>;
  return (
    <div className="path-line">
      <code title={value}>{value}</code>
      <button
        type="button"
        aria-label="复制路径"
        title="复制路径"
        onClick={() => void navigator.clipboard?.writeText(value).catch(() => undefined)}
      >
        <Copy size={11} />
      </button>
      {note && <small className="muted">{note}</small>}
    </div>
  );
}
function Files({ label, files }: { label: string; files: string[] }) {
  if (!files.length) return null;
  return (
    <section>
      <h5>
        {label} <small>{files.length}</small>
      </h5>
      <ul className="file-list">
        {files.slice(0, 12).map((f) => (
          <li key={f} className="mono" title={f}>{f}</li>
        ))}
        {files.length > 12 && <li className="muted">… 另 {files.length - 12} 个</li>}
      </ul>
    </section>
  );
}

const LOG_POLL_MS = 3000;
const LOG_TAIL = 200;
interface LogPayload {
  id: string;
  files: Record<string, string>;
}

/** Read-only log tails from /api/jobs/<id>/log; polls only while the job runs. */
function LogViewer({ id, running, fallback }: { id: string; running: boolean; fallback?: string | null }) {
  const [data, setData] = useState<LogPayload | null>(null);
  const [error, setError] = useState("");
  const [updated, setUpdated] = useState(0);
  const stick = useRef(new Map<string, boolean>());
  const panes = useRef(new Map<string, HTMLPreElement>());
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | null = null;
    setData(null);
    setError("");
    async function load() {
      controller = new AbortController();
      const timeout = setTimeout(() => controller?.abort(), 8000);
      try {
        const response = await fetch(`./api/jobs/${encodeURIComponent(id)}/log?tail=${LOG_TAIL}`, {
          signal: controller.signal,
          cache: "no-store",
        });
        if (response.status === 404) throw new Error("暂无日志目录（任务尚未启动或记录已清理）");
        if (!response.ok) throw new Error(`日志接口返回 HTTP ${response.status}`);
        const next = (await response.json()) as LogPayload;
        if (!next || typeof next.files !== "object" || next.files === null) throw new Error("日志数据格式错误");
        if (active) {
          for (const [name, pane] of panes.current)
            stick.current.set(name, pane.scrollHeight - pane.scrollTop - pane.clientHeight < 24);
          setData(next);
          setError("");
          setUpdated(Date.now());
        }
      } catch (e) {
        if (active) setError(e instanceof Error && e.name !== "AbortError" ? e.message : "日志请求超时");
      } finally {
        clearTimeout(timeout);
        if (active && running) timer = setTimeout(load, LOG_POLL_MS);
      }
    }
    void load();
    return () => {
      active = false;
      controller?.abort();
      clearTimeout(timer);
    };
  }, [id, running]);
  // Keep following the tail unless the reader scrolled up.
  useEffect(() => {
    for (const [name, pane] of panes.current)
      if (stick.current.get(name) !== false) pane.scrollTop = pane.scrollHeight;
  }, [data]);
  const names = data ? Object.keys(data.files) : [];
  return (
    <div className="log-viewer" data-testid="log-viewer">
      <h5>
        日志尾部 <small>最近 {LOG_TAIL} 行</small>
        {running && <small className="muted"> · 运行中，每 3 秒刷新</small>}
      </h5>
      {error && (
        <p className="warn" role="status" data-testid="log-error">
          {error}
        </p>
      )}
      {!data && !error && <p className="muted">正在读取日志…</p>}
      {data && !names.length && !error && <p className="muted">日志目录中没有可读取的日志文件。</p>}
      {names.map((name) => (
        <section key={name} data-testid={`log-file-${name}`}>
          <h5 className="mono">{name}</h5>
          <pre
            className="log-pre log-full"
            ref={(node) => {
              if (node) panes.current.set(name, node);
              else panes.current.delete(name);
            }}
          >
            {data!.files[name] || "（空）"}
          </pre>
        </section>
      ))}
      {fallback && (!data || !names.length) && (
        <>
          <h5>最后一行</h5>
          <pre className="log-pre">{fallback}</pre>
        </>
      )}
      {updated > 0 && <p className="muted small">读取于 {clock(updated)}</p>}
    </div>
  );
}

export function JobInspector({
  row,
  missingId,
  now,
  drift,
  exists,
  onJump,
  onClose,
  counts,
}: {
  row: Row | null;
  missingId: string | null;
  now: number;
  drift: number;
  exists: (id: string) => boolean;
  onJump: (id: string) => void;
  onClose: () => void;
  counts?: Partial<Record<string, number>>;
}) {
  const [tab, setTab] = useState<Tab>(row?.bucket === "queued" ? "reasons" : "overview");
  if (!row)
    return (
      <aside className="inspector panel" aria-label="任务详情" data-testid="job-inspector">
        <div className="inspector-head">
          <div className="kicker-row">
            <span className="kicker">JOB INSPECTOR</span>
            {missingId && (
              <button type="button" className="icon-btn" aria-label="关闭详情" onClick={onClose}>
                <X size={14} />
              </button>
            )}
          </div>
          {missingId ? (
            <>
              <h3 className="mono">{missingId}</h3>
              <p className="warn">记录不可用：当前快照中未找到此任务（可能已被清理或尚未写入）。</p>
            </>
          ) : (
            <p className="muted">选择左侧任务查看详情 · ↑↓ 切换 · / 搜索</p>
          )}
        </div>
      </aside>
    );
  const j = row.job;
  const reasons = reasonsOf(j);
  const created = ms(j.created_at), started = ms(j.started_at), finished = ms(j.finished_at);
  const stages = [
    Number.isFinite(created),
    Number.isFinite(started),
    row.bucket === "completed" && Number.isFinite(finished),
  ];
  const tabs: [Tab, string][] = [
    ["overview", "概览"],
    ["log", "日志"],
    ["reasons", reasons.length ? `排队原因 ${reasons.length}` : "排队原因"],
  ];
  return (
    <aside
      className={cn("inspector panel", `t-${row.kind}`)}
      aria-label={`任务详情 ${j.id}`}
      data-testid="job-inspector"
      data-job-id={j.id}
    >
      <div className="inspector-head">
        <div className="kicker-row">
          <span className="kicker">JOB INSPECTOR</span>
          <span className="kicker">SNAPSHOT</span>
          <button type="button" className="icon-btn" aria-label="关闭详情" onClick={onClose}>
            <X size={14} />
          </button>
        </div>
        <StatusBadge kind={row.kind} />
        <h3 className="mono" title={j.id}>{j.id}</h3>
        <p className="muted">
          {projectName(j.project)} / {j.area || "—"} · {j.pool ? poolName(j.pool) : "待分配"}
        </p>
      </div>
      <div className="inspector-tabs" role="tablist">
        {tabs.map(([id, label]) => (
          <button
            key={id}
            type="button"
            role="tab"
            data-testid={`inspector-tab-${id}`}
            aria-selected={tab === id}
            className={cn(tab === id && "active")}
            onClick={() => setTab(id)}
          >
            {label}
          </button>
        ))}
      </div>
      <div className="inspector-body">
        {tab === "overview" && (
          <>
            <Field label="执行模型">{j.model || "未记录"}</Field>
            <Field label="执行池 / 偏好">
              {j.pool ? poolName(j.pool) : "待分配"} / {j.pin || "—"}
            </Field>
            <Field label="区域 / 档位">
              {areaName(j.area)} / {j.tier || "—"}
            </Field>
            <Field label="创建 → 启动">
              {clock(j.created_at, true)} → {clock(j.started_at)}
            </Field>
            <Field label="结束">{clock(j.finished_at, true)}</Field>
            <Field label="等待耗时">{dur(waitSeconds(row, now))}</Field>
            <Field label="运行耗时">
              {dur(execSeconds(row, drift, now))}
              {row.bucket === "running" ? " (估算)" : ""}
            </Field>
            <Field label="exit / 只读">
              {j.exit ?? "—"} / {j.read_only == null ? "—" : j.read_only ? "是" : "否"}
            </Field>
            <div className="stages" aria-label="生命周期阶段">
              {stages.map((on, i) => (
                <i key={i} className={cn(on && "on", on && i === 2 && `end-${row.kind}`)} />
              ))}
            </div>
            <p className="stage-note">创建 → 启动 → 终态（仅按已记录时间戳点亮）</p>
            <section>
              <h5>WORKTREE</h5>
              <PathLine value={j.cwd} />
            </section>
            <Files label="FILES" files={list(j.files)} />
            <Files label="SHARED" files={list(j.shared_files)} />
            <section>
              <h5>ARTIFACTS</h5>
              <PathLine value={j.log} />
              <PathLine value={j.conclusion} note="正文尚未提供" />
            </section>
            <section>
              <h5>交付证据</h5>
              <p className="muted small">
                状态只反映 worker 结论；commit / PR / 合并 / 部署 证据未提供。
              </p>
            </section>
          </>
        )}
        {tab === "log" && (
          <>
            <LogViewer id={j.id} running={row.kind === "running"} fallback={j.log_last_line} />
            <h5>日志路径</h5>
            <PathLine value={j.log} />
          </>
        )}
        {tab === "reasons" &&
          (row.bucket === "queued" || reasons.length ? (
            <ReasonList job={j} exists={exists} onJump={onJump} counts={counts} />
          ) : (
            <p className="muted">此任务不在队列中，没有排队原因。</p>
          ))}
      </div>
      <footer className="inspector-foot">只读快照 · 日志经只读接口读取（尾部，非完整文件）</footer>
    </aside>
  );
}
