import { useState, type ReactNode } from "react";
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
      <aside className="inspector panel" aria-label="任务详情">
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
    <aside className={cn("inspector panel", `t-${row.kind}`)} aria-label={`任务详情 ${j.id}`}>
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
            <h5>最后一行</h5>
            {j.log_last_line ? (
              <pre className="log-pre">{j.log_last_line}</pre>
            ) : (
              <p className="muted">当前快照无日志正文（不能据此判断任务卡死）。</p>
            )}
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
      <footer className="inspector-foot">只读快照 · 路径仅为元数据，不可在此读取文件</footer>
    </aside>
  );
}
