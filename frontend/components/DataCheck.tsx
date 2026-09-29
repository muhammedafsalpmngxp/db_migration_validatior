"use client";

import { useEffect, useState } from "react";
import { dataApi, fmt, type DataCheck as Result, type DataColumn, type DataStatus, type Severity } from "@/lib/api";
import { Badge, Card, ErrorBox, Spinner, Stat, type Tone } from "./ui";
import { ValuesPanel } from "./ValuesPanel";

export const DATA_STATUS: Record<DataStatus, { label: string; tone: Tone; icon: string }> = {
  identical: { label: "Values identical", tone: "ok", icon: "✓" },
  problems: { label: "Data problems", tone: "bad", icon: "✕" },
  review: { label: "Needs review", tone: "warn", icon: "!" },
  skipped: { label: "Not checked", tone: "neutral", icon: "–" },
  error: { label: "Check failed", tone: "bad", icon: "✕" },
};

const SEVERITY: Record<Severity, { label: string; tone: Tone; icon: string }> = {
  error: { label: "Problems", tone: "bad", icon: "✕" },
  review: { label: "To review", tone: "warn", icon: "!" },
  info: { label: "Notes", tone: "neutral", icon: "i" },
};

const VERDICT: Record<DataColumn["verdict"], { label: string; tone: Tone }> = {
  identical: { label: "Identical", tone: "ok" },
  problem: { label: "Problem", tone: "bad" },
  review: { label: "Review", tone: "warn" },
};

const pct = (n: number, total: number) => (total ? `${((n * 100) / total).toFixed(n && n < total ? 1 : 0)}%` : "—");

function when(iso: string) {
  const d = new Date(iso);
  return isNaN(d.getTime()) ? iso : d.toLocaleString();
}

function Findings({ findings }: { findings: Result["findings"] }) {
  const groups = (["error", "review", "info"] as Severity[])
    .map((sev) => ({ sev, items: findings.filter((f) => f.severity === sev) }))
    .filter((g) => g.items.length);
  if (!groups.length) return null;
  return (
    <div className="flex flex-col gap-3">
      {groups.map(({ sev, items }) => (
        <div key={sev}>
          <h3 className="mb-1.5 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-muted">
            <Badge tone={SEVERITY[sev].tone}>{SEVERITY[sev].icon}</Badge> {SEVERITY[sev].label} ({items.length})
          </h3>
          <ul className="flex flex-col gap-1">
            {items.map((f, i) => (
              <li key={i} className={`rounded-md px-3 py-1.5 text-sm ${sev === "error" ? "bg-bad-soft/60" : sev === "review" ? "bg-warn-soft/60" : "bg-surface-2"}`}>
                {f.text}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

function Examples({ c }: { c: DataColumn }) {
  const keyed = Object.entries(c.examples ?? {}).filter(([, v]) => v && v.length);
  const recoding = c.recoding?.pairs ?? [];
  const multi = c.multiset?.examples;
  if (!keyed.length && !recoding.length && !(multi?.source?.length || multi?.target?.length)) return null;
  const label: Record<string, string> = { different: "Different", lost: "Lost", added: "Added in target", case_only: "Case only" };
  return (
    <details className="mt-1 text-xs">
      <summary className="cursor-pointer text-accent">examples</summary>
      <div className="mt-1.5 flex flex-col gap-2">
        {keyed.map(([kind, rows]) => (
          <div key={kind}>
            <div className="mb-0.5 font-medium text-muted">{label[kind] ?? kind}</div>
            <table className="w-full"><tbody>
              {rows!.map((e, i) => (
                <tr key={i} className="border-t border-border">
                  <td className="py-1 pr-2 font-mono text-muted">{e.key}</td>
                  <td className="py-1 pr-2 font-mono">{e.source ?? <i className="text-muted">NULL</i>}</td>
                  <td className="py-1 pr-2 text-muted">→</td>
                  <td className="py-1 font-mono">{e.target ?? <i className="text-muted">NULL</i>}</td>
                </tr>
              ))}
            </tbody></table>
          </div>
        ))}
        {recoding.length > 0 && (
          <div>
            <div className="mb-0.5 font-medium text-muted">Recoding seen (source → target, rows)</div>
            <table className="w-full"><tbody>
              {recoding.map((r, i) => (
                <tr key={i} className="border-t border-border">
                  <td className="py-1 pr-2 font-mono">{r.source}</td>
                  <td className="py-1 pr-2 text-muted">→</td>
                  <td className="py-1 pr-2 font-mono">{r.target ?? "NULL"}</td>
                  <td className="num py-1 text-right text-muted">{fmt(r.rows)}</td>
                </tr>
              ))}
            </tbody></table>
          </div>
        )}
        {(multi?.source?.length || multi?.target?.length) ? (
          <div className="grid gap-2 sm:grid-cols-2">
            {(["source", "target"] as const).map((side) => (
              <div key={side}>
                <div className="mb-0.5 font-medium text-muted">Only in {side}</div>
                {(multi?.[side] ?? []).map((v, i) => (
                  <div key={i} className="flex justify-between gap-2 border-t border-border py-1">
                    <span className="font-mono">{v.value ?? <i className="text-muted">NULL</i>}</span>
                    <span className="num text-muted">{fmt(v.rows)} rows</span>
                  </div>
                ))}
              </div>
            ))}
          </div>
        ) : null}
      </div>
    </details>
  );
}

function ColumnName({ c, onOpen }: { c: DataColumn; onOpen: (c: DataColumn) => void }) {
  return (
    <button type="button" onClick={() => onOpen(c)} title="See the values of this column in source and target"
      className="font-mono text-[13px] text-left text-accent underline decoration-dotted underline-offset-4 hover:decoration-solid">
      {c.label}
    </button>
  );
}

function Nulls({ c }: { c: DataColumn }) {
  const s = c.nulls.source + c.nulls.source_blanks;
  const t = c.nulls.target;
  return (
    <span className="num" title={`Source: ${fmt(c.nulls.source)} NULL + ${fmt(c.nulls.source_blanks)} blank · Target: ${fmt(t)} NULL`}>
      {fmt(s)} <span className="text-muted">→</span> <span className={t > s ? "font-medium text-bad" : ""}>{fmt(t)}</span>
    </span>
  );
}

function KeyedColumns({ columns, matched, onOpen }: { columns: DataColumn[]; matched: number; onOpen: (c: DataColumn) => void }) {
  return (
    <table className="num w-full min-w-[900px] text-sm">
      <thead className="bg-surface-2 text-left text-xs text-muted">
        <tr>
          <th className="px-3 py-2 font-medium">Column (source → target)</th>
          <th className="py-2 pr-3 font-medium">Result</th>
          <th className="py-2 pr-3 text-right font-medium" title="Matched rows with the same value">Identical</th>
          <th className="py-2 pr-3 text-right font-medium" title="Source has a value, target is NULL">Lost</th>
          <th className="py-2 pr-3 text-right font-medium" title="Both have a value and they differ">Different</th>
          <th className="py-2 pr-3 font-medium">Other</th>
          <th className="py-2 pr-3 text-right font-medium" title="NULL (+ blank) values: source → target">NULLs</th>
        </tr>
      </thead>
      <tbody>
        {columns.map((c) => {
          const b = c.buckets!;
          const other = [
            b.recoded && `${fmt(b.recoded)} recoded`,
            b.case_only && `${fmt(b.case_only)} case only`,
            b.added && `${fmt(b.added)} added`,
            b.blank_to_null && `${fmt(b.blank_to_null)} blank→NULL`,
          ].filter(Boolean);
          return (
            <tr key={c.label} className="border-t border-border align-top">
              <td className="px-3 py-2">
                <ColumnName c={c} onOpen={onOpen} />
                <span className="ml-1.5 text-[11px] text-muted">{c.stype}{c.stype !== c.ttype ? ` → ${c.ttype}` : ""}</span>
                {c.is_key && <Badge tone="accent">key</Badge>}
                {c.lookup && (
                  <div className="text-[11px] text-info">via {c.lookup.schema}.{c.lookup.table}.{c.lookup.column}</div>
                )}
                <Examples c={c} />
              </td>
              <td className="py-2 pr-3"><Badge tone={VERDICT[c.verdict].tone}>{VERDICT[c.verdict].label}</Badge></td>
              <td className="py-2 pr-3 text-right">
                {fmt(b.identical)} <span className="text-xs text-muted">{pct(b.identical, matched)}</span>
              </td>
              <td className={`py-2 pr-3 text-right ${b.lost ? "font-medium text-bad" : "text-muted"}`}>{fmt(b.lost)}</td>
              <td className={`py-2 pr-3 text-right ${b.different ? "font-medium text-bad" : "text-muted"}`}>{fmt(b.different)}</td>
              <td className="py-2 pr-3 text-xs text-muted">{other.length ? other.join(" · ") : "—"}</td>
              <td className="py-2 pr-3 text-right"><Nulls c={c} /></td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function KeylessColumns({ columns, onOpen }: { columns: DataColumn[]; onOpen: (c: DataColumn) => void }) {
  return (
    <table className="num w-full min-w-[820px] text-sm">
      <thead className="bg-surface-2 text-left text-xs text-muted">
        <tr>
          <th className="px-3 py-2 font-medium">Column (source → target)</th>
          <th className="py-2 pr-3 font-medium">Result</th>
          <th className="py-2 pr-3 text-right font-medium" title="Values in the source with no equal value in the target">Only in source</th>
          <th className="py-2 pr-3 text-right font-medium" title="Values in the target with no equal value in the source">Only in target</th>
          <th className="py-2 pr-3 text-right font-medium" title="Source values that do not convert to the target type">Don&apos;t convert</th>
          <th className="py-2 pr-3 text-right font-medium" title="NULL (+ blank) values: source → target">NULLs</th>
        </tr>
      </thead>
      <tbody>
        {columns.map((c) => {
          const m = c.multiset!;
          return (
            <tr key={c.label} className="border-t border-border align-top">
              <td className="px-3 py-2">
                <ColumnName c={c} onOpen={onOpen} />
                <span className="ml-1.5 text-[11px] text-muted">{c.stype}{c.stype !== c.ttype ? ` → ${c.ttype}` : ""}</span>
                {c.lookup && <div className="text-[11px] text-info">via {c.lookup.schema}.{c.lookup.table}.{c.lookup.column}</div>}
                <Examples c={c} />
              </td>
              <td className="py-2 pr-3"><Badge tone={VERDICT[c.verdict].tone}>{VERDICT[c.verdict].label}</Badge></td>
              <td className={`py-2 pr-3 text-right ${m.only_in_source ? "font-medium" : "text-muted"}`}>{fmt(m.only_in_source)}</td>
              <td className={`py-2 pr-3 text-right ${m.only_in_target ? "font-medium" : "text-muted"}`}>{fmt(m.only_in_target)}</td>
              <td className="py-2 pr-3 text-right text-muted">{fmt(c.cannot_convert)}</td>
              <td className="py-2 pr-3 text-right"><Nulls c={c} /></td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function NullProfile({ profile }: { profile: NonNullable<Result["profile"]> }) {
  const [side, setSide] = useState<"target" | "source">("target");
  const rows = profile[side];
  const total = side === "target" ? profile.target_rows : profile.source_rows;
  return (
    <details className="rounded-lg border border-border">
      <summary className="cursor-pointer px-3 py-2 text-sm font-medium">NULL profile of every column</summary>
      <div className="border-t border-border p-3">
        <div className="mb-2 inline-flex rounded-lg border border-border bg-surface-2 p-0.5">
          {(["target", "source"] as const).map((s) => (
            <button key={s} type="button" onClick={() => setSide(s)}
              className={`rounded-md px-2.5 py-1 text-xs ${side === s ? "bg-surface shadow-sm" : "text-muted"}`}>
              {s === "target" ? "Target" : "Source"} ({fmt(s === "target" ? profile.target_rows : profile.source_rows)} rows)
            </button>
          ))}
        </div>
        <div className="max-h-80 overflow-auto">
          <table className="num w-full text-sm">
            <thead className="sticky top-0 bg-surface text-left text-xs text-muted">
              <tr><th className="py-1.5 pr-3 font-medium">Column</th><th className="py-1.5 pr-3 font-medium">Type</th>
                <th className="py-1.5 pr-3 text-right font-medium">NULLs</th><th className="py-1.5 pr-3 font-medium">Filled</th>
                <th className="py-1.5 text-right font-medium">Blank</th></tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const filled = total ? ((total - r.nulls - r.blanks) * 100) / total : 0;
                return (
                  <tr key={r.column} className="border-t border-border">
                    <td className="py-1.5 pr-3 font-mono text-[13px]">{r.column}</td>
                    <td className="py-1.5 pr-3 font-mono text-xs text-muted">{r.type}</td>
                    <td className="py-1.5 pr-3 text-right">{fmt(r.nulls)} <span className="text-xs text-muted">{pct(r.nulls, total)}</span></td>
                    <td className="py-1.5 pr-3">
                      <div className="h-1.5 w-28 rounded-full bg-surface-2" title={`${filled.toFixed(1)}% filled`}>
                        <div className={`h-1.5 rounded-full ${filled >= 90 ? "bg-ok" : filled >= 10 ? "bg-warn" : "bg-bad"}`} style={{ width: `${Math.max(0, filled)}%` }} />
                      </div>
                    </td>
                    <td className="py-1.5 text-right text-muted">{r.blanks ? fmt(r.blanks) : "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </details>
  );
}

function Body({ r }: { r: Result }) {
  const [onlyIssues, setOnlyIssues] = useState(false);
  const [open, setOpen] = useState<DataColumn | null>(null);
  const st = DATA_STATUS[r.status] ?? DATA_STATUS.error;
  const rows = r.rows;
  const cols = (r.columns ?? []).filter((c) => !onlyIssues || c.verdict !== "identical");
  const issues = (r.columns ?? []).filter((c) => c.verdict !== "identical").length;

  return (
    <div className="flex flex-col gap-4">
      <div className={`flex flex-wrap items-start gap-3 rounded-lg px-4 py-3 ${
        r.status === "identical" ? "bg-ok-soft" : r.status === "review" ? "bg-warn-soft" : r.status === "skipped" ? "bg-surface-2" : "bg-bad-soft"}`}>
        <Badge tone={st.tone}>{st.icon} {st.label}</Badge>
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium">{r.headline}</p>
          {r.method && (
            <p className="mt-0.5 text-xs text-muted">
              {r.method === "key"
                ? <>Rows matched on <span className="font-mono">{r.key!.source} → {r.key!.target}</span>, every value compared after converting the source to the target type.</>
                : <>No column identifies a row on both sides, so rows are compared as whole-row fingerprints (duplicates count).</>}
            </p>
          )}
        </div>
      </div>

      {rows && r.method && (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
          <Stat label="Source rows" value={fmt(rows.source)} />
          <Stat label="Target rows" value={fmt(rows.target)} />
          {r.method === "key" ? (
            <>
              <Stat label="Matched" value={fmt(rows.matched)} tone="ok" />
              <Stat label="Missing in target" value={fmt(rows.missing_in_target)} tone={rows.missing_in_target ? "bad" : "ok"} />
              <Stat label="Extra in target" value={fmt(rows.extra_in_target)} tone={rows.extra_in_target ? "bad" : "ok"} />
              <Stat label="Rows with a lost / changed value" value={fmt(rows.problem_rows)} tone={rows.problem_rows ? "bad" : "ok"} />
            </>
          ) : (
            <>
              <Stat label="Identical rows" value={fmt(rows.identical_rows)} tone="ok" />
              {rows.identical_rows_ignoring_recoded != null && (
                <Stat label="Identical, recoded columns aside" value={fmt(rows.identical_rows_ignoring_recoded)} tone="warn" />
              )}
              <Stat label="Only in source" value={fmt(rows.only_in_source)} tone={rows.only_in_source ? "bad" : "ok"} />
              <Stat label="Only in target" value={fmt(rows.only_in_target)} tone={rows.only_in_target ? "bad" : "ok"} />
            </>
          )}
        </div>
      )}

      <Findings findings={r.findings} />

      {(r.missing_examples?.length ?? 0) > 0 && (
        <details className="rounded-lg border border-border px-3 py-2 text-sm">
          <summary className="cursor-pointer font-medium">Missing rows ({fmt(rows?.missing_in_target)})</summary>
          <ul className="mt-2 flex flex-wrap gap-1.5">
            {r.missing_examples!.map((e, i) => (
              <li key={i} className="rounded border border-border px-2 py-0.5 font-mono text-xs">{e.key} <span className="text-muted">· {e.table}</span></li>
            ))}
          </ul>
        </details>
      )}

      {(r.columns?.length ?? 0) > 0 && (
        <div>
          <div className="mb-2 flex items-center justify-between gap-2">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">
              Columns ({r.columns!.length} compared)
              <span className="ml-2 font-normal normal-case tracking-normal">· click a column name to see its values</span>
            </h3>
            <label className="flex items-center gap-1.5 text-xs text-muted">
              <input type="checkbox" checked={onlyIssues} onChange={(e) => setOnlyIssues(e.target.checked)} />
              Only columns with problems or review ({issues})
            </label>
          </div>
          <div className="overflow-x-auto rounded-lg border border-border">
            {r.method === "key"
              ? <KeyedColumns columns={cols} matched={rows?.matched ?? 0} onOpen={setOpen} />
              : <KeylessColumns columns={cols} onOpen={setOpen} />}
          </div>
        </div>
      )}

      {r.profile && <NullProfile profile={r.profile} />}

      {open && (
        <ValuesPanel
          key={open.source}
          mapping={r.mapping}
          column={open.source}
          label={open.label}
          keyed={r.method === "key"}
          checkedAt={r.checked_at}
          onClose={() => setOpen(null)}
        />
      )}
    </div>
  );
}

/** Value by value comparison of the mapping, run on request; the last result is kept. */
export function DataCheck({ mappingId, mappingType }: { mappingId: string; mappingType: string }) {
  const [result, setResult] = useState<Result | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const applicable = ["one_to_one", "union", "merge"].includes(mappingType);

  useEffect(() => {
    let live = true;
    if (!applicable) return;
    dataApi.saved(mappingId)
      .then((d) => { if (live) { setResult(d.result); setLoaded(true); } })
      .catch(() => live && setLoaded(true));
    return () => { live = false; };
  }, [mappingId, applicable]);

  async function run() {
    setRunning(true);
    setError(null);
    try {
      setResult(await dataApi.run(mappingId));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  }

  if (!applicable) {
    return (
      <Card title="Data check">
        <p className="text-sm text-muted">
          {mappingType === "transform"
            ? "Transform mapping: business logic reshapes the rows, so values cannot be compared one to one."
            : "Excluded from the migration: nothing to compare."}
        </p>
      </Card>
    );
  }

  return (
    <Card
      title={<>Data check <span className="font-normal text-muted">· are the values the same?</span></>}
      aside={
        <div className="flex items-center gap-3">
          {result && !running && <span className="text-xs text-muted">checked {when(result.checked_at)} · {result.seconds}s</span>}
          <button
            type="button"
            onClick={run}
            disabled={running}
            className="rounded-lg bg-accent px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50 dark:text-bg"
          >
            {running ? "Checking…" : result ? "Re-run" : "Run data check"}
          </button>
        </div>
      }
    >
      {error && <ErrorBox>{error}</ErrorBox>}
      {running && <Spinner label="Comparing every value in SQL Server… a large table can take a minute." />}
      {!running && result && <Body r={result} />}
      {!running && !result && loaded && !error && (
        <p className="text-sm text-muted">
          Compares every row and value of the source with the target: rows missing or extra, values lost, changed or
          recoded, and NULL counts per column. Nothing is written; the check runs read-only inside SQL Server.
        </p>
      )}
    </Card>
  );
}
