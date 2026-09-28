// Shapes returned by the backend (backend/app/main.py) and the calls that fetch them.
// Every number here is read live from SQL Server; the plan only says what maps to what.

export type Side = "A" | "B" | "T";

export type Database = { side: Side; name: string; label: string; role: "source" | "target" };

export type Health = { ok: boolean; databases: (Database & { ok: boolean; error: string | null })[] };

export type MappingType = "one_to_one" | "union" | "merge" | "transform" | "excluded";

export type TableInfo = {
  ref: string;
  side: Side;
  database: string;
  schema: string;
  table: string;
  exists: boolean;
  /** Held by another session (a load in progress); its facts cannot be read right now. */
  locked: boolean;
  rows: number | null;
  columns: number | null;
  size_kb: number | null;
  created: string | null;
};

export type CheckStatus = "match" | "mismatch" | "info" | "unknown" | "excluded" | "locked";

export type RowCheck = {
  rule: string;
  expected: number | null;
  actual: number | null;
  delta: number | null;
  delta_pct: number | null;
  status: CheckStatus;
};

export type ScopeTable = TableInfo & {
  role: "driving" | "lookup" | null;
  mapping: { id: string; type: MappingType; sources: number };
  targets: TableInfo[];
  check: RowCheck;
};

export type Scope = {
  databases: Database[];
  read_at: number;
  groups: (Database & { tables: ScopeTable[] })[];
  summary: {
    source_tables: number;
    target_tables: number;
    mappings: number;
    checks: Partial<Record<CheckStatus, number>>;
    missing_tables: number;
    locked_tables: string[];
  };
};

export type Member = TableInfo & { role: "driving" | "lookup" | null; selected: boolean };

export type Column = {
  position: number;
  name: string;
  type: string;
  nullable: boolean;
  identity: boolean;
  pk: boolean;
};

export type ColumnStatus = "match" | "renamed" | "changed" | "source_only" | "target_only";
export type MatchMethod = "declared" | "exact" | "case" | "normalized" | "inferred";

export type ColumnRow = {
  source: Column | null;
  target: Column | null;
  match: MatchMethod | null;
  diffs: ("type" | "nullable" | "pk")[];
  status: ColumnStatus;
};

export type ColumnSummary = Record<ColumnStatus, number> & {
  type_changes: number;
  nullable_changes: number;
  source_columns: number;
  target_columns: number;
};

export type Comparison = {
  target: string;
  exists: boolean;
  locked?: boolean;
  rows: ColumnRow[];
  summary: ColumnSummary | null;
};

export type CompareResult = {
  selected: TableInfo;
  mapping: {
    id: string;
    type: MappingType;
    note: string | null;
    sources: Member[];
    targets: Member[];
  };
  row_check: RowCheck;
  source_columns: Column[];
  comparisons: Comparison[];
};

export type ExactCount = { ref: string; rows: number; seconds: number; metadata_rows: number };

async function get<T>(path: string): Promise<T> {
  const res = await fetch(path, { cache: "no-store" });
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

export const api = {
  health: () => get<Health>("/api/health"),
  scope: (refresh = false) => get<Scope>(`/api/scope${refresh ? "?refresh=true" : ""}`),
  compare: (ref: string) => get<CompareResult>(`/api/compare?table=${encodeURIComponent(ref)}`),
  exactCount: (ref: string) => get<ExactCount>(`/api/row-count?table=${encodeURIComponent(ref)}`),
};

export const fmt = (n: number | null | undefined) => (n == null ? "—" : n.toLocaleString("en-US"));

export function fmtSize(kb: number | null | undefined) {
  if (kb == null) return "—";
  if (kb < 1024) return `${kb} KB`;
  if (kb < 1024 * 1024) return `${(kb / 1024).toFixed(1)} MB`;
  return `${(kb / 1024 / 1024).toFixed(2)} GB`;
}

export function fmtDelta(delta: number | null, base: number | null) {
  if (delta == null) return "—";
  if (delta === 0) return "0";
  const sign = delta > 0 ? "+" : "−";
  const pct = base ? ` (${sign}${Math.abs((delta * 100) / base).toFixed(1)}%)` : "";
  return `${sign}${fmt(Math.abs(delta))}${pct}`;
}

export const MAPPING_TYPE_LABEL: Record<MappingType, string> = {
  one_to_one: "One to one",
  union: "Union",
  merge: "Merge",
  transform: "Transform",
  excluded: "Excluded",
};

export const MAPPING_TYPE_HINT: Record<MappingType, string> = {
  one_to_one: "One source table becomes one target table.",
  union: "Several source tables are stacked into one target table.",
  merge: "A driving table is enriched from a lookup table.",
  transform: "Business logic reshapes several sources into several targets.",
  excluded: "In scope, but deliberately not migrated.",
};
