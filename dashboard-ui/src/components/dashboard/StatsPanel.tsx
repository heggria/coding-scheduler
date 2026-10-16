import { useEffect, useState } from "react";
import { Card } from "@/components/ui/card";
import { dur, POOLS } from "@/lib/derive";
import type { Stats } from "@/types";

const percent = (value: number | null | undefined) => value == null ? "—" : `${(value * 100).toFixed(1)}%`;
const duration = (value: number | null | undefined) => value == null ? "—" : dur(value);

export function StatsPanel() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController;
    let inFlight = false;
    async function refresh() {
      if (!active || inFlight) return;
      clearTimeout(timer);
      inFlight = true;
      controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 10000);
      try {
        const response = await fetch("./api/stats?since=2h", { signal: controller.signal, cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const next = await response.json() as Stats;
        if (next.schema_version !== "1" || !next.by_pool || !next.distribution || !next.elapsed_s)
          throw new Error("Invalid stats");
        if (active) { setStats(next); setError(false); }
      } catch {
        if (active) setError(true);
      } finally {
        clearTimeout(timeout);
        inFlight = false;
        if (active) timer = setTimeout(refresh, document.hidden ? 15000 : 4000);
      }
    }
    const visible = () => { if (!document.hidden) void refresh(); };
    document.addEventListener("visibilitychange", visible);
    void refresh();
    return () => {
      active = false;
      controller?.abort();
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", visible);
    };
  }, []);

  const dist = stats?.distribution;
  return (
    <Card className="panel stats-panel" data-testid="stats-panel" aria-label="近 2h 成功统计">
      <div className="panel-head">
        <span>执行结果 <small className="stats-muted">/ 近 2h · 全局 · 按结束时间</small></span>
        <small role="status" className={error ? "warn" : "stats-muted"} data-testid="stats-health">
          {error ? stats ? "统计更新失败 · 显示旧数据" : "统计暂不可用 · 正在重试" : stats ? `${stats.total} 单已结束` : "读取统计…"}
        </small>
      </div>
      <div className="stats-grid">
        <div className="stats-summary">
          <div className="metric-label">成功率 · done / 全部终态</div>
          <div className="metric-value ok" data-testid="stats-success-rate">{percent(stats?.success_rate)}</div>
          <div className="mini-note" data-testid="stats-distribution">
            {dist ? `成功 ${dist.done} · 受阻 ${dist.blocked} · 取消 ${dist.cancelled} · 失败 ${dist.failed} · 未知 ${dist.unknown}` : "—"}
          </div>
          <div className="mini-note">{stats?.total === 0 ? "窗口内无终态" : `取消前已 DONE ${stats?.cancelled_after_done ?? "—"} 单`}</div>
        </div>
        <div className="stats-summary" data-testid="stats-latency">
          <div className="metric-label">执行耗时 · 全部终态</div>
          <div className="stats-latency"><b>{duration(stats?.elapsed_s.avg)}</b><small>平均</small></div>
          <div className="mini-note">P50 {duration(stats?.elapsed_s.p50)} · P95 {duration(stats?.elapsed_s.p95)}</div>
          <div className="mini-note">平均 Prompt {stats?.mean_prompt_bytes == null ? "—" : `${Math.round(stats.mean_prompt_bytes).toLocaleString()} B`}</div>
        </div>
        {POOLS.map(pool => {
          const group = stats?.by_pool[pool];
          return <div className={`stats-pool ${pool}`} key={pool} data-testid={`stats-pool-${pool}`}>
            <div className="metric-label">{pool.toUpperCase()} <span>{group?.total ?? "—"} 单</span></div>
            <div className="stats-pool-rate">{percent(group?.success_rate)} <small>成功</small></div>
            <div className="stats-meter" aria-hidden="true"><i style={{ width: `${(group?.success_rate ?? 0) * 100}%` }} /></div>
            <div className="mini-note">受阻 {group?.distribution.blocked ?? "—"} · 取消 {group?.distribution.cancelled ?? "—"} · P95 {duration(group?.elapsed_s.p95)}</div>
          </div>;
        })}
      </div>
    </Card>
  );
}
