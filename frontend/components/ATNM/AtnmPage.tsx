"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fmt } from "@/lib/api";
import { Badge, Dot, ErrorBox, Spinner } from "../ui";
import {
  atnmApi, LEVEL_TONE, STATUS,
  type ConnError, type Health, type Job, type LogEntry, type Overview, type PairSummary, type PairView, type Scope,
  type TableRow, type TableStatus,
} from "./api";
import { RunLog } from "./RunLog";
import { SectionTabs } from "./SectionTabs";
import { TableDetail } from "./TableDetail";

type Filter = "attention" | TableStatus | "all";

const FILTERS: { key: Filter; label: string }[] = [
  { key: "attention", label: "Needs attention" },
  { key: "problem", label: "Problems" },
  { key: "review", label: "Needs a look" },
  { key: "unverified", label: "Not checked yet" },
  { key: "verified", label: "Verified" },
  { key: "all", label: "All" },
];

const BAR: { key: TableStatus; className: string }[] = [
  { key: "verified", className: "bg-ok" },
  { key: "unverified", className: "bg-muted/40" },
  { key: "review", className: "bg-warn" },
  { key: "problem", className: "bg-bad" },
];

function param(name: string) {
  return new URLSearchParams(window.location.search).get(name);
}

function ServerChips({ health, overview }: { health: Health | null; overview: Overview | null }) {
  const servers = health?.servers ?? (overview ? [overview.source, overview.target].map((s) => ({ ...s, ok: null as boolean | null })) : []);
  return (
    <ol className="flex flex-wrap items-center gap-1.5 text-xs">
      {servers.map((s, i) => (
        <li key={s.key} className="flex items-center gap-1.5">
          {i === 1 && <span aria-hidden className="text-muted">→</span>}
          <span
            className="flex items-center gap-1.5 rounded-md border border-border bg-surface px-2 py-1"
            title={s.host || "No host set in backend/.env"}
          >
            <span className={`h-1.5 w-1.5 rounded-full ${s.ok == null ? "bg-muted/50" : s.ok ? "bg-ok" : "bg-bad"}`} />
            <span className="font-medium">{s.label}</span>
            <span className="hidden text-muted sm:inline">{s.key === "source" ? "client server (VPN)" : "server"}</span>
          </span>
        </li>
      ))}
    </ol>
  );
}

function Problem({ errors, onRetry, retrying }: { errors: ConnError[]; onRetry: () => void; retrying: boolean }) {
  // One message per server: both databases of a server fail the same way.
  const byServer = [...new Map(errors.map((e) => [e.server + e.message, e])).values()];
  return (
    <div role="alert" className="flex flex-wrap items-start justify-between gap-3 rounded-xl border border-bad/30 bg-bad-soft px-4 py-3 text-sm text-bad">
      <div className="flex flex-col gap-1">
        {byServer.map((e, i) => (
          <p key={i}>
            <strong className="font-semibold">{e.message}</strong>
            {e.hint && <span className="block text-bad/90">{e.hint}</span>}
          </p>
        ))}
      </div>
      <button type="button" onClick={onRetry} disabled={retrying}
        className="rounded-lg border border-bad/40 bg-surface px-3 py-1.5 text-xs font-medium hover:bg-bad-soft disabled:opacity-50">
        {retrying ? <Spinner small label="Connecting…" /> : "Retry"}
      </button>
    </div>
  );
}

/** The summary of the tables in view: the required ones, or all of them. */
function scoped(p: PairView, scope: Scope): PairSummary | null {
  return scope === "required" && p.required ? p.required.summary : p.summary;
}

function PairCard({ p, s, noun, active, onSelect, labels }: {
  p: PairView; s: PairSummary | null; noun: string; active: boolean; onSelect: () => void; labels: { source: string; target: string };
}) {
  const total = s ? s.verified + s.unverified + s.review + s.problem : 0;
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={active}
      className={`flex min-w-0 flex-1 flex-col gap-2 rounded-xl border px-4 py-3 text-left transition-colors ${
        active ? "border-accent bg-accent-soft/50" : "border-border bg-surface hover:border-accent/60"
      }`}
    >
      <span className="flex flex-wrap items-center gap-x-2 text-sm font-semibold">
        <span>{p.source_db}</span>
        <span className="font-normal text-muted">→</span>
        <span>{p.target_db}</span>
      </span>
      {s ? (
        <>
          <span className="flex h-1.5 w-full gap-0.5 overflow-hidden rounded-full bg-surface-2">
            {BAR.map((b) => s[b.key] > 0 && <span key={b.key} className={b.className} style={{ flex: s[b.key] }} />)}
          </span>
          <span className="text-xs text-muted">
            {s.verified} of {total} {noun} verified
            {s.problem > 0 && <span className="text-bad"> · {s.problem} with problems</span>}
          </span>
        </>
      ) : (
        <span className="text-xs text-bad">
          {p.errors.length ? `Cannot read ${[...new Set(p.errors.map((e) => e.label))].join(" and ")}` : "Not read yet"}
        </span>
      )}
      <span className="sr-only">{labels.source} to {labels.target}</span>
    </button>
  );
}

function Summary({ p, s, noun, labels }: { p: PairView; s: PairSummary; noun: string; labels: { source: string; target: string } }) {
  const total = s.verified + s.unverified + s.review + s.problem;
  const legend: { key: TableStatus; count: number }[] = [
    { key: "problem", count: s.problem },
    { key: "review", count: s.review },
    { key: "unverified", count: s.unverified },
    { key: "verified", count: s.verified },
  ];
  return (
    <div className="rounded-xl border border-border bg-surface px-5 py-4">
      <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-2">
        <div className="flex items-baseline gap-2">
          <span className="num text-3xl font-semibold tracking-tight">{s.verified} of {total}</span>
          <span className="text-sm text-muted">{noun} verified identical</span>
        </div>
        <dl className="num flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted">
          <div><dt className="inline">Tables </dt><dd className="inline font-semibold text-text">{s.tables_source} in {labels.source} · {s.tables_target} in {labels.target}</dd></div>
          <div><dt className="inline">Rows </dt><dd className={`inline font-semibold ${s.rows_source !== s.rows_target ? "text-bad" : "text-text"}`}>{fmt(s.rows_source)} → {fmt(s.rows_target)}</dd></div>
        </dl>
      </div>
      <div className="mt-3 flex h-2.5 gap-0.5 overflow-hidden rounded-full bg-surface-2">
        {BAR.map((b) => s[b.key] > 0 && <span key={b.key} className={b.className} style={{ flex: s[b.key] }} />)}
      </div>
      <div className="mt-2.5 flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted">
        {legend.map((l) => (
          <span key={l.key} className="flex items-center gap-1.5">
            <Dot tone={STATUS[l.key].tone} label={STATUS[l.key].label} />
            <span className="num font-semibold text-text">{l.count}</span> {STATUS[l.key].label.toLowerCase()}
          </span>
        ))}
        {s.missing_in_target > 0 && <span className="font-medium text-bad">{s.missing_in_target} tables missing in {labels.target}</span>}
        {(s.missing_everywhere ?? 0) > 0 && (
          <span className="font-medium text-bad">{s.missing_everywhere} required tables in neither database</span>
        )}
        {s.only_in_target > 0 && <span>{s.only_in_target} tables only in {labels.target}</span>}
      </div>
      {(p.notes?.length ?? 0) > 0 && (
        <ul className="mt-3 flex flex-col gap-1 text-xs text-warn">{p.notes!.map((n, i) => <li key={i}>{n}</li>)}</ul>
      )}
    </div>
  );
}

function Checks({ t }: { t: TableRow }) {
  const items: { label: string; level: TableRow["checks"]["table"] }[] = [
    { label: "Table", level: t.checks.table },
    { label: "Columns", level: t.checks.columns },
    { label: "Rows", level: t.checks.rows },
    { label: "Values", level: t.checks.data },
  ];
  return (
    <span className="hidden shrink-0 items-center gap-2.5 text-[11px] text-muted md:flex">
      {items.map((i) => (
        <span key={i.label} className="flex items-center gap-1" title={`${i.label}: ${i.level === "none" ? "not checked" : i.level}`}>
          <Dot tone={LEVEL_TONE[i.level]} label={`${i.label} ${i.level}`} />{i.label}
        </span>
      ))}
    </span>
  );
}

const JOB_SCOPE: Record<string, string> = { required: "Required tables", all: "All tables", one: "One table" };

function JobBar({ job, counts, onRun, onCancel }: {
  job: Job | null;
  /** how many tables each button would check, across every database */
  counts: { required: number | null; all: number };
  onRun: (tables: Scope) => void;
  onCancel: () => void;
}) {
  if (job?.running) {
    const pct = job.total ? Math.round((job.done * 100) / job.total) : 0;
    const what = JOB_SCOPE[job.scope?.tables ?? "all"] ?? "Tables";
    return (
      <div className="flex min-w-0 flex-1 flex-wrap items-center justify-end gap-3">
        <div className="flex min-w-0 flex-col gap-1">
          <span className="h-1.5 w-48 overflow-hidden rounded-full bg-surface-2">
            <span className="block h-1.5 rounded-full bg-accent transition-all" style={{ width: `${pct}%` }} />
          </span>
          <span className="truncate text-xs text-muted">
            {what}: {job.total ? `${job.done} of ${job.total} checked` : "reading the table lists…"}
            {job.current && ` · now ${job.current}`}
          </span>
        </div>
        <button type="button" onClick={onCancel} disabled={job.cancelling}
          className="rounded-lg border border-border px-3 py-1.5 text-xs font-medium hover:border-bad hover:text-bad disabled:opacity-50">
          {job.cancelling ? "Stopping…" : "Stop"}
        </button>
      </div>
    );
  }
  return (
    <div className="flex flex-wrap items-center gap-2">
      {counts.required != null && (
        <button type="button" onClick={() => onRun("required")}
          title="Compare every row and value of the tables the migration uses (the source tables of the migration plan), in every database, in the background"
          className="rounded-lg bg-accent px-3.5 py-2 text-xs font-medium text-white hover:opacity-90 dark:text-bg">
          Check required tables <span className="num opacity-80">({counts.required})</span>
        </button>
      )}
      <button type="button" onClick={() => onRun("all")}
        title="Compare every row and value of every table in every database, in the background"
        className="rounded-lg border border-border px-3.5 py-2 text-xs font-medium hover:border-accent hover:text-accent">
        Check all tables <span className="num text-muted">({counts.all})</span>
      </button>
    </div>
  );
}

function ScopeSwitch({ scope, counts, onChange }: { scope: Scope; counts: { required: number; all: number }; onChange: (s: Scope) => void }) {
  const items: { key: Scope; label: string; hint: string }[] = [
    { key: "required", label: "Required tables", hint: "The tables the migration uses: the source tables of the migration plan" },
    { key: "all", label: "All tables", hint: "Every table of both databases" },
  ];
  return (
    <div role="tablist" aria-label="Tables" className="inline-flex rounded-lg border border-border bg-surface-2 p-0.5 text-xs">
      {items.map((i) => (
        <button key={i.key} type="button" role="tab" aria-selected={scope === i.key} title={i.hint} onClick={() => onChange(i.key)}
          className={`rounded-md px-3 py-1 font-medium ${scope === i.key ? "bg-surface text-accent shadow-sm" : "text-muted hover:text-text"}`}>
          {i.label} <span className="num">{counts[i.key]}</span>
        </button>
      ))}
    </div>
  );
}

export function AtnmPage() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [pairId, setPairId] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("attention");
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  const [scope, setScope] = useState<Scope>("required");
  const [log, setLog] = useState<LogEntry[]>([]);
  const lastDone = useRef<string>("");
  const logSeq = useRef(0);          // newest activity entry shown
  const logRun = useRef<string | null>(null);   // the run those entries belong to

  /** New activity entries of the run; a new run replaces the list. */
  const takeLog = useCallback((j: Job & { log?: LogEntry[] }) => {
    if (!j.log) return;
    // A new run, or a restarted backend (its numbering starts again), replaces the list.
    const fresh = j.started_at !== logRun.current || (j.log_seq ?? 0) < logSeq.current;
    logRun.current = j.started_at;
    const incoming = j.log;
    if (fresh) logSeq.current = 0;
    if (incoming.length) logSeq.current = Math.max(logSeq.current, incoming[incoming.length - 1].seq);
    setLog((old) => {
      if (fresh) return incoming.slice(-1000);
      // Two requests can return the same entries: keep only the ones newer than the list.
      const last = old.length ? old[old.length - 1].seq : 0;
      return [...old, ...incoming.filter((e) => e.seq > last)].slice(-1000);
    });
  }, []);

  const load = useCallback(async (refresh = false) => {
    setLoading(true);
    try {
      const [o, h] = await Promise.all([atnmApi.overview(refresh), refresh || !health ? atnmApi.health() : Promise.resolve(health)]);
      setOverview(o);
      setHealth(h);
      setJob(o.job);
      setLoadError(null);
      setVersion((v) => v + 1);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [health]);

  // First load, and the selection from the URL (?db=1&tables=all&table=dbo.x) so a view can be shared.
  useEffect(() => {
    Promise.resolve().then(() => {
      setPairId(param("db"));
      setOpen(param("table"));
      setScope(param("tables") === "all" ? "all" : "required");
      load();
      // The activity of the run in progress, or of the last one.
      atnmApi.status(0).then(takeLog).catch(() => undefined);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // While a check runs: follow its progress, and read the results again as tables finish.
  useEffect(() => {
    if (!job?.running) return;
    let busy = false;   // one poll at a time: a slow answer must not overlap the next tick
    const timer = setInterval(async () => {
      if (busy) return;
      busy = true;
      try {
        const j = await atnmApi.status(logSeq.current);
        setJob(j);
        takeLog(j);
        const mark = `${j.done}|${j.running}`;
        if (mark !== lastDone.current) {
          lastDone.current = mark;
          const o = await atnmApi.overview();
          setOverview(o);
          setVersion((v) => v + 1);
        }
      } catch {
        // keep polling; the next tick may succeed
      } finally {
        busy = false;
      }
    }, 2000);
    return () => clearInterval(timer);
  }, [job?.running, takeLog]);

  const pairs = overview?.pairs ?? [];
  const pair = pairs.find((p) => p.id === pairId) ?? pairs[0] ?? null;
  const labels = { source: overview?.source.label ?? "ATNM", target: overview?.target.label ?? "RDS" };

  function select(next: { db?: string; table?: string | null; tables?: Scope }) {
    const url = new URL(window.location.href);
    if (next.tables !== undefined) {
      if (next.tables === "all") url.searchParams.set("tables", "all");
      else url.searchParams.delete("tables");
      setScope(next.tables);
    }
    if (next.db !== undefined) {
      url.searchParams.set("db", next.db);
      url.searchParams.delete("table");
      setPairId(next.db);
      setOpen(null);
    }
    if (next.table !== undefined) {
      if (next.table) url.searchParams.set("table", next.table);
      else url.searchParams.delete("table");
      setOpen(next.table);
    }
    window.history.replaceState(null, "", url);
  }

  /** One table of the pair in view, or - with `tables` - every database's required or all tables. */
  async function run(opts: { table?: string; tables?: Scope }) {
    if (!pair) return;
    setActionError(null);
    try {
      const j = await atnmApi.check(opts.table ? { pair: pair.id, table: opts.table } : { tables: opts.tables });
      lastDone.current = "";
      setJob(j);
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    }
  }

  async function cancel() {
    try {
      setJob((await atnmApi.cancel()).job);
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    }
  }

  // Required tables when the backend knows them (the migration plan's source tables), else all.
  const view: Scope = scope === "required" && pair?.required?.count ? "required" : "all";
  const inView = useMemo(() => (pair?.tables ?? []).filter((t) =>
    view === "required" ? t.required : t.in_source || t.in_target), [pair, view]);
  const noun = view === "required" ? "required tables" : "tables";

  const counts = useMemo(() => {
    const c: Record<Filter, number> = { attention: 0, problem: 0, review: 0, unverified: 0, verified: 0, all: 0 };
    for (const t of inView) {
      c[t.status] += 1;
      c.all += 1;
      if (t.status === "problem" || t.status === "review") c.attention += 1;
    }
    return c;
  }, [inView]);

  // What each run button checks across every database: tables present on both sides.
  const checkable = (p: PairView, req: boolean) => p.tables.filter((t) => t.in_source && t.in_target && (!req || t.required)).length;
  const runCounts = {
    required: pairs.length && pairs.every((p) => p.required?.count) ? pairs.reduce((n, p) => n + checkable(p, true), 0) : null,
    all: pairs.reduce((n, p) => n + checkable(p, false), 0),
  };
  const scopeCounts = {
    required: pair?.required?.count ?? 0,
    all: (pair?.tables ?? []).filter((t) => t.in_source || t.in_target).length,
  };

  // With nothing to look at, the default filter would show an empty list: show all instead.
  const effective: Filter = filter === "attention" && counts.attention === 0 ? "all" : filter;
  const q = query.trim().toLowerCase();
  const shown = inView.filter((t) => {
    if (effective === "attention" && t.status !== "problem" && t.status !== "review") return false;
    if (effective !== "attention" && effective !== "all" && t.status !== effective) return false;
    return !q || t.key.includes(q);
  });

  const errors = pairs.flatMap((p) => p.errors);

  return (
    <div className="flex min-h-dvh flex-col">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-border bg-surface px-4 py-3">
        <div className="flex flex-wrap items-center gap-3">
          <span className="text-base font-semibold tracking-tight">Migration Validator</span>
          <SectionTabs current="atnm" />
        </div>
        <ServerChips health={health} overview={overview} />
      </header>

      <main className="mx-auto flex w-full max-w-6xl flex-col gap-4 px-4 py-5 sm:px-6">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="text-xl font-semibold tracking-tight">{labels.source} → {labels.target} copy check</h1>
            <p className="text-sm text-muted">
              Each {labels.source} database must be on {labels.target} unchanged: every table, every column, every row and every value.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button type="button" onClick={() => load(true)} disabled={loading}
              className="rounded-lg border border-border px-3 py-1.5 text-xs text-muted hover:text-text disabled:opacity-50"
              title="Read the table lists of every database again">
              {loading ? <Spinner small label="Reading…" /> : "↻ Refresh"}
            </button>
            {pair?.summary && (
              <a href={atnmApi.exportUrl(pair.id, view)} className="rounded-lg border border-border px-3 py-1.5 text-xs text-muted hover:text-text"
                title={`The ${noun} of this database with their results, as a spreadsheet`}>
                Download CSV
              </a>
            )}
          </div>
        </div>

        {loadError && <ErrorBox>Could not reach the backend: {loadError}</ErrorBox>}
        {!overview && !loadError && <Spinner label="Reading both servers…" />}
        {overview && pairs.length === 0 && (
          <ErrorBox>No database pairs are set up. Add ATNM_DB_1_SOURCE and ATNM_DB_1_TARGET to backend/.env.</ErrorBox>
        )}
        {errors.length > 0 && <Problem errors={errors} onRetry={() => load(true)} retrying={loading} />}

        {pairs.length > 0 && (
          <div className="flex flex-col gap-3 sm:flex-row">
            {pairs.map((p) => (
              <PairCard key={p.id} p={p} s={scoped(p, view)} noun={noun} active={p.id === pair?.id}
                onSelect={() => select({ db: p.id })} labels={labels} />
            ))}
          </div>
        )}

        {pair?.summary && (
          <>
            {pair.required && (
              <div className="flex flex-wrap items-center gap-3">
                <ScopeSwitch scope={view} counts={scopeCounts} onChange={(s) => select({ tables: s })} />
                <span className="text-xs text-muted">
                  {view === "required"
                    ? "The tables the migration uses: the source tables of the migration plan."
                    : "Every table of both databases, used by the migration or not."}
                </span>
              </div>
            )}
            {pair.required?.error && <ErrorBox>{pair.required.error}</ErrorBox>}
            <Summary p={pair} s={scoped(pair, view)!} noun={noun} labels={labels} />

            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex flex-wrap items-center gap-1.5">
                {FILTERS.map((f) => (
                  <button key={f.key} type="button" aria-pressed={effective === f.key} onClick={() => setFilter(f.key)}
                    className={`rounded-full border px-3 py-1 text-xs ${
                      effective === f.key ? "border-accent bg-accent-soft text-accent" : "border-border text-muted hover:text-text"
                    }`}>
                    {f.label} <span className="num">{counts[f.key]}</span>
                  </button>
                ))}
                <input type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Find a table"
                  className="ml-1 w-44 rounded-lg border border-border bg-surface px-3 py-1 text-xs outline-none placeholder:text-muted focus:border-accent" />
              </div>
              <JobBar job={job} counts={runCounts} onRun={(tables) => run({ tables })} onCancel={cancel} />
            </div>
            {actionError && <ErrorBox>{actionError}</ErrorBox>}
            {job && !job.running && job.error && <ErrorBox>The last check stopped: {job.error}</ErrorBox>}
            <RunLog job={job} entries={log} />

            <ul className="flex flex-col gap-2">
              {shown.length === 0 && <li className="py-8 text-center text-sm text-muted">No table matches.</li>}
              {shown.map((t) => {
                const st = STATUS[t.status];
                const isOpen = open === t.key;
                const stripe = { ok: "bg-ok", warn: "bg-warn", bad: "bg-bad", neutral: "bg-muted/40" }[st.tone];
                return (
                  <li key={t.key} className="overflow-hidden rounded-xl border border-border bg-surface">
                    <button type="button" onClick={() => select({ table: isOpen ? null : t.key })} aria-expanded={isOpen}
                      className="flex w-full items-center gap-3 px-3 py-2.5 text-left hover:bg-surface-2">
                      <span aria-hidden className={`w-1 self-stretch rounded-full ${stripe}`} />
                      <span className="min-w-0 flex-1">
                        <span className="flex items-center gap-2 truncate text-sm font-medium">
                          <span className="truncate"><span className="font-normal text-muted">{t.schema}.</span>{t.table}</span>
                          {view === "all" && t.required && (
                            <span className="shrink-0 rounded bg-accent-soft px-1.5 py-px text-[10px] font-medium text-accent"
                              title={`Used by the migration (mapping ${t.mapping})`}>Required</span>
                          )}
                        </span>
                        <span className="block truncate text-xs text-muted" title={t.reason}>{t.reason}</span>
                      </span>
                      <Checks t={t} />
                      <span className="num hidden w-44 shrink-0 text-right text-xs sm:block">
                        {fmt(t.rows.source)} <span className="text-muted">→</span>{" "}
                        <span className={t.in_source && t.in_target && t.rows.source !== t.rows.target ? "font-semibold text-bad" : ""}>{fmt(t.rows.target)}</span>
                      </span>
                      <Badge tone={st.tone}>{st.label}</Badge>
                      <span aria-hidden className="text-muted">{isOpen ? "▴" : "▾"}</span>
                    </button>
                    {isOpen && (
                      <TableDetail pair={pair.id} tableKey={t.key} labels={labels} job={job} version={version}
                        onCheck={(table) => run({ table })} />
                    )}
                  </li>
                );
              })}
            </ul>
            <p className="text-xs text-muted">
              Rows show {labels.source} → {labels.target}. Counts come from table metadata until the values are checked; then they are exact.
              Checks are read-only on both servers. Small tables take seconds; a table of tens of millions of rows takes about an hour.
            </p>
          </>
        )}
      </main>
    </div>
  );
}
