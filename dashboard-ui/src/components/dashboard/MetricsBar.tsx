import { useEffect, useRef, useState } from "react";
import { animate, useReducedMotion } from "framer-motion";
import { cn } from "@/lib/utils";
import { PoolSlots } from "./PoolSlots";
import {
  POOLS,
  REASON_LABEL,
  STATUS_LABEL,
  num,
  reasonsOf,
  type Snapshot,
} from "@/lib/derive";
import type { State } from "@/types";

/** Interpolates only when the value really changes; instant on reduced motion. */
export function NumberFlow({ value }: { value: number }) {
  const reduce = useReducedMotion();
  const previous = useRef(value);
  const [display, setDisplay] = useState(value);
  useEffect(() => {
    if (previous.current === value) return;
    const controls = animate(previous.current, value, {
      duration: reduce ? 0 : 0.22,
      onUpdate: setDisplay,
    });
    previous.current = value;
    return () => controls.stop();
  }, [value, reduce]);
  return <span className="tabular-nums">{Math.round(display)}</span>;
}

export function MetricsBar({
  state,
  snap,
  poolFilter,
  onPool,
}: {
  state: State | null;
  snap: Snapshot;
  poolFilter: string;
  onPool: (pool: string) => void;
}) {
  const queued = snap.queued.length;
  // Unique affected jobs per reason type; groups overlap, so they never sum.
  const reasonJobs = new Map<string, number>();
  for (const r of snap.queued) {
    for (const t of new Set(reasonsOf(r.job).map((x) => x.type)))
      reasonJobs.set(t, (reasonJobs.get(t) || 0) + 1);
  }
  const topReasons = [...reasonJobs.entries()].sort((a, b) => b[1] - a[1]);
  const done = snap.done;
  const ok = done.filter((r) => r.kind === "done").length;
  const breakdown = ["failed", "blocked", "cancelled", "unknown"]
    .map((k) => [k, done.filter((r) => r.kind === k).length] as const)
    .filter(([, n]) => n > 0);
  return (
    <section className="metric-strip" aria-label="全局执行池与队列概览">
      {POOLS.map((pool) => {
        const used = num(state?.counts?.[pool]);
        const cap = num(state?.caps?.[pool]);
        const pct = used != null && cap != null && cap > 0 ? Math.round((used / cap) * 100) : null;
        const active = poolFilter === pool;
        return (
          <button
            key={pool}
            type="button"
            className={cn("metric metric-pool", pool, active && "active")}
            aria-pressed={active}
            onClick={() => onPool(active ? "all" : pool)}
            title={active ? "取消执行池筛选" : `只看 ${pool} 池任务`}
          >
            <div className="metric-label">
              <span>{pool.toUpperCase()} / 全局槽位</span>
              <span className={cn(pct != null && pct > 100 && "bad")}>
                {pct == null ? "—" : `${pct}%`}
              </span>
            </div>
            <div className="metric-value">
              {used == null ? "—" : <NumberFlow value={used} />}
              <small>/ {cap != null && cap > 0 ? cap : "?"} 占用</small>
              {used != null && cap != null && cap > 0 && used > cap && (
                <small className="bad">越限 +{used - cap}</small>
              )}
            </div>
            <PoolSlots pool={pool} used={used} cap={cap} />
          </button>
        );
      })}
      <div className="metric">
        <div className="metric-label">
          <span>排队 / 全局</span>
          {queued === 0 && state ? <span className="ok">无等待任务</span> : null}
        </div>
        <div className="metric-value">
          {state ? <NumberFlow value={queued} /> : "—"}
          <small>单等待</small>
        </div>
        <div className="mini-note">
          {topReasons.length
            ? topReasons
                .slice(0, 3)
                .map(([t, n]) => `${REASON_LABEL[t] || t} ${n}`)
                .join(" · ")
            : queued
              ? "暂无原因记录"
              : "空槽位不代表所有任务都可启动"}
        </div>
      </div>
      <div className="metric">
        <div className="metric-label">
          <span>近 24h / 已结束</span>
          <span>{done.length ? `${Math.round((ok / done.length) * 100)}% 成功` : "—"}</span>
        </div>
        <div className="metric-value">
          {state ? <NumberFlow value={done.length} /> : "—"}
          <small>
            单 · 成功 <b className="ok">{ok}</b>
          </small>
        </div>
        <div className="mini-note">
          {breakdown.length
            ? breakdown.map(([k, n]) => `${STATUS_LABEL[k]} ${n}`).join(" · ")
            : done.length
              ? "全部成功"
              : "窗口内无终态"}
        </div>
      </div>
    </section>
  );
}
