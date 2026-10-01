"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { fmt, type Column, type ColumnRow, type ColumnSummary, type Comparison } from "@/lib/api";
import { Badge, ErrorBox, Spinner } from "../ui";
import { renamesApi, type Decision, type NamedCheck, type RenameJob, type RenameResult, type TargetResult } from "./api";

/** A column comparison row as shown: the backend's row, or a rename the data proved. */
export type DisplayRow = Omit<ColumnRow, "match"> & {
  match: ColumnRow["match"] | "data";
  /** the data's verdict on this pair */
  evidence?: { verdict: "verified" | "possible" | "not_supported"; text: string; rows: number; identical: number };
  /** a source column with no partner: the closest target column the data found, not confirmed */
  suggestion?: Decision;
};

/** The saved rename check of a mapping, a way to run it, and its progress. */
export function useRenames(mappingId: string | undefined) {
  const [result, setResult] = useState<RenameResult | null>(null);
  const [job, setJob] = useState<RenameJob | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [available, setAvailable] = useState(true);
  const seen = useRef(0);

  const load = useCallback(async () => {
    if (!mappingId) return;
    try {
      const r = await renamesApi.saved(mappingId);
      setResult(r.result);
      setJob(r.job);
      setError(null);
    } catch (e) {
      const text = e instanceof Error ? e.message : String(e);
      if (text.startsWith("404")) setAvailable(false);     // a backend without the rename check
      else setError(text);
    }
  }, [mappingId]);

  useEffect(() => {
    Promise.resolve().then(load);
  }, [load]);

  // While a check runs, follow it; read the result again when it ends.
  useEffect(() => {
    if (!job?.running) return;
    let busy = false;
    const t = setInterval(async () => {
      if (busy) return;
      busy = true;
      try {
        const j = await renamesApi.status(seen.current);
        if (j.log?.length) seen.current = j.log[j.log.length - 1].seq;
        setJob(j);
        if (!j.running) await load();
      } catch {
        // try again next tick
      } finally {
        busy = false;
      }
    }, 2000);
    return () => clearInterval(t);
  }, [job?.running, load]);

  const run = useCallback(async () => {
    if (!mappingId) return;
    setError(null);
    try {
      setJob(await renamesApi.run(mappingId));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [mappingId]);

  const cancel = useCallback(async () => {
    try {
      setJob((await renamesApi.cancel()).job);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  return { result, job, error, available, run, cancel };
}

export function targetResult(result: RenameResult | null, comparison: Comparison): TargetResult | null {
  if (!result || result.status !== "done") return null;
  const name = comparison.target.toLowerCase();
  return result.targets.find((t) => t.target.toLowerCase() === name) ?? null;
}

function diffs(s: Column, t: Column): ColumnRow["diffs"] {
  const out: ColumnRow["diffs"] = [];
  if (s.type.toLowerCase() !== t.type.toLowerCase()) out.push("type");
  if (s.nullable !== t.nullable) out.push("nullable");
  if (s.pk !== t.pk) out.push("pk");
  return out;
}

const pct = (r: number) => `${(Math.floor(r * 1000) / 10).toFixed(1)}%`;

/** The comparison's rows with the proven renames joined up and the rest annotated. Without
 *  a result the backend's rows are returned unchanged. */
export function mergeRows(comparison: Comparison, tr: TargetResult | null): { rows: DisplayRow[]; summary: ColumnSummary } {
  const rows: DisplayRow[] = comparison.rows.map((r) => ({ ...r }));
  if (!tr || !comparison.summary) return { rows, summary: comparison.summary! };
  const named = new Map<string, NamedCheck>(tr.named.map((n) => [`${n.source}|${n.target}`.toLowerCase(), n]));
  for (const r of rows) {
    if (r.source && r.target) {
      const n = named.get(`${r.source.name}|${r.target.name}`.toLowerCase());
      if (n) {
        r.evidence = {
          verdict: n.verdict, rows: n.rows_checked, identical: n.identical,
          text: n.verdict === "verified" ? `The data agrees: ${n.reason}`
            : `The data does not support this pairing: ${n.reason}`,
        };
      }
    }
  }
  const out: DisplayRow[] = [];
  const takenTargets = new Set<string>();
  const verified = new Map(tr.decisions.filter((d) => d.verdict === "verified").map((d) => [d.source.toLowerCase(), d]));
  const suggestions = new Map(tr.decisions.filter((d) => d.verdict !== "verified").map((d) => [d.source.toLowerCase(), d]));
  for (const d of verified.values()) takenTargets.add(d.target.toLowerCase());
  const byTarget = new Map(rows.filter((r) => r.status === "target_only" && r.target).map((r) => [r.target!.name.toLowerCase(), r]));
  for (const r of rows) {
    if (r.status === "target_only" && r.target && takenTargets.has(r.target.name.toLowerCase())) continue;
    if (r.status === "source_only" && r.source) {
      const d = verified.get(r.source.name.toLowerCase());
      const t = d ? byTarget.get(d.target.toLowerCase()) : undefined;
      if (d && t?.target) {
        const df = diffs(r.source, t.target);
        out.push({
          source: r.source, target: t.target, match: "data", diffs: df, status: df.length ? "changed" : "renamed",
          evidence: { verdict: "verified", rows: d.rows_checked, identical: d.identical,
            text: `Renamed: identical on all ${fmt(d.rows_checked)} paired rows (${tr.paired_on}).` },
        });
        continue;
      }
      const s = suggestions.get(r.source.name.toLowerCase());
      if (s) r.suggestion = s;
    }
    out.push(r);
  }
  const summary: ColumnSummary = { ...comparison.summary, match: 0, renamed: 0, changed: 0, source_only: 0, target_only: 0 };
  for (const r of out) summary[r.status] += 1;
  return { rows: out, summary };
}

/** The "Paired by" cell of a row the rename check has something to say about. */
export function Evidence({ row }: { row: DisplayRow }) {
  if (row.match === "data") {
    return (
      <span title={row.evidence?.text} className="font-medium text-ok">
        verified by data · 100%
        <span className="block text-[11px] font-normal text-muted">{fmt(row.evidence?.rows)} rows</span>
      </span>
    );
  }
  if (row.suggestion) {
    const s = row.suggestion;
    return (
      <span title={s.reason} className="text-warn">
        {s.verdict === "ambiguous" ? "ambiguous" : `possible · ${pct(s.rate)}`}
        <span className="block font-mono text-[11px]">→ {s.target}</span>
      </span>
    );
  }
  return null;
}

/** After the name of the method in the "Paired by" cell: what the data says about that pair. */
export function EvidenceNote({ row }: { row: DisplayRow }) {
  if (!row.evidence || row.match === "data") return null;
  const good = row.evidence.verdict === "verified";
  return (
    <span title={row.evidence.text} className={`block text-[11px] ${good ? "text-ok" : "text-bad"}`}>
      {good ? "data agrees · 100%" : `data disagrees · ${pct(row.evidence.rows ? row.evidence.identical / row.evidence.rows : 0)}`}
    </span>
  );
}

function ago(iso: string) {
  const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return `${s} s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  return new Date(iso).toLocaleString();
}

/** The button, the state of the last check, and what it could not confirm. */
export function RenamePanel({ state, tr }: { state: ReturnType<typeof useRenames>; tr: TargetResult | null }) {
  const { result, job, error, available, run, cancel } = state;
  if (!available) return null;
  const running = !!job?.running;
  const mine = running && job?.current === result?.mapping;
  const review = tr?.decisions.filter((d) => d.verdict !== "verified") ?? [];
  const verified = tr?.decisions.filter((d) => d.verdict === "verified") ?? [];
  const disagree = tr?.named.filter((n) => n.verdict !== "verified") ?? [];
  const yaml = review.filter((d) => d.verdict === "possible").map((d) => `${JSON.stringify(d.source)}: ${d.target}`);

  return (
    <section className="flex flex-col gap-3 rounded-lg border border-border bg-surface-2/50 px-4 py-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-sm">
          <span className="font-semibold">Renamed columns, checked on the data</span>
          <span className="ml-2 text-xs text-muted">
            {result?.checked_at ? `last checked ${ago(result.checked_at)}` : "not checked yet"}
            {result?.stale && " · the columns changed since: check again"}
          </span>
        </div>
        {running ? (
          <span className="flex items-center gap-3">
            <Spinner small label={job?.waiting ? `Waiting for ${job.waiting}…` : mine ? job?.step ?? "Checking…" : `Checking ${job?.current ?? "…"}`} />
            <button type="button" onClick={cancel} className="rounded-lg border border-border px-3 py-1 text-xs hover:border-bad hover:text-bad">Stop</button>
          </span>
        ) : (
          <button type="button" onClick={run}
            title="Pair the rows, then compare every column without a partner with every target column that can hold the same values, on all paired rows. Only a 100% match is confirmed."
            className="rounded-lg border border-accent px-3 py-1.5 text-xs font-medium text-accent hover:bg-accent-soft">
            {result ? "Check renames again" : "Find renamed columns by data"}
          </button>
        )}
      </div>
      {error && <ErrorBox>{error}</ErrorBox>}
      {result && result.status !== "done" && <p className="text-xs text-warn">{result.headline}</p>}
      {result?.last_attempt && <p className="text-xs text-bad">The last check could not finish: {result.last_attempt.headline}</p>}
      {tr && (
        <>
          <p className="text-xs text-muted">
            {tr.paired_on ? <>Rows paired on {tr.paired_on}. </> : null}
            {tr.rows?.paired != null && (
              <span className="num">
                {fmt(tr.rows.paired)} rows paired ({pct(tr.rows.coverage ?? 0)} of the smaller table)
                {(tr.rows.unpaired_source ?? 0) > 0 && ` · ${fmt(tr.rows.unpaired_source)} source rows have no target row`}
                {(tr.rows.unpaired_target ?? 0) > 0 && ` · ${fmt(tr.rows.unpaired_target)} target rows have no source row`}
                {" "}(rows on one side only do not count against a rename; the data check reports them).
              </span>
            )}
            {" "}{tr.headline}
          </p>
          {verified.length > 0 && (
            <p className="text-xs">
              <Badge tone="ok">{verified.length} verified</Badge>{" "}
              {verified.map((d) => `${d.source} → ${d.target}`).join(" · ")}
            </p>
          )}
          {review.length > 0 && (
            <div className="flex flex-col gap-2">
              <h4 className="text-xs font-semibold text-muted">Not confirmed: a person decides</h4>
              <ul className="flex flex-col gap-1.5 text-xs">
                {review.map((d) => (
                  <li key={`${d.source}|${d.target}`} className="rounded-md border border-border bg-surface px-3 py-2">
                    <span className="font-mono">{d.source} → {d.target}</span>{" "}
                    <Badge tone="warn">{d.verdict === "ambiguous" ? "ambiguous" : `${pct(d.rate)} ${d.full ? "of all paired rows" : "of the sample"}`}</Badge>
                    <span className="mt-0.5 block text-muted">{d.reason}</span>
                    {(d.examples?.length ?? 0) > 0 && (
                      <span className="mt-1 block font-mono text-[11px] text-muted">
                        {d.examples!.map((e, i) => (
                          <span key={i} className="block">
                            {e.row != null && `row ${e.row}: `}{e.source ?? "empty"} → {e.target ?? "empty"}
                          </span>
                        ))}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
              {yaml.length > 0 && (
                <p className="text-[11px] text-muted">
                  To accept one after review, add it to this mapping in <code>backend/mappings/migration_plan.yaml</code>:{" "}
                  <code className="font-mono">columns: {"{"}{yaml.join(", ")}{"}"}</code>
                </p>
              )}
            </div>
          )}
          {disagree.length > 0 && (
            <p className="text-xs text-bad">
              Name pairings the data does not support: {disagree.map((n) => `${n.source} → ${n.target} (${pct(n.rate)})`).join(" · ")}
            </p>
          )}
        </>
      )}
    </section>
  );
}
