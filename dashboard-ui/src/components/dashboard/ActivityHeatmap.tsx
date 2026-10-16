import { cn } from "@/lib/utils";
import { STATUS_LABEL, clock, heatLevel, type Heat, type HeatCell } from "@/lib/derive";

const summary = (c: HeatCell) =>
  `${c.project} · ${clock(c.from)}–${clock(c.to)}\n已结束 ${c.total} 单` +
  (c.total
    ? "\n" +
      Object.entries(c.byStatus)
        .map(([k, n]) => `${STATUS_LABEL[k] || k} ${n}`)
        .join(" · ")
    : "");

/** Project × rolling 24 one-hour buckets; single indigo scale, not a status colour. */
export function ActivityHeatmap({
  heat,
  picked,
  onPick,
}: {
  heat: Heat;
  picked: { project: string; from: number } | null;
  onPick: (cell: HeatCell | null) => void;
}) {
  const ticks = Array.from({ length: 8 }, (_, i) => heat.start + i * 3 * 3600000);
  return (
    <section className="panel heat-panel" aria-label="近 24 小时完成活动">
      <div className="panel-head">
        <h2>完成活动 / 项目 × 小时</h2>
        <small>近 24h · 颜色 = 已结束单数（含失败/取消）</small>
      </div>
      {heat.projects.length === 0 ? (
        <p className="empty-note pad">近 24 小时当前记录中没有已结束任务。</p>
      ) : (
        <div className="heat-scroll">
          <div className="heat-grid">
            <span className="heat-axis mono">本地</span>
            {ticks.map((t) => (
              <span key={t} className="heat-axis mono" style={{ gridColumn: "span 3" }}>
                {clock(t)}
              </span>
            ))}
            {heat.projects.map((p, r) => (
              <div key={p} className="heat-row" role="row">
                <span className="heat-label mono" title={p}>{p}</span>
                {heat.cells[r].map((c) => {
                  const active = picked?.project === p && picked.from === c.from;
                  return (
                    <button
                      key={c.from}
                      type="button"
                      className={cn("heat-cell", `l${heatLevel(c.total, heat.max)}`, active && "active")}
                      title={summary(c)}
                      aria-label={summary(c).replace(/\n/g, "，")}
                      aria-pressed={active}
                      disabled={!c.total}
                      onClick={() => onPick(active ? null : c)}
                    />
                  );
                })}
              </div>
            ))}
          </div>
        </div>
      )}
      <div className="heat-foot">
        <span className="muted">
          {picked
            ? `已筛选 ${picked.project} · ${clock(picked.from)} 起 1 小时 → 见「已结束」`
            : "点选一小时，在任务表中查看该项目该时段的已结束单"}
        </span>
        <span className="heat-legend">
          0
          {[1, 2, 3, 4].map((l) => (
            <i key={l} className={`heat-cell l${l}`} />
          ))}
          {heat.max} 单/格
        </span>
      </div>
    </section>
  );
}
