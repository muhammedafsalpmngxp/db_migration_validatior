"use client";

import { useCallback, useEffect, useState } from "react";
import { dataApi, type DataCheckJob, type DataCheckSummary } from "@/lib/api";
import { DATA_STATUS } from "./DataCheck";
import { Badge, Card, ErrorBox, Stat } from "./ui";

export type DataResults = Record<string, DataCheckSummary>;

/** Loads the saved data check results, and polls while a background run is going. */
export function useDataResults() {
  const [results, setResults] = useState<DataResults>({});
  const [job, setJob] = useState<DataCheckJob | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Each tick reads the results again; while a run is going the next tick is 2s away.
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    dataApi.all()
      .then((d) => {
        if (!live) return;
        setResults(d.results);
        setJob(d.job);
        setError(null);
        if (d.job.running) timer = setTimeout(() => setTick((t) => t + 1), 2000);
      })
      .catch((e) => live && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      live = false;
      if (timer) clearTimeout(timer);
    };
  }, [tick]);

  const runAll = useCallback(async () => {
    try {
      const r = await dataApi.runAll();
      setJob(r.job);
      setTick((t) => t + 1);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  return { results, job, error, runAll };
}

type Named = { id: string; label: string; ref: string };

function List({ title, tone, items, results, onSelect, detail }: {
  title: string;
  tone: "ok" | "bad" | "warn";
  items: Named[];
  results: DataResults;
  onSelect: (ref: string) => void;
  detail: boolean;
}) {
  if (!items.length) return null;
  return (
    <div>
      <h3 className="mb-1.5 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-muted">
        <Badge tone={tone}>{items.length}</Badge> {title}
      </h3>
      {detail ? (
        <ul className="flex flex-col gap-1">
          {items.map((x) => (
            <li key={x.id}>
              <button type="button" onClick={() => onSelect(x.ref)}
                className="flex w-full flex-wrap items-baseline gap-x-2 rounded-md px-2 py-1 text-left text-sm hover:bg-surface-2">
                <span className="font-medium">{x.label}</span>
                <span className="text-xs text-muted">
                  {results[x.id]?.status === "problems" && `${results[x.id].errors} problem${results[x.id].errors === 1 ? "" : "s"} · `}
                  {results[x.id]?.status === "review" && `${results[x.id].reviews} to review · `}
                  {results[x.id]?.headline}
                </span>
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <ul className="flex flex-wrap gap-1.5">
          {items.map((x) => (
            <li key={x.id}>
              <button type="button" onClick={() => onSelect(x.ref)}
                className="rounded-md border border-border px-2 py-0.5 text-xs hover:border-accent hover:text-accent">
                {x.label}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function DataOverview({ mappings, data, onSelect }: {
  mappings: Named[];
  data: ReturnType<typeof useDataResults>;
  onSelect: (ref: string) => void;
}) {
  const { results, job, error, runAll } = data;
  const by = (st: string) => mappings.filter((m) => results[m.id]?.status === st);
  const checked = mappings.filter((m) => results[m.id]).length;
  const running = !!job?.running;
  const last = Object.values(results).map((r) => r.checked_at).sort().pop();

  return (
    <Card
      title={<>Data check <span className="font-normal text-muted">· are the values the same?</span></>}
      aside={
        <div className="flex items-center gap-3">
          {running ? (
            <span className="flex items-center gap-2 text-xs text-muted">
              <span className="h-1.5 w-28 rounded-full bg-surface-2">
                <span className="block h-1.5 rounded-full bg-accent" style={{ width: `${job!.total ? (job!.done * 100) / job!.total : 0}%` }} />
              </span>
              {job!.done}/{job!.total} · {job!.current}
            </span>
          ) : last ? (
            <span className="text-xs text-muted">last run {new Date(last).toLocaleString()}</span>
          ) : null}
          <button type="button" onClick={runAll} disabled={running}
            className="rounded-lg bg-accent px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50 dark:text-bg"
            title="Compare every value of every mapping, and check that every link points at the right row, in the background (a few minutes)">
            {running ? "Checking…" : checked ? "Re-run all data checks" : "Run all data checks"}
          </button>
        </div>
      }
    >
      {error && <ErrorBox>{error}</ErrorBox>}
      {!checked && !running ? (
        <p className="text-sm text-muted">
          No data check yet. Run all to compare every row and value of each mapping — rows missing or extra, values lost,
          changed or recoded, and NULLs per column. It runs read-only inside SQL Server and takes a few minutes.
        </p>
      ) : (
        <div className="flex flex-col gap-4">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Stat label="Values identical" value={by("identical").length} tone="ok" />
            <Stat label="Data problems" value={by("problems").length + by("error").length} tone="bad" />
            <Stat label="Needs review" value={by("review").length} tone="warn" hint="Values match, but a recoding rule or case change needs a human to confirm it" />
            <Stat label="Not checked" value={mappings.length - checked + by("skipped").length} tone="neutral" hint="Transform, excluded, too large, or locked" />
          </div>
          <List title={DATA_STATUS.problems.label} tone="bad" items={[...by("problems"), ...by("error")]} results={results} onSelect={onSelect} detail />
          <List title={DATA_STATUS.review.label} tone="warn" items={by("review")} results={results} onSelect={onSelect} detail />
          <List title={DATA_STATUS.identical.label} tone="ok" items={by("identical")} results={results} onSelect={onSelect} detail={false} />
        </div>
      )}
    </Card>
  );
}
