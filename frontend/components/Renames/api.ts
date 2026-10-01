// The rename check (backend/app/renames.py): renamed columns decided by the data, 100% or not at all.

export type Verdict = "verified" | "possible" | "ambiguous";

export type Decision = {
  source: string;
  target: string;
  source_type: string;
  target_type: string;
  verdict: Verdict;
  /** paired rows compared, and how many were identical */
  rows_checked: number;
  identical: number;
  differing: number;
  filled: number;
  rate: number;
  /** false: measured on the sample only (a near miss of a big table) */
  full: boolean;
  reason: string;
  also?: string[];
  examples?: { row: string | null; source: string | null; target: string | null }[];
};

export type NamedCheck = {
  source: string;
  target: string;
  match: string;
  rows_checked: number;
  identical: number;
  differing: number;
  rate: number;
  verdict: "verified" | "possible" | "not_supported";
  reason: string;
  examples?: Decision["examples"];
};

export type TargetResult = {
  target: string;
  method: "key" | "columns" | null;
  paired_on?: string;
  headline?: string;
  rows?: {
    source: number; target: number; paired: number | null; unpaired_source: number | null;
    unpaired_target: number | null; coverage: number | null; sampled: number | null;
  };
  decisions: Decision[];
  named: NamedCheck[];
  unmatched?: { source: string; reason: string }[];
  leftover?: { source: string[]; target: string[] };
};

export type RenameResult = {
  mapping: string;
  status: "done" | "skipped" | "locked" | "timeout" | "error";
  headline: string;
  checked_at: string;
  seconds: number;
  targets: TargetResult[];
  stale?: boolean;
  last_attempt?: { status: string; headline: string; checked_at: string } | null;
};

export type RenameJob = {
  running: boolean;
  cancelling: boolean;
  done: number;
  total: number;
  current: string | null;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  waiting: string | null;
  step: string | null;
  step_started_at: string | null;
  log_seq: number;
  log?: { seq: number; at: string; level: string; mapping: string | null; text: string }[];
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

export const renamesApi = {
  saved: (mapping: string) =>
    call<{ result: RenameResult | null; job: RenameJob }>(`/api/renames?mapping=${encodeURIComponent(mapping)}`),
  run: (mapping?: string, refresh = false) =>
    call<RenameJob>("/api/renames/run", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ mapping: mapping ?? null, refresh }),
    }),
  status: (since?: number) => call<RenameJob>(`/api/renames/status${since != null ? `?since=${since}` : ""}`),
  cancel: () => call<{ cancelled: boolean; job: RenameJob }>("/api/renames/cancel", { method: "POST" }),
};
