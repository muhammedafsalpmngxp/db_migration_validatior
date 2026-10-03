// The report section's calls (backend app/report/api.py).

export type ResultWord = "Must fix" | "Not checked" | "Needs a decision" | "Correct" | "By design";
export type Overall = "NOT READY" | "INCOMPLETE" | "READY";

export type Stage = {
  key: "preflight" | "part1" | "part2" | "collect" | "ai" | "files";
  title: string;
  status: "waiting" | "running" | "done" | "failed" | "skipped" | "stopped";
  done: number;
  total: number;
  /** the table (Part 1) or mapping (Part 2) being checked now */
  current: string | null;
  step: string | null;
  fraction: number | null;
  eta_seconds: number | null;
  waiting: string | null;
  started_at: string | null;
  finished_at: string | null;
  note: string | null;
};

export type ReportLogEntry = { seq: number; at: string; level: "info" | "ok" | "warn" | "error"; text: string; stage: string | null };

export type ReportMeta = {
  run_id: string;
  started_at: string;
  finished_at: string | null;
  mode: string | null;
  overall: Overall;
  totals: { part1: Record<ResultWord, number>; part2: Record<ResultWord, number>; issues: Record<ResultWord, number> };
  not_checked: number;
  summary_source: "ai" | "template" | null;
  files: { xlsx: string; docx: string };
  self_check: string[];
  built_at: string;
  /** a test run (REPORT_TEST_TABLES): what it covered */
  test?: string | null;
};

export type ReportStatus = {
  running: boolean;
  cancelling: boolean;
  run_id: string | null;
  started_at: string | null;
  finished_at: string | null;
  mode: string | null;
  stage: Stage["key"] | null;
  error: string | null;
  result: ReportMeta | null;
  stages: Stage[];
  fraction: number | null;
  interrupted: { run_id: string; started_at: string; stage: string } | null;
  log_seq: number;
  log?: ReportLogEntry[];
  allowed: boolean;
  client: string;
};

export type Preflight = {
  ok: boolean;
  allowed: boolean;
  atnm: { ok: boolean; label: string; error: string | null };
  rds: { ok: boolean; label: string; error: string | null };
  plan: { ok: boolean; tables?: number; mappings?: number; error?: string;
          databases?: { source_db: string; target_db: string; tables: number }[] } | null;
  busy: string[];
  ai: { enabled: boolean; model: string | null; reason: string | null };
  /** test mode (REPORT_TEST_TABLES > 0): how many of the smallest tables and mappings are checked */
  test: { tables: number; mappings: number; of_tables: number; of_mappings: number } | null;
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

export const reportApi = {
  preflight: () => call<Preflight>("/api/report/preflight"),
  status: (since?: number) => call<ReportStatus>(`/api/report/status${since != null ? `?since=${since}` : ""}`),
  start: (resume = false) => post<ReportStatus>("/api/report/start", { resume }),
  cancel: () => post<ReportStatus>("/api/report/cancel", {}),
  list: () => call<{ reports: ReportMeta[] }>("/api/report/list"),
  rebuild: (run_id: string) => post<ReportMeta>("/api/report/rebuild", { run_id }),
  /** analyse a finished run again from what its checks measured (no table is checked again) */
  reanalyse: (run_id: string) => post<ReportStatus>("/api/report/reanalyse", { run_id }),
  download: (run_id: string, kind: "docx" | "xlsx") => `/api/report/download/${encodeURIComponent(run_id)}/${kind}`,
};
