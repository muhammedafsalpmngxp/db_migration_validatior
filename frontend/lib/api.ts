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

// ---- Keys ------------------------------------------------------------------------

export type LinkedTable = { ref: string; schema: string; table: string; rows: number; in_plan: boolean };

export type ForeignKey = {
  name: string;
  /** outgoing: this table holds the key; incoming: another table points at this one */
  direction: "outgoing" | "incoming";
  parent: LinkedTable;
  referenced: LinkedTable;
  columns: string[];
  ref_columns: string[];
  enabled: boolean;
  trusted: boolean;
  on_delete: string;
  on_update: string;
};

export type TableKeys = {
  ref: string;
  locked: boolean;
  keys: { name: string; kind: "primary" | "unique"; index: string; columns: string[] }[];
  foreign_keys: ForeignKey[];
};

export type ForeignKeyCheck = {
  table: string;
  fk: string;
  child_rows: number;
  filled_rows: number;
  null_rows: number;
  distinct_values: number | null;
  orphan_rows: number;
  seconds: number;
};

export const keysApi = {
  keys: (ref: string) => get<TableKeys>(`/api/keys?table=${encodeURIComponent(ref)}`),
  check: (ref: string, fk: string) =>
    get<ForeignKeyCheck>(`/api/fk-check?table=${encodeURIComponent(ref)}&fk=${encodeURIComponent(fk)}`),
};

// ---- Data check (backend/app/datacheck.py) ---------------------------------------

export type DataStatus = "identical" | "problems" | "review" | "skipped" | "error";
export type Severity = "error" | "review" | "info";
export type Bucket = "identical" | "case_only" | "added" | "blank_to_null" | "lost" | "recoded" | "different";

export type DataExample = { key: string; source: string | null; target: string | null };
export type ValueExample = { value: string | null; rows: number };

export type DataColumn = {
  label: string;
  source: string;
  target: string;
  stype: string;
  ttype: string;
  match: string;
  mode: "direct" | "converted" | "lookup";
  lookup: { schema: string; table: string; column: string; key: string; coverage: number } | null;
  is_key: boolean;
  verdict: "identical" | "problem" | "review";
  cannot_convert: number;
  nulls: { source: number; source_blanks: number; target: number; target_blanks: number; added: number };
  /** keyed comparison: every matched row falls into one bucket */
  buckets?: Record<Bucket, number>;
  examples?: Partial<Record<"different" | "lost" | "added" | "case_only", DataExample[]>>;
  recoding?: { pairs: { source: string; target: string; rows: number }[]; distinct_source_values: number; inconsistent: number };
  /** keyless comparison: values compared as multisets */
  multiset?: { only_in_source: number; only_in_target: number; examples: { source?: ValueExample[]; target?: ValueExample[] } };
  recoded?: boolean;
};

export type DataCheck = {
  mapping: string;
  type: MappingType;
  status: DataStatus;
  headline: string;
  checked_at: string;
  seconds: number;
  method?: "key" | "fingerprint" | null;
  key?: { source: string; target: string } | null;
  key_notes?: string[];
  source?: string;
  target?: string;
  rows?: {
    source: number;
    target: number;
    matched?: number;
    missing_in_target?: number;
    extra_in_target?: number;
    source_null_keys?: number;
    target_null_keys?: number;
    identical_rows?: number;
    problem_rows?: number;
    only_in_source?: number;
    only_in_target?: number;
    identical_rows_ignoring_recoded?: number;
  };
  missing_by_source?: { table: string; rows: number }[];
  missing_examples?: { table: string; key: string }[];
  extra_examples?: string[];
  columns?: DataColumn[];
  findings: { severity: Severity; text: string; column?: string }[];
  profile?: {
    source_rows: number;
    target_rows: number;
    source: { column: string; type: string; nulls: number; blanks: number }[];
    target: { column: string; type: string; nulls: number; blanks: number }[];
  };
};

export type DataCheckSummary = Pick<DataCheck, "mapping" | "status" | "headline" | "checked_at" | "seconds" | "method"> & {
  errors: number;
  reviews: number;
};

export type DataCheckJob = {
  running: boolean;
  done: number;
  total: number;
  current: string | null;
  started_at: string | null;
  finished_at: string | null;
};

export const dataApi = {
  saved: (mapping: string) => get<{ result: DataCheck | null }>(`/api/data-check/saved?mapping=${encodeURIComponent(mapping)}`),
  run: (mapping: string) => get<DataCheck>(`/api/data-check?mapping=${encodeURIComponent(mapping)}&refresh=true`),
  all: () => get<{ results: Record<string, DataCheckSummary>; job: DataCheckJob }>("/api/data-checks"),
  runAll: async () => {
    const res = await fetch("/api/data-checks/run", { method: "POST" });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.json() as Promise<{ started: boolean; job: DataCheckJob }>;
  },
};

// ---- Values behind a data check (backend/app/values.py) ----------------------------

export type ValuesColumn = {
  source: string;
  target: string;
  stype: string;
  ttype: string;
  lookup: { schema: string; table: string; column: string } | null;
};

export type RowStatus = Bucket | "missing" | "extra";

export type ValueRows = {
  view: "rows";
  column: ValuesColumn;
  key: { source: string; target: string };
  union: boolean;
  hidden: boolean;
  sensitive: boolean;
  filter: string;
  total: number;
  page: number;
  size: number;
  counts: Record<"all_rows" | "differences" | "lost" | "different" | "recoded" | "missing" | "extra", number>;
  rows: { key: string | null; table: string | null; source: string | null; target: string | null;
          target_raw: string | null; truncated: boolean; status: RowStatus }[];
};

export type ValueCounts = {
  view: "counts";
  column: ValuesColumn;
  hidden: boolean;
  sensitive: boolean;
  filter: string;
  total: number;
  page: number;
  size: number;
  totals: { distinct_values: number; differing_values: number; source_rows: number; target_rows: number };
  values: { value: string | null; target_raw: string | null; is_null: boolean; truncated: boolean;
            source_rows: number; target_rows: number }[];
};

export type ValuesQuery = {
  mapping: string;
  column: string;
  view: "rows" | "counts";
  filter?: string;
  q?: string;
  page?: number;
  size?: number;
  reveal?: boolean;
};

export function valuesUrl(p: ValuesQuery, format: "json" | "csv" = "json") {
  const u = new URLSearchParams({
    mapping: p.mapping, column: p.column, view: p.view, filter: p.filter ?? "all", q: p.q ?? "",
    page: String(p.page ?? 0), size: String(p.size ?? 50), reveal: String(!!p.reveal), format,
  });
  return `/api/data-check/values?${u.toString()}`;
}

export const valuesApi = {
  get: <T extends ValueRows | ValueCounts>(p: ValuesQuery) => get<T>(valuesUrl(p)),
};
