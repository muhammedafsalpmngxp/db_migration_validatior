"use client";

import { useEffect, useState } from "react";
import {
  fmt, valuesApi, valuesUrl,
  type RowStatus, type ValueCounts, type ValueRows, type ValuesQuery,
} from "@/lib/api";
import { Badge, ErrorBox, Spinner, type Tone } from "./ui";

const STATUS: Record<RowStatus, { label: string; tone: Tone }> = {
  identical: { label: "Same", tone: "ok" },
  case_only: { label: "Case only", tone: "warn" },
  added: { label: "Added in target", tone: "warn" },
  blank_to_null: { label: "Blank → NULL", tone: "neutral" },
  lost: { label: "Lost", tone: "bad" },
  recoded: { label: "Recoded", tone: "info" },
  different: { label: "Different", tone: "bad" },
  missing: { label: "Missing in target", tone: "bad" },
  extra: { label: "Extra in target", tone: "bad" },
};

const ROW_FILTERS: { id: string; label: string; count: keyof ValueRows["counts"] }[] = [
  { id: "all", label: "All rows", count: "all_rows" },
  { id: "differences", label: "Only differences", count: "differences" },
  { id: "lost", label: "Lost", count: "lost" },
  { id: "different", label: "Different", count: "different" },
  { id: "recoded", label: "Recoded", count: "recoded" },
  { id: "missing", label: "Missing in target", count: "missing" },
  { id: "extra", label: "Extra in target", count: "extra" },
];

const PAGE = 50;

function Value({ v, raw }: { v: string | null; raw?: string | null }) {
  if (v === null) return <i className="text-muted">NULL</i>;
  if (v === "") return <i className="text-muted">(blank)</i>;
  return (
    <span className="whitespace-pre-wrap break-all font-mono text-[13px]">
      {v}
      {raw != null && <span className="ml-1.5 text-[11px] text-muted" title="The id stored in the target">(id {raw})</span>}
    </span>
  );
}

function useDebounced<T>(value: T, ms = 350) {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

/** The real values of one column, both sides, read live - opened from the data check. */
export function ValuesPanel({ mapping, column, label, keyed, checkedAt, onClose }: {
  mapping: string;
  column: string;
  label: string;
  keyed: boolean;
  checkedAt: string;
  onClose: () => void;
}) {
  const [view, setView] = useState<"rows" | "counts">(keyed ? "rows" : "counts");
  const [filter, setFilter] = useState("all");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(0);
  const [reveal, setReveal] = useState(false);
  const [data, setData] = useState<ValueRows | ValueCounts | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const q = useDebounced(search);

  const query: ValuesQuery = { mapping, column, view, filter, q, page, size: PAGE, reveal };

  useEffect(() => {
    let live = true;
    Promise.resolve().then(() => { if (live) { setLoading(true); setError(null); } });
    valuesApi.get({ mapping, column, view, filter, q, page, size: PAGE, reveal })
      .then((d) => { if (live) setData(d); })
      .catch((e) => { if (live) { setData(null); setError(e instanceof Error ? e.message : String(e)); } })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [mapping, column, view, filter, q, page, reveal]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  function pick(next: Partial<{ view: "rows" | "counts"; filter: string }>) {
    if (next.view) { setView(next.view); setFilter("all"); }
    if (next.filter) setFilter(next.filter);
    setPage(0);
  }

  const total = data?.total ?? 0;
  const from = total ? page * PAGE + 1 : 0;
  const to = Math.min(total, (page + 1) * PAGE);

  return (
    <div className="fixed inset-0 z-50 flex justify-end" role="dialog" aria-modal="true" aria-label={`Values of ${label}`}>
      <button type="button" aria-label="Close" onClick={onClose} className="absolute inset-0 bg-black/30" />
      <div className="relative flex h-full w-full max-w-5xl flex-col border-l border-border bg-surface shadow-2xl">
        <header className="flex flex-wrap items-start justify-between gap-3 border-b border-border px-5 py-4">
          <div className="min-w-0">
            <p className="text-xs uppercase tracking-wide text-muted">Values · read live now</p>
            <h2 className="truncate font-mono text-base font-semibold">{label}</h2>
            {data && (
              <p className="mt-0.5 text-xs text-muted">
                {data.column.stype}{data.column.stype !== data.column.ttype ? ` → ${data.column.ttype}` : ""}
                {data.column.lookup && <> · target id shown as {data.column.lookup.schema}.{data.column.lookup.table}.{data.column.lookup.column}</>}
                {data.view === "rows" && <> · rows matched on <span className="font-mono">{data.key.source} → {data.key.target}</span></>}
              </p>
            )}
          </div>
          <button type="button" onClick={onClose} className="rounded-md border border-border px-2.5 py-1 text-sm hover:border-accent hover:text-accent">
            Close ✕
          </button>
        </header>

        <div className="flex flex-col gap-3 border-b border-border px-5 py-3">
          <div className="flex flex-wrap items-center gap-2">
            <div role="tablist" className="inline-flex rounded-lg border border-border bg-surface-2 p-0.5">
              {keyed && (
                <button role="tab" aria-selected={view === "rows"} onClick={() => pick({ view: "rows" })}
                  className={`rounded-md px-3 py-1 text-xs font-medium ${view === "rows" ? "bg-surface shadow-sm" : "text-muted"}`}>
                  Side by side
                </button>
              )}
              <button role="tab" aria-selected={view === "counts"} onClick={() => pick({ view: "counts" })}
                className={`rounded-md px-3 py-1 text-xs font-medium ${view === "counts" ? "bg-surface shadow-sm" : "text-muted"}`}>
                Value counts
              </button>
            </div>
            <input type="search" value={search} onChange={(e) => { setSearch(e.target.value); setPage(0); }}
              placeholder={view === "rows" ? "Search key or value…" : "Search value…"}
              className="w-56 rounded-lg border border-border bg-bg px-3 py-1.5 text-xs outline-none focus:border-accent focus:ring-2 focus:ring-accent/20" />
            <a href={valuesUrl({ ...query, page: 0 }, "csv")} className="ml-auto rounded-lg border border-border px-3 py-1.5 text-xs hover:border-accent hover:text-accent"
              title="Download this view (up to 100,000 rows) as CSV">
              Download CSV
            </a>
          </div>

          {data?.view === "rows" && (
            <div className="flex flex-wrap gap-1.5">
              {ROW_FILTERS.filter((f) => f.id === "all" || f.id === "differences" || data.counts[f.count]).map((f) => (
                <button key={f.id} type="button" aria-pressed={filter === f.id} onClick={() => pick({ filter: f.id })}
                  className={`rounded-full border px-2.5 py-0.5 text-xs ${filter === f.id ? "border-accent bg-accent-soft text-accent" : "border-border text-muted hover:text-text"}`}>
                  {f.label} <span className="num">{fmt(data.counts[f.count])}</span>
                </button>
              ))}
            </div>
          )}
          {data?.view === "counts" && (
            <div className="flex flex-wrap items-center gap-1.5">
              {[{ id: "all", label: `All values ${fmt(data.totals.distinct_values)}` },
                { id: "differences", label: `Count differs ${fmt(data.totals.differing_values)}` }].map((f) => (
                <button key={f.id} type="button" aria-pressed={filter === f.id} onClick={() => pick({ filter: f.id })}
                  className={`rounded-full border px-2.5 py-0.5 text-xs ${filter === f.id ? "border-accent bg-accent-soft text-accent" : "border-border text-muted hover:text-text"}`}>
                  {f.label}
                </button>
              ))}
              <span className="num ml-auto text-xs text-muted">
                {fmt(data.totals.source_rows)} source rows · {fmt(data.totals.target_rows)} target rows
              </span>
            </div>
          )}

          {data?.sensitive && (
            <div className="flex items-center gap-2 rounded-lg bg-warn-soft px-3 py-1.5 text-xs text-warn">
              {data.hidden ? "Contact details are hidden." : "Showing contact details."}
              <button type="button" onClick={() => setReveal(!reveal)} className="font-medium underline">
                {data.hidden ? "Show values" : "Hide again"}
              </button>
            </div>
          )}
          <p className="text-[11px] text-muted">
            These values are read from the databases now. The data check verdict is from {new Date(checkedAt).toLocaleString()}; re-run it if the two disagree.
          </p>
        </div>

        <div className="min-h-0 flex-1 overflow-auto">
          {error && <div className="p-5"><ErrorBox>{error}</ErrorBox></div>}
          {loading && !data && <div className="p-5"><Spinner label="Reading values…" /></div>}

          {data?.view === "rows" && (
            <table className={`w-full text-sm ${loading ? "opacity-50" : ""}`}>
              <thead className="sticky top-0 bg-surface-2 text-left text-xs text-muted">
                <tr>
                  <th className="px-4 py-2 font-medium">Key · {data.key.target}</th>
                  {data.union && <th className="py-2 pr-3 font-medium">Source table</th>}
                  <th className="py-2 pr-3 font-medium">Source · {data.column.source}</th>
                  <th className="py-2 pr-3 font-medium">Target · {data.column.target}</th>
                  <th className="py-2 pr-4 font-medium">Result</th>
                </tr>
              </thead>
              <tbody>
                {data.rows.length === 0 && (
                  <tr><td colSpan={5} className="px-4 py-10 text-center text-muted">No rows.</td></tr>
                )}
                {data.rows.map((r, i) => (
                  <tr key={`${r.key}|${i}`} className={`border-t border-border align-top ${STATUS[r.status]?.tone === "bad" ? "bg-bad-soft/30" : ""}`}>
                    <td className="num px-4 py-1.5 font-mono text-xs text-muted">{r.key ?? <i>NULL</i>}</td>
                    {data.union && <td className="py-1.5 pr-3 text-xs text-muted">{r.table ?? "—"}</td>}
                    <td className="py-1.5 pr-3">{r.status === "extra" ? <span className="text-xs text-muted">no source row</span> : <Value v={r.source} />}</td>
                    <td className="py-1.5 pr-3">{r.status === "missing" ? <span className="text-xs text-muted">no target row</span> : <Value v={r.target} raw={r.target_raw} />}</td>
                    <td className="py-1.5 pr-4"><Badge tone={STATUS[r.status]?.tone ?? "neutral"}>{STATUS[r.status]?.label ?? r.status}</Badge></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {data?.view === "counts" && (
            <table className={`w-full text-sm ${loading ? "opacity-50" : ""}`}>
              <thead className="sticky top-0 bg-surface-2 text-left text-xs text-muted">
                <tr>
                  <th className="px-4 py-2 font-medium">Value</th>
                  <th className="py-2 pr-3 text-right font-medium">Rows in source</th>
                  <th className="py-2 pr-3 text-right font-medium">Rows in target</th>
                  <th className="py-2 pr-4 text-right font-medium">Difference</th>
                </tr>
              </thead>
              <tbody>
                {data.values.length === 0 && (
                  <tr><td colSpan={4} className="px-4 py-10 text-center text-muted">No values.</td></tr>
                )}
                {data.values.map((v, i) => {
                  const d = v.target_rows - v.source_rows;
                  return (
                    <tr key={i} className={`border-t border-border align-top ${d ? "bg-bad-soft/30" : ""}`}>
                      <td className="px-4 py-1.5"><Value v={v.is_null ? null : v.value} raw={v.target_raw} /></td>
                      <td className="num py-1.5 pr-3 text-right">{fmt(v.source_rows)}</td>
                      <td className="num py-1.5 pr-3 text-right">{fmt(v.target_rows)}</td>
                      <td className={`num py-1.5 pr-4 text-right ${d ? "font-medium text-bad" : "text-ok"}`}>
                        {d === 0 ? "0" : `${d > 0 ? "+" : "−"}${fmt(Math.abs(d))}`}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>

        {data && total > 0 && (
          <footer className="flex items-center justify-between gap-2 border-t border-border px-5 py-2.5 text-xs">
            <span className="num text-muted">{fmt(from)}–{fmt(to)} of {fmt(total)} {data.view === "rows" ? "rows" : "values"}</span>
            <div className="flex gap-1.5">
              <button type="button" disabled={page === 0 || loading} onClick={() => setPage(page - 1)}
                className="rounded-md border border-border px-3 py-1 disabled:opacity-40">← Previous</button>
              <button type="button" disabled={to >= total || loading} onClick={() => setPage(page + 1)}
                className="rounded-md border border-border px-3 py-1 disabled:opacity-40">Next →</button>
            </div>
          </footer>
        )}
      </div>
    </div>
  );
}
