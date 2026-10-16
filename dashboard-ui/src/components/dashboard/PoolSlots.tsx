import { cn } from "@/lib/utils";

/**
 * One cell per concurrency slot. Cells are not worker identities: they only
 * visualise used/cap. Over-cap usage is drawn as extra "over" cells, never
 * truncated; an unknown cap renders no fake grid.
 */
export function PoolSlots({
  pool,
  used,
  cap,
}: {
  pool: string;
  used: number | null;
  cap: number | null;
}) {
  if (cap == null || cap <= 0)
    return <div className="slots slots-unknown">容量未知</div>;
  const u = Math.max(0, used ?? 0);
  const total = Math.max(cap, u);
  return (
    <div
      className={cn("slots", pool)}
      role="img"
      aria-label={`${pool} 槽位 ${used ?? "未知"} / ${cap}`}
    >
      {Array.from({ length: total }, (_, i) => (
        <span
          key={i}
          className={cn("slot", i < u && "used", i >= cap && "over")}
        >
          <i />
        </span>
      ))}
    </div>
  );
}
