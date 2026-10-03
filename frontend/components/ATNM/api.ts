// Shapes returned by the ATNM copy check (backend/app/ATNM/api.py) and the calls that fetch them.

export type Level = "ok" | "review" | "problem" | "none";
export type TableStatus = "verified" | "unverified" | "review" | "problem";
export type Scope = "required" | "all";
export type DataStatus = "identical" | "different" | "error" | "locked" | "skipped" | "timeout" | "changed";

/** How a row count moved during the last hour of reads (a live table). */
export type Movement = { rows_then: number; rows_now: number; change: number; minutes: number };

export type ServerInfo = { key: "source" | "target"; label: string; host: string; configured: boolean };

export type ConnError = { server: "source" | "target"; label: string; database: string; message: string; hint: string | null };

export type Health = {
  ok: boolean;
  servers: (ServerInfo & {
    ok: boolean;
    databases: { server: string; database: string; ok: boolean; error: ConnError | null; version?: string; seconds: number }[];
  })[];
  pairs: { id: string; source_db: string; target_db: string }[];
};

export type ColumnDef = {
  position: number;
  name: string;
  type: string;
  base: string;
  max_length: number;
  nullable: boolean;
  identity: boolean;
  computed: boolean;
  collation: string | null;
};

export type ColumnSummary = {
  source: number;
  target: number;
  same: number;
  missing: string[];
  extra: string[];
  changed: { name: string; notes: string[]; severity: Level }[];
  /** columns renamed in RDS, verified by their data (identical on every paired row) */
  renamed?: { source: string; target: string; notes: string[]; severity: Level }[];
  severity: Level;
};

/** One key or rule (primary / unique / foreign key, default value, check rule) ATNM vs RDS. */
export type ConstraintRow = {
  kind: "primary key" | "unique key" | "foreign key" | "default value" | "check rule";
  what: string;
  status: "same" | "missing" | "changed" | "extra";
  severity: Level;
  note: string;
};

export type ConstraintSummary = {
  total: number;
  same: number;
  missing: number;
  changed: number;
  extra: number;
  severity: Level;
  rows: ConstraintRow[];
};

/** A column only one table has, matched with one only the other has, by its data. */
export type RenameFound = {
  source: string;
  target: string;
  source_type: string;
  target_type: string;
  paired: number;
  identical: number;
  differing: number;
  filled: number;
  rate: number;
  coverage: number;
  verdict: "verified" | "possible" | "ambiguous";
  reason: string;
};

export type EmptyCount = { nulls: number; blanks: number | null };

export type TableRow = {
  key: string;
  schema: string;
  table: string;
  in_source: boolean;
  in_target: boolean;
  rows: { source: number | null; target: number | null; exact: boolean; metadata_source?: number; metadata_target?: number };
  columns: ColumnSummary | null;
  row_key: { kind: string; columns: string[] } | null;
  checks: { table: Level; columns: Level; rows: Level; data: Level; constraints?: Level };
  /** keys and rules compared (null: could not be read; absent: older backend) */
  constraints?: ConstraintSummary | null;
  data: {
    status: DataStatus;
    headline: string;
    checked_at: string;
    seconds: number;
    /** the result no longer describes the table: other columns, another cutoff, or rows changed since */
    stale: boolean;
    stale_reason?: string | null;
    changed_since?: { source: { then: number; now: number }; target: { then: number; now: number } } | null;
    cutoff?: { column: string; value: string } | null;
    /** a later check that could not finish; the result above is the last measured one */
    last_attempt?: { status: DataStatus; headline: string; checked_at: string; error_kind?: string } | null;
  } | null;
  /** row counts still moving on either server (still being written to or copied into) */
  live?: { source: Movement | null; target: Movement | null } | null;
  status: TableStatus;
  reason: string;
  reasons?: { severity: Level; text: string }[];
  /** one of the tables the migration uses (a source table of the migration plan) */
  required?: boolean;
  /** the plan mapping it belongs to */
  mapping?: string | null;
};

export type PairSummary = {
  tables_source: number;
  tables_target: number;
  missing_in_target: number;
  only_in_target: number;
  /** required tables that neither database has */
  missing_everywhere?: number;
  /** results that no longer describe their table */
  out_of_date?: number;
  /** tables whose row counts are still moving */
  live?: number;
  verified: number;
  unverified: number;
  review: number;
  problem: number;
  rows_source: number;
  rows_target: number;
};

export type PairView = {
  id: string;
  source_db: string;
  target_db: string;
  source: { database: string; server: { major: number; version: string; edition: string; collation: string }; read_at: number; tables: number } | null;
  target: PairView["source"];
  errors: ConnError[];
  tables: TableRow[];
  summary: PairSummary | null;
  /** the required tables of this pair: how many, and their summary */
  required?: { count: number; summary: PairSummary; error: string | null };
  notes?: string[];
};

export type Job = {
  running: boolean;
  cancelling: boolean;
  done: number;
  total: number;
  current: string | null;
  pair: string | null;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  scope: { pairs: string[]; table: string | null; tables?: "all" | "required" | "one" } | null;
  /** the database of the table being checked */
  database?: string | null;
  table_started_at?: string | null;
  table_rows?: number | null;
  /** results so far in this run, by status */
  results?: Partial<Record<DataStatus, number>>;
  /** what the run is doing right now, in words, and since when */
  step?: string | null;
  step_started_at?: string | null;
  /** number of the newest activity entry */
  log_seq?: number;
  /** waiting for a server that cannot be reached (VPN down?), or for the other section's run */
  waiting?: { reason: string; hint?: string; paused: boolean; until?: number; since?: number; server?: string } | null;
  /** weighted by rows; the ETA assumes the tables still to come are identical (one read each) */
  progress?: {
    fraction: number;
    rows_done: number;
    rows_total: number;
    eta_seconds: number | null;
    eta_is_minimum: boolean;
    speed_rows_per_second: number | null;
    pass: { n: number; of: number; label: string; elapsed: number; estimate: number | null } | null;
  } | null;
  /** the last run, when it did not finish (restart, failure, stop) and can be resumed */
  resumable?: {
    started_at: string; status: "running" | "paused" | "stopped" | "failed"; done: number; total: number | null;
    tables: string; table: string | null; updated_at: string;
  } | null;
  resumed_from?: string | null;
};

export type LogEntry = {
  seq: number;
  at: string;
  level: "info" | "ok" | "warn" | "error";
  database: string | null;
  table: string | null;
  text: string;
};

export type Overview = {
  source: ServerInfo;
  target: ServerInfo;
  pairs: PairView[];
  job: Job;
  settings: { diff_rows_max: number; data_check_max_rows: number };
  /** the options file (cutoff) cannot be read */
  options_error?: string | null;
};

export type ColumnCompare = {
  name: string;
  source: ColumnDef | null;
  target: ColumnDef | null;
  status: "same" | "missing" | "extra" | "changed" | "renamed";
  severity: Level;
  notes: string[];
};

export type DataResult = {
  pair: string;
  key: string;
  status: DataStatus;
  headline: string;
  checked_at: string;
  seconds: number;
  method?: { modern: boolean; key: string[] | null; key_kind: string | null };
  rows?: { source: number; target: number };
  compared_columns?: number;
  not_compared?: string[];
  columns?: { name: string; filled_source: number; filled_target: number }[];
  diff?: {
    groups_differing: number;
    groups_examined: number;
    partial: boolean;
    missing?: number;
    extra?: number;
    changed?: number;
    duplicate_keys?: number;
    only_in_source?: number;
    only_in_target?: number;
  };
  examples?: {
    missing_keys?: string[];
    extra_keys?: string[];
    changed?: { key: string; columns: { name: string; source: string | null; target: string | null }[] }[];
    only_in_source_rows?: Record<string, string | null>[];
    only_in_target_rows?: Record<string, string | null>[];
  };
  findings: { severity: "ok" | "info" | "review" | "problem" | "error"; text: string }[];
  renames?: RenameFound[];
  renames_note?: string | null;
  /** NULL and blank values of every column on both servers */
  profile?: { rows: { source: number; target: number }; columns: { name: string; source: EmptyCount | null; target: EmptyCount | null }[] };
};

export type TableDetail = {
  pair: { id: string; source_db: string; target_db: string };
  table: TableRow;
  columns: ColumnCompare[];
  data: DataResult | null;
  job: Job;
};

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { cache: "no-store", ...init });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      // not JSON: keep the status line
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

const post = <T,>(path: string, body: unknown) =>
  call<T>(path, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });

export const atnmApi = {
  health: () => call<Health>("/api/atnm/health"),
  overview: (refresh = false) => call<Overview>(`/api/atnm/overview${refresh ? "?refresh=true" : ""}`),
  table: (pair: string, table: string) =>
    call<TableDetail>(`/api/atnm/table?pair=${encodeURIComponent(pair)}&table=${encodeURIComponent(table)}`),
  check: (opts: { pair?: string; table?: string; tables?: Scope }) =>
    post<Job>("/api/atnm/check", { pair: opts.pair ?? null, table: opts.table ?? null, tables: opts.tables ?? "all" }),
  /** the run, and its activity entries after `since` (0 = all kept) */
  status: (since?: number) =>
    call<Job & { log?: LogEntry[] }>(`/api/atnm/check/status${since != null ? `?since=${since}` : ""}`),
  cancel: () => post<{ cancelled: boolean; job: Job }>("/api/atnm/check/cancel", {}),
  resume: () => post<Job>("/api/atnm/check/resume", {}),
  exportUrl: (pair: string | undefined, tables: Scope) =>
    `/api/atnm/export.csv?tables=${tables}${pair ? `&pair=${encodeURIComponent(pair)}` : ""}`,
};

export const STATUS: Record<TableStatus, { label: string; tone: "ok" | "warn" | "bad" | "neutral" }> = {
  verified: { label: "Verified", tone: "ok" },
  unverified: { label: "Not checked yet", tone: "neutral" },
  review: { label: "Needs a look", tone: "warn" },
  problem: { label: "Problem", tone: "bad" },
};

export const LEVEL_TONE: Record<Level, "ok" | "warn" | "bad" | "neutral"> = {
  ok: "ok",
  review: "warn",
  problem: "bad",
  none: "neutral",
};
