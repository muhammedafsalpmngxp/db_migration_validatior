"use client";

import { useMemo, useState } from "react";
import { fmt, fmtDelta, MAPPING_TYPE_LABEL, type CheckStatus, type Scope, type ScopeTable } from "@/lib/api";
import { Coverage } from "./Coverage";
import { DATA_STATUS } from "./DataCheck";
import { DataOverview, useDataResults } from "./DataOverview";
import { Badge, Card, CHECK, SideTag, Stat } from "./ui";

type Filter = "all" | CheckStatus;

type Row = {
  id: string;
  type: ScopeTable["mapping"]["type"];
  sources: ScopeTable[];
  targets: ScopeTable["targets"];
  check: ScopeTable["check"];
};

function mappingRows(scope: Scope): Row[] {
  const byId = new Map<string, Row>();
  for (const g of scope.groups) {
    for (const t of g.tables) {
      const row = byId.get(t.mapping.id);
      if (row) row.sources.push(t);
      else byId.set(t.mapping.id, { id: t.mapping.id, type: t.mapping.type, sources: [t], targets: t.targets, check: t.check });
    }
  }
  return [...byId.values()];
}

const sum = (xs: (number | null)[]) => (xs.some((x) => x == null) ? null : xs.reduce<number>((a, b) => a + (b ?? 0), 0));

export function Overview({ scope, onSelect }: { scope: Scope; onSelect: (ref: string) => void }) {
  const [filter, setFilter] = useState<Filter>("all");
  const rows = useMemo(() => mappingRows(scope), [scope]);
  const s = scope.summary;
  const count = (st: CheckStatus) => s.checks[st] ?? 0;
  const shown = filter === "all" ? rows : rows.filter((r) => r.check.status === filter);
  const data = useDataResults();
  const named = useMemo(() => rows.map((r) => ({
    id: r.id,
    ref: r.sources[0].ref,
    label: r.sources.length > 1 ? `${r.sources[0].table} +${r.sources.length - 1}` : r.sources[0].table,
  })), [rows]);

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h2 className="text-lg font-semibold">Migration overview</h2>
        <p className="text-sm text-muted">
          Live row counts for every mapping in the plan. Pick a table on the left, or a row below, for its column comparison.
        </p>
        {s.locked_tables.length > 0 && (
          <p className="mt-2 rounded-lg border border-warn/30 bg-warn-soft px-3 py-2 text-sm text-warn">
            Locked right now (probably being loaded): {s.locked_tables.map((r) => r.split(".").slice(1).join(".")).join(", ")}.
            Their counts show once the load releases them; press Refresh.
          </p>
        )}
        {(s.busy_tables?.length ?? 0) > 0 && (
          <p className="mt-2 rounded-lg border border-info/30 bg-info-soft px-3 py-2 text-sm text-info">
            Being written to right now: {s.busy_tables!.map((r) => r.split(".").slice(1).join(".")).join(", ")}.
            Their rows were counted directly and include rows the load has not committed yet, so these counts
            may still change; press Refresh once the load has finished.
          </p>
        )}
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-6">
        <Stat label="Source tables" value={s.source_tables} hint="In the migration scope" />
        <Stat label="Target tables" value={s.target_tables} />
        <Stat label="Mappings" value={s.mappings} />
        <Stat label="Counts match" value={count("match")} tone="ok" />
        <Stat label="Counts differ" value={count("mismatch") + count("unknown")} tone="bad"
          hint={count("locked") ? `${count("locked")} more cannot be checked: table locked` : undefined} />
        <Stat label="No rule / excluded" value={count("info") + count("excluded")} tone="info" hint="Transform mappings have no count rule" />
      </div>

      <DataOverview mappings={named} data={data} onSelect={onSelect} />

      <Card
        flush
        title="Mappings"
        aside={
          <div className="flex flex-wrap gap-1.5">
            {(["all", "mismatch", "match", "info", "excluded", ...(count("locked") ? ["locked" as const] : [])] as Filter[]).map((f) => (
              <button
                key={f}
                type="button"
                aria-pressed={filter === f}
                onClick={() => setFilter(f)}
                className={`rounded-full border px-2.5 py-0.5 text-xs ${
                  filter === f ? "border-accent bg-accent-soft text-accent" : "border-border text-muted hover:text-text"
                }`}
              >
                {f === "all" ? `All ${rows.length}` : `${CHECK[f].label} ${rows.filter((r) => r.check.status === f).length}`}
              </button>
            ))}
          </div>
        }
      >
        <div className="overflow-x-auto">
          <table className="num w-full min-w-[980px] text-sm">
            <thead className="text-left text-xs text-muted">
              <tr className="border-b border-border">
                <th className="px-4 py-2 font-medium">Source</th>
                <th className="px-3 py-2 text-right font-medium">Source rows</th>
                <th className="px-3 py-2 font-medium">Target</th>
                <th className="px-3 py-2 text-right font-medium">Target rows</th>
                <th className="px-3 py-2 text-right font-medium">Difference</th>
                <th className="px-3 py-2 font-medium">Row counts</th>
                <th className="px-4 py-2 font-medium" title="Result of the last data check">Data</th>
              </tr>
            </thead>
            <tbody>
              {shown.length === 0 && (
                <tr><td colSpan={7} className="px-4 py-8 text-center text-muted">No mapping with this status.</td></tr>
              )}
              {shown.map((r) => {
                const srcRows = r.type === "merge"
                  ? (r.sources.find((x) => x.role === "driving") ?? r.sources[0]).rows
                  : sum(r.sources.map((x) => x.rows));
                const tgtRows = sum(r.targets.map((x) => x.rows));
                const first = r.sources[0];
                return (
                  <tr
                    key={r.id}
                    onClick={() => onSelect(first.ref)}
                    className="cursor-pointer border-b border-border last:border-0 hover:bg-surface-2"
                  >
                    <td className="px-4 py-2.5">
                      <div className="flex items-center gap-2">
                        <SideTag side={first.side} />
                        <button type="button" className="truncate text-left font-medium hover:text-accent" onClick={(e) => { e.stopPropagation(); onSelect(first.ref); }}>
                          {first.table}
                        </button>
                        {r.sources.length > 1 && <span className="shrink-0 text-xs text-muted">+{r.sources.length - 1} more</span>}
                      </div>
                      <div className="mt-0.5 pl-7 text-xs text-muted">{MAPPING_TYPE_LABEL[r.type]}{r.type === "merge" ? " · driving table rows" : r.type === "union" ? " · sum of sources" : ""}</div>
                    </td>
                    <td className="px-3 py-2.5 text-right">{r.type === "transform" ? <span className="text-muted">{fmt(srcRows)} total</span> : fmt(srcRows)}</td>
                    <td className="px-3 py-2.5">
                      {r.targets.length === 0 ? <span className="text-muted">—</span> : r.targets.map((t) => (
                        <div key={t.ref} className="flex items-center gap-2">
                          <SideTag side="T" /><span><span className="text-muted">{t.schema}.</span>{t.table}</span>
                        </div>
                      ))}
                    </td>
                    <td className="px-3 py-2.5 text-right">
                      {r.targets.length > 1 ? r.targets.map((t) => <div key={t.ref}>{fmt(t.rows)}</div>) : fmt(tgtRows)}
                    </td>
                    <td className={`px-3 py-2.5 text-right ${r.check.status === "mismatch" ? "font-medium text-bad" : "text-muted"}`}>
                      {fmtDelta(r.check.delta, r.check.expected)}
                    </td>
                    <td className="px-3 py-2.5"><Badge tone={CHECK[r.check.status].tone}>{CHECK[r.check.status].label}</Badge></td>
                    <td className="px-4 py-2.5">
                      {data.results[r.id] ? (
                        <Badge tone={DATA_STATUS[data.results[r.id].status]?.tone ?? "neutral"} title={data.results[r.id].headline}>
                          {DATA_STATUS[data.results[r.id].status]?.icon} {DATA_STATUS[data.results[r.id].status]?.label}
                        </Badge>
                      ) : <span className="text-xs text-muted">—</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>

      <Coverage />
    </div>
  );
}
