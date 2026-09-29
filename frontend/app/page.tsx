"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, fmt, fmtSize, type CompareResult, type Health, type Scope } from "@/lib/api";
import { ColumnComparison, ColumnList } from "@/components/ColumnComparison";
import { DataCheck } from "@/components/DataCheck";
import { KeysView } from "@/components/KeysView";
import { MappingView } from "@/components/MappingView";
import { Overview } from "@/components/Overview";
import { Sidebar } from "@/components/Sidebar";
import { ErrorBox, SideTag, Spinner } from "@/components/ui";

function tableFromUrl() {
  return new URLSearchParams(window.location.search).get("table");
}

function DatabaseBar({ health, scope }: { health: Health | null; scope: Scope | null }) {
  const dbs = health?.databases ?? scope?.databases.map((d) => ({ ...d, ok: true, error: null })) ?? [];
  return (
    <ol className="flex flex-wrap items-center gap-1.5 text-xs">
      {dbs.map((d, i) => (
        <li key={d.side} className="flex items-center gap-1.5">
          {i === 1 && <span aria-hidden className="text-muted">+</span>}
          {i === 2 && <span aria-hidden className="text-muted">→</span>}
          <span title={d.error ?? `${d.role} database`} className="flex items-center gap-1.5 rounded-md border border-border bg-surface px-2 py-1">
            <span className={`h-1.5 w-1.5 rounded-full ${!health ? "bg-muted/50" : d.ok ? "bg-ok" : "bg-bad"}`} />
            <SideTag side={d.side} />
            <span className="font-medium">{d.name}</span>
          </span>
        </li>
      ))}
    </ol>
  );
}

export default function Home() {
  const [scope, setScope] = useState<Scope | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [result, setResult] = useState<CompareResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const mainRef = useRef<HTMLElement>(null);
  const request = useRef(0);

  const load = useCallback(async (ref: string | null) => {
    setSelected(ref);
    setError(null);
    mainRef.current?.scrollTo({ top: 0 });
    if (!ref) {
      setResult(null);
      return;
    }
    const id = ++request.current;
    setLoading(true);
    try {
      const r = await api.compare(ref);
      if (id === request.current) setResult(r);
    } catch (e) {
      if (id === request.current) {
        setResult(null);
        setError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      if (id === request.current) setLoading(false);
    }
  }, []);

  const navigate = useCallback((ref: string | null) => {
    const url = new URL(window.location.href);
    if (ref) url.searchParams.set("table", ref);
    else url.searchParams.delete("table");
    window.history.pushState(null, "", url);
    load(ref);
  }, [load]);

  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth(null));
    api.scope().then(setScope).catch((e) => setLoadError(e.message));
    // The URL is the source of truth for the selection; read it once the page is mounted.
    Promise.resolve().then(() => load(tableFromUrl()));
    const onPop = () => load(tableFromUrl());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, [load]);

  async function refresh() {
    setRefreshing(true);
    try {
      setScope(await api.scope(true));
      if (selected) await load(selected);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e));
    } finally {
      setRefreshing(false);
    }
  }

  const sel = result?.selected;
  const readAt = scope ? new Date(scope.read_at * 1000).toLocaleTimeString() : null;

  return (
    <div className="flex min-h-dvh flex-col lg:h-dvh lg:overflow-hidden">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-border bg-surface px-4 py-3">
        <div className="flex items-baseline gap-3">
          <button type="button" onClick={() => navigate(null)} className="text-base font-semibold tracking-tight hover:text-accent">
            Migration Validator
          </button>
          {readAt && <span className="text-xs text-muted" title="Row counts and columns are read from the databases">Live · read {readAt}</span>}
        </div>
        <DatabaseBar health={health} scope={scope} />
      </header>

      <div className="grid min-h-0 flex-1 lg:grid-cols-[320px_minmax(0,1fr)]">
        {scope ? (
          <Sidebar
            scope={scope}
            selected={selected}
            onSelect={navigate}
            onOverview={() => navigate(null)}
            onRefresh={refresh}
            refreshing={refreshing}
          />
        ) : (
          <aside className="border-border p-4 lg:border-r">
            {loadError ? <ErrorBox>Could not reach the backend: {loadError}</ErrorBox> : <Spinner label="Reading the databases…" />}
          </aside>
        )}

        <main ref={mainRef} className="min-w-0 overflow-y-auto">
          <div className="mx-auto flex w-full max-w-6xl flex-col gap-4 px-4 py-5 sm:px-6">
            {error && <ErrorBox>{error}</ErrorBox>}

            {!selected && scope && <Overview scope={scope} onSelect={navigate} />}

            {selected && (
              <nav aria-label="Breadcrumb" className="text-xs text-muted">
                <button type="button" onClick={() => navigate(null)} className="hover:text-accent">Overview</button>
                <span aria-hidden> / </span>
                <span>{selected.split(".").slice(1).join(".")}</span>
              </nav>
            )}

            {selected && loading && !result && <Spinner label="Reading tables and columns…" />}

            {selected && result && sel && (
              <div className={`flex flex-col gap-4 transition-opacity ${loading ? "opacity-50" : ""}`}>
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                  <h1 className="text-xl font-semibold tracking-tight">
                    <span className="font-normal text-muted">{sel.database}.{sel.schema}.</span>{sel.table}
                  </h1>
                  <SideTag side={sel.side} />
                  <span className="num text-sm text-muted">
                    {fmt(sel.rows)} rows · {sel.columns ?? "—"} columns · {fmtSize(sel.size_kb)}
                  </span>
                  {loading && <Spinner small />}
                </div>

                {sel.locked && (
                  <p className="rounded-lg border border-warn/30 bg-warn-soft px-4 py-3 text-sm text-warn">
                    This table is locked by another session (probably a load in progress), so its row count
                    and columns cannot be read right now. Press Refresh in a moment.
                  </p>
                )}

                <MappingView key={`${sel.ref}|${scope?.read_at}`} result={result} onSelect={navigate} />

                <DataCheck key={`data|${result.mapping.id}`} mappingId={result.mapping.id} mappingType={result.mapping.type} />

                <KeysView key={`keys|${result.mapping.id}|${scope?.read_at}`} targets={result.mapping.targets} />

                {result.mapping.type === "excluded" ? (
                  <ColumnList key={sel.ref} columns={result.source_columns} title={`Columns · ${sel.schema}.${sel.table}`} />
                ) : (
                  <ColumnComparison
                    key={sel.ref}
                    comparisons={result.comparisons}
                    sourceLabel={`${sel.schema}.${sel.table}`}
                    transform={result.mapping.type === "transform"}
                  />
                )}
              </div>
            )}
          </div>
        </main>
      </div>
    </div>
  );
}
