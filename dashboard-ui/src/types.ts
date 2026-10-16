export type Pool = "codex" | "cc";
export interface Reason {
  type: string;
  pool?: string;
  job?: string;
  mine?: string;
  theirs?: string;
  kind?: string;
  score?: number;
  minimum?: number;
  detail?: string;
}
export interface Job {
  id: string;
  project?: string;
  model?: string;
  pool?: Pool;
  pin?: Pool;
  area?: string;
  tier?: string;
  status?: string;
  created_at?: string;
  started_at?: string;
  finished_at?: string;
  cwd?: string;
  elapsed_seconds?: number | null;
  log_last_line?: string;
  queue_reasons?: Reason[];
  blocked_by?: Reason[];
  exit?: number | null;
}
export interface State {
  schema_version: string;
  counts: Record<Pool, number>;
  caps: Record<Pool, number>;
  running: Job[];
  queued: Job[];
  reclaimed: number;
  dashboard: {
    running: Job[];
    queued: Job[];
    done: Job[];
    completed: Job[];
    timeline: Job[];
    updated_at: string;
    tick: {
      last_at: string | null;
      age_seconds: number | null;
      healthy: boolean;
      error: boolean | null;
      launchd: {
        loaded: boolean | null;
        running: boolean | null;
        pid: number | null;
        last_exit_status: number | null;
      };
    };
  };
}
