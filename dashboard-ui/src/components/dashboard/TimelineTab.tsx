import { useMemo, useState } from "react";
import { cn } from "@/lib/utils";
import {
  STATUS_LABEL,
  clock,
  ms,
  poolName,
  projectName,
  tone,
} from "@/lib/derive";
import type { Row } from "@/types";

const LIMIT = 120;
const WINDOWS = { "60m": 3600000, "24h": 86400000 } as const;
type Win = keyof typeof WINDOWS;

/**
 * Direction B, minimal: [created, start) is waiting, [start, finish|snapshot)
 * is executing. Only recorded timestamps are drawn; no invented phases.
 */
export function TimelineTab({
  rows,
  end,
  selectedId,
  onSelect,
}: {
  rows: Row[];
  end: number;
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const [win, setWin] = useState<Win>("60m");
  const [group, setGroup] = useState<"pool" | "project">("pool");
  const span = WINDOWS[win];
  const start = end - span;
  const lanes = useMemo(() => {
    const map = new Map<string, { row: Row; c: number; s: number; f: number }[]>();
    for (const row of rows) {
      const j = row.job;
      const s = ms(j.started_at);
      const c = Number.isFinite(ms(j.created_at)) ? ms(j.created_at) : s;
      if (!Number.isFinite(c)) continue; // no valid timestamp → no bar
      const f = row.bucket === "completed" ? ms(j.finished_at) : end;
      const stop = Number.isFinite(f) ? f : end;
      if (c > end || stop < start) continue;
      const key = group === "pool" ? (j.pool ? poolName(j.pool) : "待分配") : projectName(j.project);
      if (!map.has(key)) map.set(key, []);
      map.get(key)!.push({ row, c, s, f: stop });
    }
    for (const items of map.values()) items.sort((a, b) => b.c - a.c);
    return [...map.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [rows, end, start, group]);
  const pct = (t: number) => Math.max(0, Math.min(100, ((t - start) / span) * 100));
  const step = win === "60m" ? 600000 : 3 * 3600000;
  const ticks = Array.from({ length: Math.floor(span / step) + 1 }, (_, i) => start + i * step);
  return (
    <div className="timeline" data-testid="timeline-tab">
      <div className="toolbar">
        <div className="seg" role="group" aria-label="时间窗口">
          {(Object.keys(WINDOWS) as Win[]).map((w) => (
            <button key={w} type="button" aria-pressed={win === w} onClick={() => setWin(w)}>
              近 {w}
            </button>
          ))}
        </div>
        <div className="seg" role="group" aria-label="分组">
          <button type="button" aria-pressed={group === "pool"} onClick={() => setGroup("pool")}>
            按执行池
          </button>
          <button type="button" aria-pressed={group === "project"} onClick={() => setGroup("project")}>
            按项目
          </button>
        </div>
        <span className="tl-legend">
          <i className="bar-wait" /> 启动前等待 <i className="bar-run" /> 执行
          <i className="bar-bad" /> 异常终态
        </span>
      </div>
      <div className="tl-scroll">
        <div className="tl-axis">
          <span />
          <div>
            {ticks.map((t) => (
              <span key={t} className="mono" style={{ left: `${pct(t)}%` }}>
                {clock(t)}
              </span>
            ))}
          </div>
        </div>
        {!lanes.length && <p className="empty-note pad">此窗口内没有可绘制的任务时间段。</p>}
        {lanes.map(([lane, items]) => (
          <div key={lane} className="tl-lane">
            <div className="tl-lane-head">
              <b>{lane}</b> <small className="muted">{items.length} 单</small>
            </div>
            {items.slice(0, LIMIT).map(({ row, c, s, f }) => {
              const j = row.job;
              const waitEnd = Number.isFinite(s) ? s : f;
              return (
                <button
                  key={j.id}
                  type="button"
                  data-testid={`tl-row-${j.id}`}
                  className={cn("tl-row", selectedId === j.id && "selected")}
                  onClick={() => onSelect(j.id)}
                  title={`${j.id} · ${STATUS_LABEL[row.kind]}\n创建 ${clock(j.created_at, true)} · 启动 ${clock(j.started_at)} · 结束 ${clock(j.finished_at)}`}
                >
                  <span className="tl-id mono">{j.id}</span>
                  <span className="tl-track">
                    {waitEnd > c && (
                      <i
                        className="bar-wait"
                        style={{ left: `${pct(c)}%`, width: `${Math.max(0, pct(waitEnd) - pct(c))}%` }}
                      />
                    )}
                    {Number.isFinite(s) && (
                      <i
                        className={cn(
                          tone(row.kind) === "bad" || row.kind === "blocked" ? "bar-bad" : "bar-run",
                          row.kind === "running" && "live",
                        )}
                        style={{
                          left: `${pct(s)}%`,
                          width: `max(2px, ${Math.max(0, pct(f) - pct(s))}%)`,
                        }}
                      />
                    )}
                  </span>
                </button>
              );
            })}
            {items.length > LIMIT && (
              <p className="muted pad-s">仅显示最近创建的 {LIMIT} 单，其余请在工作台筛选查看。</p>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
