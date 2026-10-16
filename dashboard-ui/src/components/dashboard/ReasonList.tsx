import { ArrowUpRight, Lock } from "lucide-react";
import { cn } from "@/lib/utils";
import { clock, reasonLabel, reasonTone, reasonsOf } from "@/lib/derive";
import type { Job, Reason } from "@/types";

/** Compact table form: first two reasons + N, lock blockers named inline. */
export function ReasonChips({ job }: { job: Job }) {
  const reasons = reasonsOf(job);
  if (!reasons.length) return <span className="muted">暂无原因记录</span>;
  const shown = reasons.slice(0, 2);
  return (
    <span className="reason-chips" title={reasons.map(describe).join("\n")}>
      {shown.map((r, i) => (
        <span key={i} className={cn("reason-chip", reasonTone(r))}>
          {r.type === "lock" && <Lock size={10} aria-hidden />}
          {reasonLabel(r)}
          {r.type === "lock" && r.job ? <b className="mono"> ← {r.job}</b> : null}
        </span>
      ))}
      {reasons.length > 2 && <span className="reason-more">+{reasons.length - 2}</span>}
    </span>
  );
}

function describe(r: Reason): string {
  if (r.type === "lock")
    return `文件锁冲突：${r.job || "?"} 占用 ${r.theirs || r.mine || "?"}${r.kind ? `（${r.kind}）` : ""}`;
  if (r.type === "spill_insufficient")
    return `${reasonLabel(r)}：${r.score ?? "?"} < ${r.minimum ?? "?"}`;
  if (r.type === "launch_error") return `启动失败：${r.detail || "无详情"}`;
  return reasonLabel(r);
}

/** Full inspector form: every reason side by side, blockers jumpable. */
export function ReasonList({
  job,
  exists,
  onJump,
  counts,
}: {
  job: Job;
  exists: (id: string) => boolean;
  onJump: (id: string) => void;
  counts?: Partial<Record<string, number>>;
}) {
  const reasons = reasonsOf(job);
  return (
    <div className="reason-list">
      {!reasons.length && (
        <p className="empty-note">暂无原因记录（不自动归因为等待 tick）。</p>
      )}
      {reasons.map((r, i) => (
        <div key={i} className={cn("reason-card", reasonTone(r))}>
          <h4>{reasonLabel(r)}</h4>
          {r.type === "lock" && (
            <dl>
              <dt>占用任务</dt>
              <dd>
                {r.job ? (
                  exists(r.job) ? (
                    <button type="button" className="link mono" onClick={() => onJump(r.job!)}>
                      {r.job} <ArrowUpRight size={11} aria-hidden />
                    </button>
                  ) : (
                    <span className="mono">
                      {r.job} <small className="muted">当前记录未找到</small>
                    </span>
                  )
                ) : (
                  "—"
                )}
              </dd>
              <dt>申请</dt>
              <dd className="mono path">{r.mine || "—"}</dd>
              <dt>占用</dt>
              <dd className="mono path">{r.theirs || r.mine || "—"}</dd>
              <dt>锁类型</dt>
              <dd className="mono">{r.kind || "—"}</dd>
              {r.repo && (
                <>
                  <dt>仓库</dt>
                  <dd className="mono path">{r.repo}</dd>
                </>
              )}
            </dl>
          )}
          {r.type === "pool_full" && (
            <dl>
              <dt>执行池</dt>
              <dd className="mono">{r.pool || "—"}</dd>
              <dt>全局占用</dt>
              <dd className="mono">
                {r.pool && counts?.[r.pool] != null ? counts[r.pool] : "—"}
                {r.free != null ? ` · 空闲 ${r.free}` : ""}
              </dd>
            </dl>
          )}
          {r.type === "spill_insufficient" && (
            <dl>
              <dt>副池</dt>
              <dd className="mono">{r.pool || "—"}</dd>
              <dt>亲和分</dt>
              <dd className="mono">
                {r.score ?? "?"} / 最低 {r.minimum ?? "?"}
              </dd>
            </dl>
          )}
          {(r.type === "quota_unknown" || r.type === "quota_exhausted") && (
            <dl>
              <dt>执行池</dt>
              <dd className="mono">{r.pool || "—"}</dd>
              <dt>说明</dt>
              <dd>{r.type === "quota_unknown" ? "额度读数缺失或无效，不等于耗尽" : "某次检查额度为 0，不代表当前余额"}</dd>
            </dl>
          )}
          {r.type === "launch_error" && (
            <p className="mono path clamp">{(r.detail || "无详情").slice(0, 400)}</p>
          )}
          {r.type === "awaiting_tick" && <p>已入队，尚待下一次 tick 评估。</p>}
        </div>
      ))}
      <p className="foot-note">
        最近检查 {clock(job.last_checked, true)} · 全部原因并列，不承诺解除时间
      </p>
    </div>
  );
}
