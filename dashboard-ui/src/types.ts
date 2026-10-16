// Contract for GET /api/state (lib/dashboard.py, schema_version "1").
// Every job field is optional: old job records may predate any of them.
export type Pool = "codex" | "cc";
export interface Reason {
  type: string;
  pool?: string;
  job?: string;
  mine?: string;
  theirs?: string;
  kind?: string;
  repo?: string;
  score?: number;
  minimum?: number;
  free?: number;
  remaining?: number;
  detail?: string;
}
export interface Job {
  id: string;
  project?: string | null;
  model?: string | null;
  pool?: Pool | string | null;
  pin?: Pool | string | null;
  area?: string | null;
  tier?: string | null;
  status?: string | null;
  created_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  last_checked?: string | null;
  cwd?: string | null;
  log?: string | null;
  conclusion?: string | null;
  files?: string[] | null;
  shared_files?: string[] | null;
  read_only?: boolean | null;
  base_sha?: string | null;
  elapsed_seconds?: number | null;
  log_last_line?: string | null;
  queue_reasons?: Reason[] | null;
  blocked_by?: Reason[] | null;
  reason_text?: string | null;
  affinity?: Partial<Record<Pool, number>> | null;
  exit?: number | null;
}
export interface Tick {
  last_at?: string | null;
  time_source?: string | null;
  age_seconds?: number | null;
  healthy?: boolean | null;
  error?: boolean | null;
  launchd?: {
    loaded?: boolean | null;
    running?: boolean | null;
    pid?: number | null;
    last_exit_status?: number | null;
  } | null;
}
export interface State {
  schema_version?: string;
  counts?: Partial<Record<Pool, number>>;
  caps?: Partial<Record<Pool, number>>;
  running?: Job[];
  queued?: Job[];
  reclaimed?: number;
  dashboard?: {
    running?: Job[];
    queued?: Job[];
    done?: Job[];
    completed?: Job[];
    timeline?: Job[];
    updated_at?: string;
    tick?: Tick | null;
  };
}
/** Which snapshot collection a job was found in. */
export type Bucket = "running" | "queued" | "completed";
export interface Row {
  job: Job;
  bucket: Bucket;
  /** Normalised status: running | queued | done | failed | blocked | cancelled | unknown */
  kind: string;
}
