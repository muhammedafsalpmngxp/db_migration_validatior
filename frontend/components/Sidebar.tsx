"use client";

import { useMemo, useState } from "react";
import { MAPPING_TYPE_LABEL, type Scope, type ScopeTable } from "@/lib/api";
import { CHECK, Dot, SideTag, Spinner } from "./ui";

type SideFilter = "all" | "A" | "B";
type StatusFilter = "all" | "attention";

const compact = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });

const needsAttention = (t: ScopeTable) => ["mismatch", "unknown", "locked"].includes(t.check.status);

/** Consecutive tables of one multi-source mapping, so they can be drawn as a bracket. */
function runs(tables: ScopeTable[]) {
  const out: { id: string; tables: ScopeTable[] }[] = [];
  for (const t of tables) {
    const last = out[out.length - 1];
    if (last && last.id === t.mapping.id) last.tables.push(t);
    else out.push({ id: t.mapping.id, tables: [t] });
  }
  return out;
}

function Item({ t, active, onSelect }: { t: ScopeTable; active: boolean; onSelect: (ref: string) => void }) {
  const check = CHECK[t.check.status];
  const first = t.targets[0];
  return (
    <li>
      <button
        type="button"
        onClick={() => onSelect(t.ref)}
        aria-current={active ? "true" : undefined}
        className={`group flex w-full items-start gap-2.5 rounded-lg px-2.5 py-1.5 text-left transition-colors ${
          active ? "bg-accent-soft" : "hover:bg-surface-2"
        }`}
      >
        <span className="pt-1.5"><Dot tone={check.tone} label={check.label} /></span>
        <span className="min-w-0 flex-1">
          <span className={`block truncate text-[13px] ${active ? "font-semibold text-accent" : "font-medium"}`}>
            {t.table}
          </span>
          <span className="block truncate text-[11px] text-muted">
            {first ? <>→ {first.schema}.{first.table}{t.targets.length > 1 ? ` +${t.targets.length - 1}` : ""}</> : "not migrated"}
          </span>
        </span>
        <span className="num pt-0.5 text-[11px] text-muted" title={`${t.rows?.toLocaleString("en-US") ?? "—"} rows`}>
          {t.rows == null ? "—" : compact.format(t.rows)}
        </span>
      </button>
    </li>
  );
}

export function Sidebar({ scope, selected, onSelect, onOverview, onRefresh, refreshing }: {
  scope: Scope;
  selected: string | null;
  onSelect: (ref: string) => void;
  onOverview: () => void;
  onRefresh: () => void;
  refreshing: boolean;
}) {
  const [query, setQuery] = useState("");
  const [side, setSide] = useState<SideFilter>("all");
  const [status, setStatus] = useState<StatusFilter>("all");

  const attention = scope.groups.flatMap((g) => g.tables).filter(needsAttention).length;

  const groups = useMemo(() => {
    const q = query.trim().toLowerCase();
    return scope.groups
      .filter((g) => side === "all" || g.side === side)
      .map((g) => ({
        ...g,
        tables: g.tables.filter((t) => {
          if (status === "attention" && !needsAttention(t)) return false;
          if (!q) return true;
          return (
            t.table.toLowerCase().includes(q) ||
            t.targets.some((x) => `${x.schema}.${x.table}`.toLowerCase().includes(q)) ||
            t.mapping.id.includes(q)
          );
        }),
      }));
  }, [scope, query, side, status]);

  const shown = groups.reduce((n, g) => n + g.tables.length, 0);

  return (
    <aside className="flex min-h-0 flex-col border-border bg-surface lg:h-full lg:border-r">
      <div className="flex flex-col gap-2.5 border-b border-border p-3">
        <div className="flex items-center justify-between">
          <button type="button" onClick={onOverview} className="text-sm font-semibold hover:text-accent">
            Source tables <span className="num font-normal text-muted">({scope.summary.source_tables})</span>
          </button>
          <button
            type="button"
            onClick={onRefresh}
            disabled={refreshing}
            className="rounded-md border border-border px-2 py-1 text-xs text-muted hover:text-text disabled:opacity-50"
            title="Read row counts and columns from the databases again"
          >
            {refreshing ? <Spinner small label="Refreshing" /> : "↻ Refresh"}
          </button>
        </div>
        <label className="relative block">
          <span className="sr-only">Search tables</span>
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search source or target table…"
            className="w-full rounded-lg border border-border bg-bg px-3 py-2 text-sm outline-none placeholder:text-muted focus:border-accent focus:ring-2 focus:ring-accent/20"
          />
        </label>
        <div className="flex flex-wrap gap-1.5">
          {(["all", "A", "B"] as SideFilter[]).map((s) => (
            <button
              key={s}
              type="button"
              aria-pressed={side === s}
              onClick={() => setSide(s)}
              className={`rounded-full border px-2.5 py-0.5 text-xs ${
                side === s ? "border-accent bg-accent-soft text-accent" : "border-border text-muted hover:text-text"
              }`}
            >
              {s === "all" ? "Both" : scope.groups.find((g) => g.side === s)?.name}
            </button>
          ))}
          <button
            type="button"
            aria-pressed={status === "attention"}
            onClick={() => setStatus(status === "attention" ? "all" : "attention")}
            className={`rounded-full border px-2.5 py-0.5 text-xs ${
              status === "attention" ? "border-bad bg-bad-soft text-bad" : "border-border text-muted hover:text-text"
            }`}
          >
            Needs attention ({attention})
          </button>
        </div>
      </div>

      <nav aria-label="Source tables" className="max-h-[60vh] min-h-0 flex-1 overflow-y-auto p-2 lg:max-h-none">
        {shown === 0 && <p className="px-2 py-6 text-center text-sm text-muted">No table matches.</p>}
        {groups.map((g) => g.tables.length > 0 && (
          <div key={g.side} className="mb-3">
            <h3 className="sticky top-0 z-10 flex items-center gap-2 bg-surface px-2.5 py-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted">
              <SideTag side={g.side} /> {g.name}
              <span className="num ml-auto font-normal normal-case">{g.tables.length}</span>
            </h3>
            <ul className="flex flex-col gap-0.5">
              {runs(g.tables).map((run) =>
                run.tables[0].mapping.sources > 1 ? (
                  <li key={run.id} className="my-1 rounded-lg border border-dashed border-border py-1">
                    <p className="px-2.5 pb-0.5 text-[10px] font-semibold uppercase tracking-wide text-muted">
                      {MAPPING_TYPE_LABEL[run.tables[0].mapping.type]} · {run.id}
                    </p>
                    <ul className="flex flex-col gap-0.5">
                      {run.tables.map((t) => <Item key={t.ref} t={t} active={t.ref === selected} onSelect={onSelect} />)}
                    </ul>
                  </li>
                ) : (
                  <Item key={run.id} t={run.tables[0]} active={run.tables[0].ref === selected} onSelect={onSelect} />
                ),
              )}
            </ul>
          </div>
        ))}
      </nav>

      <div className="flex flex-wrap gap-x-3 gap-y-1 border-t border-border px-3 py-2 text-[11px] text-muted">
        {(["match", "mismatch", "info", "excluded"] as const).map((s) => (
          <span key={s} className="flex items-center gap-1.5"><Dot tone={CHECK[s].tone} label={CHECK[s].label} />{CHECK[s].label}</span>
        ))}
      </div>
    </aside>
  );
}
