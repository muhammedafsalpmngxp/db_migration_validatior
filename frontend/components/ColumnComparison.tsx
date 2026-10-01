"use client";

import { useState } from "react";
import type { Column, ColumnRow, ColumnStatus, Comparison, MatchMethod } from "@/lib/api";
import { Badge, Card, COLUMN_STATUS, ErrorBox } from "./ui";
import { Evidence, EvidenceNote, mergeRows, RenamePanel, targetResult, useRenames } from "./Renames/RenameCheck";

const METHOD_LABEL: Record<MatchMethod, string> = {
  declared: "declared in plan",
  exact: "same name",
  case: "case differs",
  normalized: "name normalized",
  inferred: "inferred",
};

const METHOD_HINT: Record<MatchMethod, string> = {
  declared: "Paired because the mapping file says so",
  exact: "Identical column name",
  case: "Same name apart from upper/lower case",
  normalized: "Same name once case, underscores and spaces are ignored",
  inferred: "A guess from the target's naming convention (key suffix or table prefix). Check it.",
};

const FILTERS: ("all" | ColumnStatus)[] = ["all", "match", "renamed", "changed", "source_only", "target_only"];

function ColumnSearch({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <label className="ml-auto">
      <span className="sr-only">Filter columns</span>
      <input
        type="search"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="Filter columns…"
        className="w-48 rounded-lg border border-border bg-bg px-3 py-1.5 text-xs outline-none placeholder:text-muted focus:border-accent focus:ring-2 focus:ring-accent/20"
      />
    </label>
  );
}

function Flags({ col, hot }: { col: Column; hot?: boolean }) {
  return (
    <>
      {col.pk && <span className={`ml-1.5 rounded bg-surface-2 px-1 text-[10px] font-semibold ${hot ? "text-warn" : "text-muted"}`}>PK</span>}
      {col.identity && <span className="ml-1 rounded bg-surface-2 px-1 text-[10px] text-muted">identity</span>}
    </>
  );
}

function Cell({ col, diffs }: { col: Column | null; diffs: ColumnRow["diffs"] }) {
  if (!col) return <><td className="py-2 pr-3 text-muted">—</td><td className="pr-3" /><td className="pr-3" /></>;
  const hot = (d: ColumnRow["diffs"][number]) => (diffs.includes(d) ? "font-medium text-warn" : "");
  return (
    <>
      <td className="py-2 pr-3">
        <span className="font-mono text-[13px]">{col.name}</span>
        <Flags col={col} hot={diffs.includes("pk")} />
      </td>
      <td className={`py-2 pr-3 font-mono text-[13px] ${hot("type") || "text-muted"}`}>{col.type}</td>
      <td className={`py-2 pr-3 text-xs ${hot("nullable") || "text-muted"}`}>{col.nullable ? "NULL" : "NOT NULL"}</td>
    </>
  );
}

function ComparisonTable({ comparison, renames }: { comparison: Comparison; renames?: ReturnType<typeof useRenames> }) {
  const [filter, setFilter] = useState<"all" | ColumnStatus>("all");
  const [query, setQuery] = useState("");
  if (comparison.locked) {
    return (
      <div className="rounded-lg border border-warn/30 bg-warn-soft px-4 py-3 text-sm text-warn">
        {comparison.target.replace(/^T\./, "")} is locked by another session (probably a load in progress),
        so its columns cannot be read right now. Refresh in a moment.
      </div>
    );
  }
  if (!comparison.exists || !comparison.summary) {
    return <ErrorBox>{comparison.target} does not exist in the target database.</ErrorBox>;
  }
  // Renames the data proved join their two rows; without a rename result nothing changes.
  const tr = renames ? targetResult(renames.result, comparison) : null;
  const merged = mergeRows(comparison, tr);
  const s = merged.summary;
  const q = query.trim().toLowerCase();
  const rows = merged.rows.filter((r) =>
    (filter === "all" || r.status === filter) &&
    (!q || [r.source?.name, r.target?.name].some((n) => n?.toLowerCase().includes(q))),
  );

  return (
    <div className="flex flex-col gap-3">
      {renames && <RenamePanel state={renames} tr={tr} />}
      <div className="flex flex-wrap items-center gap-2">
        {FILTERS.map((f) => {
          const n = f === "all" ? merged.rows.length : s[f];
          const active = filter === f;
          return (
            <button
              key={f}
              type="button"
              onClick={() => setFilter(f)}
              aria-pressed={active}
              title={f === "all" ? undefined : COLUMN_STATUS[f].hint}
              disabled={f !== "all" && n === 0}
              className={`rounded-full border px-3 py-1 text-xs font-medium transition-colors disabled:opacity-40 ${
                active ? "border-accent bg-accent-soft text-accent" : "border-border text-muted hover:text-text"
              }`}
            >
              {f === "all" ? "All" : COLUMN_STATUS[f].label} <span className="num">{n}</span>
            </button>
          );
        })}
        <ColumnSearch value={query} onChange={setQuery} />
      </div>
      <p className="num text-xs text-muted">
        {s.source_columns} source columns · {s.target_columns} target columns · {s.type_changes} type change{s.type_changes === 1 ? "" : "s"} · {s.nullable_changes} nullability change{s.nullable_changes === 1 ? "" : "s"}
      </p>

      <div className="overflow-x-auto rounded-lg border border-border">
        <table className="w-full min-w-[900px] text-sm">
          <thead className="bg-surface-2 text-left text-xs text-muted">
            <tr>
              <th colSpan={3} className="border-b border-border px-3 pt-2 font-semibold uppercase tracking-wide">Source</th>
              <th className="border-b border-border px-3 pt-2" />
              <th colSpan={3} className="border-b border-border px-3 pt-2 font-semibold uppercase tracking-wide">Target</th>
              <th className="border-b border-border px-3 pt-2" />
            </tr>
            <tr>
              <th className="px-3 py-2 font-medium">Column</th>
              <th className="py-2 pr-3 font-medium">Type</th>
              <th className="py-2 pr-3 font-medium">Null</th>
              <th className="py-2 pr-3 font-medium">Paired by</th>
              <th className="py-2 pr-3 font-medium">Column</th>
              <th className="py-2 pr-3 font-medium">Type</th>
              <th className="py-2 pr-3 font-medium">Null</th>
              <th className="py-2 pr-3 font-medium">Status</th>
            </tr>
          </thead>
          <tbody className="[&>tr>td:first-child]:pl-3">
            {rows.length === 0 && (
              <tr><td colSpan={8} className="py-6 text-center text-muted">No matching columns.</td></tr>
            )}
            {rows.map((r, i) => (
              <tr key={`${r.source?.name ?? ""}|${r.target?.name ?? ""}|${i}`} className="border-t border-border align-top">
                <Cell col={r.source} diffs={r.diffs} />
                <td className="py-2 pr-3 text-xs">
                  {r.match === "data" || r.suggestion ? <Evidence row={r} /> : r.match ? (
                    <span title={METHOD_HINT[r.match]} className={r.match === "inferred" ? "text-info" : "text-muted"}>
                      {METHOD_LABEL[r.match]}
                      <EvidenceNote row={r} />
                    </span>
                  ) : <span className="text-muted">—</span>}
                </td>
                <Cell col={r.target} diffs={r.diffs} />
                <td className="py-2 pr-3">
                  <Badge tone={COLUMN_STATUS[r.status].tone} title={COLUMN_STATUS[r.status].hint}>
                    {COLUMN_STATUS[r.status].label}
                  </Badge>
                  {r.diffs.length > 0 && (
                    <span className="mt-1 block text-[11px] text-warn">{r.diffs.join(", ")} differ{r.diffs.length === 1 ? "s" : ""}</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function ColumnComparison({ comparisons, sourceLabel, transform, mappingId }: {
  comparisons: Comparison[];
  sourceLabel: string;
  transform: boolean;
  /** with it, renamed columns can be checked on the data (see components/Renames) */
  mappingId?: string;
}) {
  const [tab, setTab] = useState(0);
  const renames = useRenames(transform ? undefined : mappingId);
  const current = comparisons[Math.min(tab, comparisons.length - 1)];
  if (!current) return null;

  return (
    <Card
      title={<>Column comparison <span className="font-normal text-muted">· {sourceLabel} vs {current.target.replace(/^T\./, "")}</span></>}
      aside={comparisons.length > 1 && (
        <div role="tablist" aria-label="Target table" className="inline-flex rounded-lg border border-border bg-surface-2 p-0.5">
          {comparisons.map((c, i) => (
            <button
              key={c.target}
              role="tab"
              aria-selected={i === tab}
              onClick={() => setTab(i)}
              className={`rounded-md px-2.5 py-1 font-mono text-xs ${i === tab ? "bg-surface text-text shadow-sm" : "text-muted hover:text-text"}`}
            >
              {c.target.replace(/^T\./, "")}
            </button>
          ))}
        </div>
      )}
    >
      {transform && (
        <p className="mb-3 text-xs text-muted">
          This is a transform mapping: target columns may come from any of its sources, so pairing by name is a guide, not a verdict.
        </p>
      )}
      <ComparisonTable key={current.target} comparison={current} renames={mappingId && !transform ? renames : undefined} />
    </Card>
  );
}

/** The columns of one table on its own, for a table with nothing to compare against. */
export function ColumnList({ columns, title }: { columns: Column[]; title: string }) {
  const [query, setQuery] = useState("");
  const q = query.trim().toLowerCase();
  const shown = columns.filter((c) => !q || c.name.toLowerCase().includes(q));
  return (
    <Card title={title} aside={<ColumnSearch value={query} onChange={setQuery} />}>
      <div className="overflow-x-auto rounded-lg border border-border">
        <table className="w-full min-w-[520px] text-sm">
          <thead className="bg-surface-2 text-left text-xs text-muted">
            <tr>
              <th className="px-3 py-2 font-medium">#</th>
              <th className="py-2 pr-3 font-medium">Column</th>
              <th className="py-2 pr-3 font-medium">Type</th>
              <th className="py-2 pr-3 font-medium">Null</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((c) => (
              <tr key={c.name} className="border-t border-border">
                <td className="num px-3 py-2 text-xs text-muted">{c.position}</td>
                <td className="py-2 pr-3"><span className="font-mono text-[13px]">{c.name}</span><Flags col={c} /></td>
                <td className="py-2 pr-3 font-mono text-[13px] text-muted">{c.type}</td>
                <td className="py-2 pr-3 text-xs text-muted">{c.nullable ? "NULL" : "NOT NULL"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
