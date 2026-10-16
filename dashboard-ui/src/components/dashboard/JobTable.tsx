import type { KeyboardEvent, ReactNode } from "react";
import { cn } from "@/lib/utils";
import {
  STATUS_LABEL,
  areaName,
  clock,
  dur,
  execSeconds,
  poolName,
  projectName,
  tone,
  waitSeconds,
} from "@/lib/derive";
import { ReasonChips } from "./ReasonList";
import type { Row } from "@/types";

export function StatusBadge({ kind }: { kind: string }) {
  return (
    <span className={cn("status-badge", tone(kind), kind === "running" && "live")}>
      <i aria-hidden />
      {STATUS_LABEL[kind] || kind}
    </span>
  );
}

function Signal({ row }: { row: Row }) {
  const j = row.job;
  if (row.bucket === "queued") return <ReasonChips job={j} />;
  if (row.bucket === "running")
    return j.log_last_line ? (
      <span className="log-line" title={j.log_last_line}>{j.log_last_line}</span>
    ) : (
      <span className="muted">尚无日志</span>
    );
  return (
    <span className="muted">
      结束 {clock(j.finished_at, true)}
      {j.exit != null ? ` · exit ${j.exit}` : ""}
    </span>
  );
}

export function JobTable({
  rows,
  selectedId,
  onSelect,
  now,
  drift,
  flash,
  flashPhase,
  empty,
}: {
  rows: Row[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  now: number;
  drift: number;
  flash: Set<string>;
  flashPhase: number;
  empty: ReactNode;
}) {
  const move = (e: KeyboardEvent<HTMLTableRowElement>, id: string) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onSelect(id);
      return;
    }
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    const target = (
      e.key === "ArrowDown"
        ? e.currentTarget.nextElementSibling
        : e.currentTarget.previousElementSibling
    ) as HTMLTableRowElement | null;
    if (target?.dataset.id) {
      target.focus();
      onSelect(target.dataset.id);
    }
  };
  return (
    <div className="table-scroll">
      <table className="job-table">
        <colgroup>
          <col className="c-job" />
          <col className="c-project" />
          <col className="c-area" />
          <col className="c-pool" />
          <col className="c-status" />
          <col className="c-time" />
          <col className="c-signal" />
        </colgroup>
        <thead>
          <tr>
            <th>任务 / 模型</th>
            <th>项目</th>
            <th>区域</th>
            <th>执行池</th>
            <th>状态</th>
            <th>运行 / 等待</th>
            <th>原因 / 最新信号</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const j = row.job;
            const exec = execSeconds(row, drift, now);
            const wait = waitSeconds(row, now);
            return (
              <tr
                key={j.id}
                data-id={j.id}
                tabIndex={0}
                aria-selected={selectedId === j.id}
                className={cn(
                  `k-${row.kind}`,
                  selectedId === j.id && "selected",
                  flash.has(j.id) && (flashPhase % 2 ? "flash-a" : "flash-b"),
                )}
                onClick={() => onSelect(j.id)}
                onKeyDown={(e) => move(e, j.id)}
              >
                <td>
                  <span className="job-id mono" title={j.id}>{j.id}</span>
                  <span className="sub">{j.model || "未记录模型"}</span>
                </td>
                <td className="strong">{projectName(j.project)}</td>
                <td className="muted">{areaName(j.area)}</td>
                <td className="mono">
                  {j.pool ? poolName(j.pool) : <span className="muted">待分配</span>}
                  {!j.pool && j.pin ? <span className="sub">偏好 {j.pin}</span> : null}
                </td>
                <td>
                  <StatusBadge kind={row.kind} />
                </td>
                <td className="mono">
                  {row.bucket === "queued" ? (
                    <span className="warn" title="快照时间 − 创建时间">{dur(wait)}</span>
                  ) : (
                    <>
                      {dur(exec)}
                      {row.bucket === "running" && exec != null && drift > 30 ? (
                        <small className="muted"> 估算</small>
                      ) : null}
                    </>
                  )}
                </td>
                <td className="signal">
                  <Signal row={row} />
                </td>
              </tr>
            );
          })}
          {!rows.length && (
            <tr className="empty-row">
              <td colSpan={7}>{empty}</td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
