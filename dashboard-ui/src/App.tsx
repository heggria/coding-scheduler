import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  AnimatePresence,
  animate,
  motion,
  useReducedMotion,
} from "framer-motion";
import {
  Activity,
  ArrowDownRight,
  ArrowUpRight,
  Check,
  CheckCheck,
  ChevronRight,
  CircleDot,
  Clock3,
  Code2,
  Command,
  Layers3,
  ListFilter,
  Moon,
  Search,
  Sun,
  Terminal,
  Timer,
  TriangleAlert,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import type { Job, Pool, Reason, State } from "./types";

const results: Record<string, string> = {
  done: "成功",
  failed: "失败",
  blocked: "受阻",
  cancelled: "已取消",
  unknown: "结果未知",
};
const areas: Record<string, string> = {
  backend: "后端",
  frontend: "前端",
  protocol: "协议",
  test: "测试",
  research: "调研",
  review: "审查",
  "ui-copy": "界面文案",
  "ops-script": "运维脚本",
};
const poolName = (pool?: string) =>
  pool === "codex" ? "Codex" : pool === "cc" ? "CC" : "待分配";
const time = (v?: string) => (v ? new Date(v).getTime() : NaN);
const date = (v?: string) =>
  v
    ? new Date(v).toLocaleString("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      })
    : "—";
function duration(seconds?: number | null) {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  const s = Math.max(0, Math.floor(seconds));
  return s >= 3600
    ? `${Math.floor(s / 3600)}小时 ${Math.floor((s % 3600) / 60)}分`
    : s >= 60
      ? `${Math.floor(s / 60)}分 ${s % 60}秒`
      : `${s}秒`;
}
function NumberFlow({ value }: { value: number }) {
  const reduce = useReducedMotion(),
    previous = useRef(value);
  const [display, setDisplay] = useState(value);
  useEffect(() => {
    const controls = animate(previous.current, value, {
      duration: reduce ? 0 : 0.45,
      onUpdate: setDisplay,
    });
    previous.current = value;
    return () => controls.stop();
  }, [value, reduce]);
  return <span className="tabular-nums">{Math.round(display)}</span>;
}
function Hint({ children, text }: { children: ReactNode; text: ReactNode }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>{children}</TooltipTrigger>
      <TooltipContent className="max-w-96 break-words">{text}</TooltipContent>
    </Tooltip>
  );
}
function Project({ name }: { name?: string }) {
  return (
    <span className="project">
      <span className="project-mark">
        {(name || "?").slice(0, 2).toUpperCase()}
      </span>
      {name || "未分类"}
    </span>
  );
}
function FilterSelect({
  value,
  onChange,
  label,
  children,
}: {
  value: string;
  onChange: (v: string) => void;
  label: string;
  children: ReactNode;
}) {
  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger aria-label={label} className="filter-select">
        <SelectValue />
      </SelectTrigger>
      <SelectContent>{children}</SelectContent>
    </Select>
  );
}
function ReasonBadge({
  reason,
  blockers,
}: {
  reason: Reason;
  blockers?: Reason[];
}) {
  const labels: Record<string, string> = {
    lock: "文件锁冲突",
    pool_full: `${poolName(reason.pool)} 池满`,
    spill_insufficient: "副池分数不足",
    quota_unknown: "额度未知",
    quota_exhausted: "额度耗尽",
    awaiting_tick: "等待调度",
    launch_error: "启动失败",
  };
  const locks = reason.job ? [reason] : blockers || [];
  const detail = (
    <div className="space-y-1">
      <p>{labels[reason.type] || reason.type}</p>
      {locks.map((r, i) => (
        <p key={i}>
          阻挡单：{r.job || "—"}
          <br />
          申请文件：{r.mine || "—"}
          <br />
          占用文件：{r.theirs || r.mine || "—"}
        </p>
      ))}
      {reason.detail && <p>{reason.detail}</p>}
      {reason.type === "spill_insufficient" && (
        <p>
          亲和分 {reason.score} / 最低 {reason.minimum}
        </p>
      )}
      {!locks.length && <p>无文件或任务阻挡记录</p>}
    </div>
  );
  return (
    <Hint text={detail}>
      <button
        className="reason-trigger"
        aria-label={`${labels[reason.type] || reason.type}，查看原因`}
      >
        <Badge
          variant="outline"
          className={reason.type === "lock" ? "reason-lock" : "reason-other"}
        >
          {reason.type === "lock" && <Layers3 size={11} />}
          {labels[reason.type] || reason.type}
        </Badge>
      </button>
    </Hint>
  );
}
function PoolCard({
  pool,
  used,
  cap,
}: {
  pool: Pool;
  used: number;
  cap: number;
}) {
  const reduce = useReducedMotion();
  const percent = cap > 0 ? (used / cap) * 100 : 0;
  return (
    <Card className={cn("pool-card", pool)}>
      <CardContent className="p-6">
        <div className="flex items-start justify-between">
          <div>
            <div className="eyebrow">
              <span className="pool-symbol">
                {pool === "codex" ? <Code2 size={16} /> : <Command size={16} />}
              </span>
              {poolName(pool)} 执行池
            </div>
            <div className="pool-count">
              <NumberFlow value={used} />
              <span>
                / {cap}
                <small>槽位占用</small>
              </span>
            </div>
          </div>
          <svg
            className="pool-ring"
            viewBox="0 0 64 64"
            aria-label={`占用 ${Math.round(percent)}%`}
          >
            <circle className="ring-track" cx="32" cy="32" r="26" />
            <motion.circle
              cx="32"
              cy="32"
              r="26"
              className="ring-fill"
              transition={{ duration: reduce ? 0 : 0.45 }}
              initial={false}
              animate={{ pathLength: Math.min(1, used / Math.max(1, cap)) }}
              pathLength="1"
              strokeDasharray="1 1"
            />
            <text x="32" y="36" textAnchor="middle">
              {Math.round(percent)}%
            </text>
          </svg>
        </div>
        <div className="capacity-track">
          <motion.div
            transition={{ duration: reduce ? 0 : 0.45 }}
            initial={false}
            animate={{ width: `${Math.min(100, percent)}%` }}
          />
        </div>
        <div className="pool-foot">
          <span>{used >= cap ? "容量已满" : "可接收新任务"}</span>
          <span>
            剩余 <b>{Math.max(0, cap - used)}</b> 个槽位
          </span>
        </div>
      </CardContent>
    </Card>
  );
}
function RunningCard({ job, now }: { job: Job; now: number }) {
  const reduce = useReducedMotion();
  const seconds = Number.isFinite(time(job.started_at))
    ? (now - time(job.started_at)) / 1000
    : job.elapsed_seconds;
  return (
    <motion.article
      layout={!reduce}
      initial={{ opacity: 0, y: reduce ? 0 : 10 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: reduce ? 0 : -8 }}
      transition={{ duration: reduce ? 0 : 0.2 }}
      className="running-card"
    >
      <div className="flex justify-between gap-3">
        <Project name={job.project} />
        <Badge variant="outline" className={cn("pool-badge", job.pool)}>
          <span className="live-dot" />
          {poolName(job.pool)}
        </Badge>
      </div>
      <h3 title={job.id}>{job.id}</h3>
      <p className="job-model">{job.model || "模型未记录"}</p>
      <div className="job-details">
        <span>
          {areas[job.area || ""] || job.area || "未分类"}
          <i />
          {job.tier || "—"} 档
        </span>
        <span className="job-timer">
          <Timer size={13} />
          {duration(seconds)}
        </span>
      </div>
      <Hint text={job.log_last_line || "等待日志输出"}>
        <div className="log-line" tabIndex={0}>
          <Terminal size={13} />
          <span>{job.log_last_line || "等待日志输出…"}</span>
        </div>
      </Hint>
    </motion.article>
  );
}
function Timeline({ jobs, end }: { jobs: Job[]; end: number }) {
  const start = end - 86400000;
  const left = (v: number) =>
    Math.max(0, Math.min(100, ((v - start) / 86400000) * 100));
  const lanes = [
    "codex",
    "cc",
    ...(jobs.some((j) => !j.pool && !j.pin) ? ["unassigned"] : []),
  ];
  return (
    <Card className="timeline-card">
      <CardContent className="p-6">
        <div className="section-heading">
          <div>
            <h2>
              <Activity size={16} />
              生命周期
            </h2>
            <p>最近 24 小时 · 排队到完成，按执行池分泳道</p>
          </div>
          <div className="timeline-legend">
            <span>
              <i className="queue-color" />
              排队
            </span>
            <span>
              <i className="run-color" />
              执行
            </span>
            <span>
              <i className="fail-color" />
              异常
            </span>
          </div>
        </div>
        <div className="timeline-scroll">
          <div className="timeline-axis">
            <span />
            {Array.from({ length: 5 }, (_, i) => (
              <span key={i}>
                {new Date(start + i * 21600000).toLocaleTimeString("zh-CN", {
                  hour: "2-digit",
                  minute: "2-digit",
                  hour12: false,
                })}
              </span>
            ))}
          </div>
          {lanes.map((pool) => {
            const laneJobs = jobs.filter(
              (j) => (j.pool || j.pin || "unassigned") === pool,
            );
            return (
              <div className="lane" key={pool}>
                <div className="lane-title">
                  <span className={cn("lane-dot", pool)} />
                  {poolName(pool)}
                  <small>{laneJobs.length} 单</small>
                </div>
                <div className="lane-body">
                  {laneJobs.length === 0 ? (
                    <p className="lane-empty">此时段暂无任务</p>
                  ) : (
                    laneJobs.map((job) => {
                      const created =
                          time(job.created_at) || time(job.started_at),
                        started = time(job.started_at),
                        finished = time(job.finished_at) || end;
                      const queueEnd = Number.isFinite(started)
                        ? started
                        : finished;
                      return (
                        <div className="timeline-row" key={job.id}>
                          <Hint
                            text={
                              <>
                                <b>{job.id}</b>
                                <br />
                                项目：{job.project || "未分类"}
                                <br />
                                提交：{date(job.created_at)}
                                <br />
                                开始：{date(job.started_at)}
                                <br />
                                结束：{date(job.finished_at)}
                                <br />
                                结果：
                                {results[job.status || ""] ||
                                  (job.status === "running"
                                    ? "运行中"
                                    : "排队中")}
                              </>
                            }
                          >
                            <button
                              className="timeline-job"
                              aria-label={`${job.id} 生命周期`}
                            >
                              <span
                                className="timeline-bar queue-color"
                                style={{
                                  left: `${left(created)}%`,
                                  width: `${Math.max(0.3, left(queueEnd) - left(created))}%`,
                                }}
                              />
                              {Number.isFinite(started) && (
                                <span
                                  className={cn(
                                    "timeline-bar",
                                    ["failed", "blocked", "unknown"].includes(
                                      job.status || "",
                                    )
                                      ? "fail-color"
                                      : "run-color",
                                  )}
                                  style={{
                                    left: `${left(started)}%`,
                                    width: `${Math.max(0.4, left(finished) - left(started))}%`,
                                  }}
                                />
                              )}
                              <span className="timeline-label">{job.id}</span>
                            </button>
                          </Hint>
                        </div>
                      );
                    })
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </CardContent>
    </Card>
  );
}
function MiniCharts({ jobs }: { jobs: Job[] }) {
  const success = jobs.filter((j) => j.status === "done").length;
  const rate = jobs.length ? Math.round((success / jobs.length) * 100) : 0;
  const durations = jobs
    .filter((j) => j.elapsed_seconds != null)
    .slice(0, 24)
    .reverse();
  const max = Math.max(1, ...durations.map((j) => j.elapsed_seconds || 0));
  const average = durations.length
    ? durations.reduce((sum, j) => sum + (j.elapsed_seconds || 0), 0) /
      durations.length
    : null;
  return (
    <div className="mini-charts">
      <div className="success-summary">
        <svg
          viewBox="0 0 50 50"
          role="img"
          aria-label={`成功率 ${jobs.length ? rate + "%" : "暂无数据"}`}
        >
          <circle className="ring-track" cx="25" cy="25" r="20" />
          <circle
            className="success-arc"
            cx="25"
            cy="25"
            r="20"
            pathLength="100"
            strokeDasharray={`${rate} 100`}
          />
          <CheckCheck x="16" y="16" width="18" height="18" />
        </svg>
        <div>
          <small>当前筛选成功率</small>
          <strong>
            {jobs.length ? `${rate}%` : "—"}
            <span>
              {success} / {jobs.length} 单
            </span>
          </strong>
        </div>
      </div>
      <div className="duration-summary">
        <div>
          <small>
            平均耗时 <b>{duration(average)}</b>
          </small>
          <span>最近 {durations.length} 单</span>
        </div>
        <svg
          viewBox="0 0 280 34"
          preserveAspectRatio="none"
          role="img"
          aria-label="当前筛选最近完成单耗时柱状图"
        >
          {durations.map((job, i) => (
            <rect
              key={job.id}
              x={i * (280 / Math.max(1, durations.length))}
              y={34 - ((job.elapsed_seconds || 0) / max) * 30}
              width={Math.max(2, 280 / Math.max(1, durations.length) - 4)}
              height={Math.max(2, ((job.elapsed_seconds || 0) / max) * 30)}
              rx="2"
              className={
                job.status === "done" ? "chart-success" : "chart-failure"
              }
            >
              <title>
                {job.id}：{duration(job.elapsed_seconds)}
              </title>
            </rect>
          ))}
        </svg>
      </div>
    </div>
  );
}
export default function App() {
  const [state, setState] = useState<State | null>(null),
    [error, setError] = useState("");
  const [now, setNow] = useState(Date.now()),
    [project, setProject] = useState("all"),
    [result, setResult] = useState("all"),
    [keyword, setKeyword] = useState("");
  const [dark, setDark] = useState(() =>
    document.documentElement.classList.contains("dark"),
  );
  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    try {
      localStorage.setItem("cs-theme", dark ? "dark" : "light");
    } catch {
      /* private mode */
    }
  }, [dark]);
  useEffect(() => {
    let active = true,
      timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    async function refresh() {
      try {
        const response = await fetch("./api/state", {
          signal: controller.signal,
          cache: "no-store",
        });
        if (!response.ok) throw new Error("状态接口暂不可用");
        const next = (await response.json()) as State;
        if (active) {
          setState(next);
          setError("");
        }
      } catch {
        if (active) setError("连接中断，显示上次快照，正在重试");
      } finally {
        if (active) timer = setTimeout(refresh, 4000);
      }
    }
    void refresh();
    const clock = setInterval(() => setNow(Date.now()), 1000);
    return () => {
      active = false;
      controller.abort();
      clearTimeout(timer);
      clearInterval(clock);
    };
  }, []);
  const dash = state?.dashboard;
  const allJobs = [
    ...(dash?.running || []),
    ...(dash?.queued || []),
    ...(dash?.completed || []),
  ];
  const projects = [
    ...new Set(allJobs.map((j) => j.project || "未分类")),
  ].sort();
  const matches = (job: Job) =>
    project === "all" || (job.project || "未分类") === project;
  const running = (dash?.running || []).filter(matches),
    queued = (dash?.queued || []).filter(matches);
  const completed = (dash?.completed || [])
    .filter(matches)
    .filter(
      (j) =>
        (result === "all" || j.status === result) &&
        `${j.id} ${j.project || ""} ${j.model || ""} ${j.cwd || ""}`
          .toLowerCase()
          .includes(keyword.trim().toLowerCase()),
    );
  const today = (dash?.done || []).filter(matches),
    success = today.filter((j) => j.status === "done").length;
  const tick = dash?.tick;
  const tickAge = tick?.last_at
    ? Math.max(0, (now - time(tick.last_at)) / 1000)
    : null;
  const tickHealthy = tick?.healthy && tickAge !== null && tickAge <= 180;
  const tickLabel =
    tick?.launchd.loaded === false
      ? "调度未加载"
      : tick?.launchd.loaded == null
        ? "调度状态未知"
        : tickAge == null
          ? "等待首次心跳"
          : tickAge > 180
            ? "心跳已超时"
            : tick.error
              ? "上次调度异常"
              : "调度正常";
  return (
    <div className="app-shell">
      <header className="topbar">
        <a className="brand" href="./" aria-label="cs 实时看板首页">
          <span>
            <Terminal size={19} />
          </span>
          <b>
            cs<span className="brand-divider">/</span>
            <em>调度看板</em>
          </b>
          <Badge variant="outline">只读</Badge>
        </a>
        <div className="header-actions">
          <Hint
            text={
              <div>
                launchd：
                {tick?.launchd.loaded
                  ? "已加载"
                  : tick?.launchd.loaded === false
                    ? "未加载"
                    : "未知"}
                <br />
                上次 tick：{date(tick?.last_at || undefined)}
                <br />
                距今：{duration(tickAge)}
                <br />
                超过 3 分钟报红
              </div>
            }
          >
            <span
              tabIndex={0}
              className={cn(
                "tick-status",
                tickHealthy ? "healthy" : "unhealthy",
              )}
            >
              <span
                className={cn("heartbeat", tickHealthy && !error && "beating")}
              />
              {tickLabel}
            </span>
          </Hint>
          <span className="header-line" />
          <Button
            variant="ghost"
            size="icon"
            aria-label={dark ? "切换为亮色" : "切换为暗色"}
            onClick={() => setDark(!dark)}
          >
            {dark ? <Sun size={18} /> : <Moon size={18} />}
          </Button>
        </div>
      </header>
      <main>
        <div className="page-heading">
          <div>
            <div className="eyebrow heading-eyebrow">工作流 / 实时概览</div>
            <h1>
              让每一单，有序推进<span className="heading-dot">.</span>
            </h1>
            <p>执行池、排队原因与交付结果，一屏掌握。</p>
          </div>
          <div className="overview-controls">
            <span className="refresh-note">
              <span className="live-dot" />每 4 秒同步
            </span>
            <FilterSelect
              value={project}
              onChange={setProject}
              label="按项目筛选"
            >
              <SelectItem value="all">全部项目</SelectItem>
              {projects.map((p) => (
                <SelectItem value={p} key={p}>
                  {p}
                </SelectItem>
              ))}
            </FilterSelect>
          </div>
        </div>
        {error && (
          <div role="alert" className="error-banner">
            <TriangleAlert size={16} />
            {error}
          </div>
        )}
        {!state && !error && (
          <div className="loading" role="status">
            正在读取调度快照…
          </div>
        )}
        <section
          className="summary-grid"
          aria-label="执行池总占用与项目交付概览"
        >
          <PoolCard
            pool="codex"
            used={state?.counts.codex || 0}
            cap={state?.caps.codex || 16}
          />
          <PoolCard
            pool="cc"
            used={state?.counts.cc || 0}
            cap={state?.caps.cc || 6}
          />
          <Card className="delivery-card">
            <CardContent className="p-6">
              <div className="eyebrow">
                <CheckCheck size={16} />
                24 小时交付
              </div>
              <div className="delivery-count">
                <NumberFlow value={today.length} />
                <span>单已结束</span>
                <span className="delivery-arrow">
                  <ArrowUpRight size={22} />
                </span>
              </div>
              <div className="delivery-foot">
                <span>
                  <i />
                  成功 <b>{success}</b>
                </span>
                <span>
                  成功率{" "}
                  <b>
                    {today.length
                      ? `${Math.round((success / today.length) * 100)}%`
                      : "—"}
                  </b>
                </span>
              </div>
            </CardContent>
          </Card>
        </section>
        <section className="work-section">
          <div className="section-heading">
            <div>
              <h2>
                <CircleDot size={17} />
                运行中 <Badge variant="secondary">{running.length}</Badge>
              </h2>
              <p>实时工作现场</p>
            </div>
            <span className="section-note">池容量显示全部项目占用</span>
          </div>
          <div className="running-grid">
            <AnimatePresence initial={false}>
              {running.map((job) => (
                <RunningCard key={job.id} job={job} now={now} />
              ))}
            </AnimatePresence>
          </div>
          {!running.length && (
            <div className="empty-state">
              <Code2 size={22} />
              <span>当前项目暂无运行中的任务</span>
            </div>
          )}
        </section>
        <section className="work-section queue-section">
          <div className="section-heading">
            <div>
              <h2>
                <Layers3 size={17} />
                排队中 <Badge variant="secondary">{queued.length}</Badge>
              </h2>
              <p>查看每一单的等待原因</p>
            </div>
            <span className="section-note">
              悬停或聚焦标签查看阻挡详情 <ChevronRight size={13} />
            </span>
          </div>
          <Card className="queue-card">
            <CardContent className="p-0">
              {queued.length ? (
                queued.map((job, i) => (
                  <div className="queue-row" key={job.id}>
                    <span className="queue-number">
                      {String(i + 1).padStart(2, "0")}
                    </span>
                    <div className="queue-identity">
                      <strong>{job.id}</strong>
                      <span>
                        {job.project || "未分类"}
                        <i />
                        {areas[job.area || ""] || job.area || "未分类"}
                        <i />
                        {job.tier || "—"} 档
                      </span>
                    </div>
                    <div className="queue-reasons">
                      {(job.queue_reasons?.length
                        ? job.queue_reasons
                        : [{ type: "awaiting_tick" }]
                      ).map((r, k) => (
                        <ReasonBadge
                          key={k}
                          reason={r}
                          blockers={job.blocked_by}
                        />
                      ))}
                    </div>
                    <span className="queue-time">
                      <Clock3 size={13} />
                      {date(job.created_at)}
                    </span>
                  </div>
                ))
              ) : (
                <div className="empty-state">
                  <Check size={22} />
                  <span>队列已清空，任务畅通无阻</span>
                </div>
              )}
            </CardContent>
          </Card>
        </section>
        <Timeline
          jobs={(dash?.timeline || []).filter(matches)}
          end={time(dash?.updated_at) || now}
        />
        <section className="work-section completed-section">
          <div className="section-heading">
            <div>
              <h2>
                <CheckCheck size={17} />
                完成记录 <Badge variant="secondary">{completed.length}</Badge>
              </h2>
              <p>所有已结束任务 · 结果与执行耗时</p>
            </div>
            <div className="table-filters">
              <div className="search-wrap">
                <Search size={14} />
                <Input
                  aria-label="搜索完成单"
                  placeholder="搜索任务、模型或路径…"
                  value={keyword}
                  onChange={(e) => setKeyword(e.target.value)}
                />
              </div>
              <ListFilter size={15} className="muted-icon" />
              <FilterSelect
                value={result}
                onChange={setResult}
                label="按结果筛选"
              >
                <SelectItem value="all">全部结果</SelectItem>
                {Object.entries(results).map(([v, text]) => (
                  <SelectItem value={v} key={v}>
                    {text}
                  </SelectItem>
                ))}
              </FilterSelect>
            </div>
          </div>
          <Card className="completed-card">
            <CardContent className="p-0">
              <MiniCharts jobs={completed} />
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>任务 / 项目</TableHead>
                    <TableHead>执行池 / 模型</TableHead>
                    <TableHead>结果</TableHead>
                    <TableHead>耗时</TableHead>
                    <TableHead className="text-right">完成时间</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {completed.map((job) => (
                    <TableRow key={job.id}>
                      <TableCell>
                        <div className="table-job">
                          <b>{job.id}</b>
                          <span>{job.project || "未分类"}</span>
                        </div>
                      </TableCell>
                      <TableCell>
                        <div className="table-model">
                          <span>{poolName(job.pool)}</span>
                          <small>{job.model || "未记录模型"}</small>
                        </div>
                      </TableCell>
                      <TableCell>
                        <Badge
                          variant="outline"
                          className={cn(
                            "result-badge",
                            job.status === "done"
                              ? "success"
                              : job.status === "failed"
                                ? "failure"
                                : "other",
                          )}
                        >
                          <span />
                          {results[job.status || ""] || "结果未知"}
                        </Badge>
                      </TableCell>
                      <TableCell className="tabular-nums">
                        {duration(job.elapsed_seconds)}
                      </TableCell>
                      <TableCell className="text-right muted-cell">
                        {date(job.finished_at)}
                      </TableCell>
                    </TableRow>
                  ))}
                  {!completed.length && (
                    <TableRow>
                      <TableCell colSpan={5}>
                        <div className="empty-state">
                          <Search size={22} />
                          <span>没有匹配的完成记录</span>
                        </div>
                      </TableCell>
                    </TableRow>
                  )}
                </TableBody>
              </Table>
            </CardContent>
          </Card>
        </section>
        <footer>
          <span>
            <Terminal size={13} />
            cs · 编码调度器
          </span>
          <span>
            {dash ? `快照更新于 ${date(dash.updated_at)}` : "等待快照"}
            <i />
            只读快照，不回收、不调度
          </span>
          <a href="#top" aria-label="回到顶部">
            回到顶部 <ArrowDownRight size={12} />
          </a>
        </footer>
      </main>
    </div>
  );
}
