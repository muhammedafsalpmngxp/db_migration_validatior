// The direct report: client ATNM databases straight to AlTasnimBI (backend app/direct, /api/direct-report).

export type DirectStage = {
  key: "catalogs" | "mappings" | "files";
  title: string;
  status: "waiting" | "running" | "done" | "failed" | "stopped";
  done: number;
  total: number;
  current: string | null;
};

export type DirectLogEntry = { seq: number; at: string; level: "info" | "ok" | "warn" | "error"; text: string };

export type DirectReportMeta = {
  run_id: string;
  started_at: string | null;
  finished_at: string | null;
  overall: "NOT READY" | "INCOMPLETE" | "READY";
  totals: Record<string, number>;
  issues: number;
  test: string | null;
  files: { xlsx: string };
};

export type DirectStatus = {
  running: boolean;
  cancelling: boolean;
  run_id: string | null;
  started_at: string | null;
  finished_at: string | null;
  stage: string | null;
  error: string | null;
  result: DirectReportMeta | null;
  test: string | null;
  waiting: string | null;
  stages: DirectStage[];
  log_seq: number;
  log?: DirectLogEntry[];
  allowed: boolean;
};

export type DirectPreflight = {
  ok: boolean;
  allowed: boolean;
  atnm: { ok: boolean; label: string; error: string | null };
  rds: { ok: boolean; label: string; error: string | null };
  plan: { ok: boolean; mappings?: number; error?: string; databases?: { server: string; database: string }[] } | null;
  busy: string[];
  /** DIRECT_TEST_MAPPINGS: how many of the smallest mappings a test run grades */
  test: number | null;
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

const post = <T,>(path: string) => call<T>(path, { method: "POST" });

export const directApi = {
  preflight: () => call<DirectPreflight>("/api/direct-report/preflight"),
  status: (since?: number) => call<DirectStatus>(`/api/direct-report/status${since != null ? `?since=${since}` : ""}`),
  start: () => post<DirectStatus>("/api/direct-report/start"),
  cancel: () => post<DirectStatus>("/api/direct-report/cancel"),
  list: () => call<{ reports: DirectReportMeta[] }>("/api/direct-report/list"),
  download: (run_id: string) => `/api/direct-report/download/${encodeURIComponent(run_id)}`,
};
