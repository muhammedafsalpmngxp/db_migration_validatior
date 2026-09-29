"use client";

import { useEffect, useState } from "react";
import { fmt, keymapApi, type KeyBucket, type KeyLink, type KeyMapping, type KeyRows } from "@/lib/api";
import { Badge, ErrorBox, Spinner, type Tone } from "./ui";

// Key mapping check (backend/app/keymap.py): does every foreign key of the target point at
// the row that holds the source value?

export const KEY_STATUS: Record<KeyMapping["status"], { tone: Tone; label: string }> = {
  ok: { tone: "ok", label: "✓ Links correct" },
  review: { tone: "warn", label: "! Review" },
  problems: { tone: "bad", label: "✕ Problems" },
  not_checked: { tone: "neutral", label: "Not checked" },
  none: { tone: "neutral", label: "No links" },
  skipped: { tone: "neutral", label: "Skipped" },
  error: { tone: "bad", label: "Check failed" },
};

const VERDICT: Record<KeyLink["verdict"], { tone: Tone; label: string }> = {
  ok: { tone: "ok", label: "✓ correct" },
  review: { tone: "warn", label: "! review" },
  problem: { tone: "bad", label: "✕ problem" },
  not_checked: { tone: "neutral", label: "not checked" },
};

export const BUCKET: Record<KeyBucket, { label: string; tone: Tone; hint: string }> = {
  correct: { label: "correct", tone: "ok", hint: "The id points at the row that holds the source value" },
  case_only: {
    label: "written differently", tone: "info",
    hint: "The right row, but the value is written differently (e.g. upper/lower case): the list keeps one spelling",
  },
  both_empty: { label: "both empty", tone: "neutral", hint: "No value in the source and no id in the target" },
  not_filled: { label: "not filled", tone: "bad", hint: "The source value is in the list, but the id is empty" },
  not_in_list: { label: "not in the list", tone: "warn", hint: "The source value is not in the list, so there is no id to give" },
  wrong: { label: "wrong id", tone: "bad", hint: "The id points at a row with a different value" },
  added: { label: "id but source empty", tone: "warn", hint: "The target has an id although the source value is empty" },
  orphan: { label: "id not in the list", tone: "bad", hint: "The id does not exist in the list" },
  old_broken: { label: "old id not in old list", tone: "warn", hint: "The source id points at no row of the old list" },
};

// Problems first (shown only when there are any), then the rows that are fine.
const ORDER: KeyBucket[] = ["wrong", "not_filled", "orphan", "not_in_list", "added", "old_broken", "correct", "case_only", "both_empty"];
const PROBLEMS: KeyBucket[] = ["not_filled", "not_in_list", "wrong", "added", "orphan", "old_broken"];

const CHIP: Record<Tone, string> = {
  ok: "bg-ok-soft text-ok",
  warn: "bg-warn-soft text-warn",
  bad: "bg-bad-soft text-bad",
  info: "bg-info-soft text-info",
  accent: "bg-accent-soft text-accent",
  neutral: "bg-surface-2 text-muted",
};

function when(iso: string | null | undefined) {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? iso : d.toLocaleString();
}

function pairedOn(r: KeyMapping) {
  if (r.method === "key" && r.key) return <>rows paired on <span className="font-mono">{r.key.source} = {r.key.target}</span></>;
  if (r.method === "columns" && r.rows) {
    return <>no unique key: rows paired on {r.rows.anchor_columns} columns with the same values ({fmt(r.rows.matched)} of {fmt(r.rows.source)} rows)</>;
  }
  return null;
}

/** The mapping check's overall result, shown at the top of the Keys card. */
export function MappingSummary({ result, loaded, running, error }: {
  result: KeyMapping | null;
  loaded: boolean;
  running: boolean;
  error: string | null;
}) {
  const st = result ? KEY_STATUS[result.status] ?? KEY_STATUS.not_checked : null;
  return (
    <div className="flex flex-col gap-1.5 rounded-lg border border-border bg-surface-2/60 px-3 py-2">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="text-xs font-semibold uppercase tracking-wide text-muted">Mapping check</span>
        {running ? (
          <Spinner small label="Comparing every link with the source rows… this can take up to a minute." />
        ) : !loaded ? (
          <Spinner small />
        ) : result && st ? (
          <>
            <Badge tone={st.tone}>{st.label}</Badge>
            <span className="min-w-0 flex-1">{result.headline}</span>
          </>
        ) : (
          <span className="text-xs text-muted">
            Not run yet. <strong className="font-medium">Check mapping</strong> tests that every link column points at the
            row holding the source value (for example New_Crew_code → crew_type_id through the crew list).
          </span>
        )}
      </div>
      {result && !running && (
        <p className="text-[11px] text-muted">
          {pairedOn(result)}{pairedOn(result) ? " · " : ""}checked {when(result.checked_at)} · {result.seconds}s · read live
        </p>
      )}
      {error && <ErrorBox>{error}</ErrorBox>}
    </div>
  );
}

/** One link's result, shown under its foreign key row. */
export function LinkResult({ link, onOpen }: { link: KeyLink; onOpen: (filter: string) => void }) {
  if (link.verdict === "not_checked" || !link.buckets) {
    return (
      <p className="text-xs text-muted">
        <span className="font-medium">Mapping:</span> not checked — {link.reason}
      </p>
    );
  }
  const b = link.buckets;
  const v = VERDICT[link.verdict];
  const problems = PROBLEMS.reduce((n, k) => n + b[k], 0);
  const cov = link.coverage;
  return (
    <div className="flex flex-col gap-1.5 text-xs">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <Badge tone={v.tone} title={link.summary}>{v.label}</Badge>
        <span className="text-muted">Made from</span>
        <span className="font-mono text-[12px]">{link.source}</span>
        <span aria-hidden className="text-muted">→</span>
        <span className="font-mono text-[12px]">{link.column}</span>
        <span className="text-muted">· checked through <span className="font-mono">{link.via}</span></span>
        {link.found_by === "values" && cov && (
          <Badge tone="info" title={`No source column is paired with ${link.column}; ${link.source} was found because ${fmt(cov.found)} of its ${fmt(cov.distinct)} different values are in the list.`}>
            found by its values
          </Badge>
        )}
        {link.mode === "meaning" && (
          <Badge tone="info" title="The id was copied as it is; the old and new lists are compared through a label both keep">
            old list = new list
          </Badge>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-1.5">
        {ORDER.filter((k) => b[k] > 0).map((k) => (
          <button
            key={k}
            type="button"
            onClick={() => onOpen(k)}
            title={`${BUCKET[k].hint}. Click to see the rows.`}
            className={`num rounded-full px-2 py-0.5 font-medium hover:opacity-80 ${CHIP[BUCKET[k].tone]}`}
          >
            {fmt(b[k])} {BUCKET[k].label}
          </button>
        ))}
        <button type="button" onClick={() => onOpen(problems ? "problems" : "all")} className="ml-1 text-accent hover:underline">
          See rows
        </button>
      </div>
      {link.findings.length > 0 && (
        <ul className="flex flex-col gap-0.5 leading-5">
          {link.findings.map((f, i) => (
            <li key={i} className={f.severity === "error" ? "text-bad" : f.severity === "review" ? "text-warn" : "text-text/80"}>
              <span aria-hidden className="mr-1">{f.severity === "error" ? "✕" : f.severity === "review" ? "!" : "·"}</span>
              {f.text}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Value({ v }: { v: string | null }) {
  if (v === null) return <i className="text-muted">empty</i>;
  return <span className="whitespace-pre-wrap break-all font-mono text-[13px]">{v}</span>;
}

const PAGE = 50;

/** The paired rows behind one link: source value, target id, and the value that id points at. */
export function KeyRowsPanel({ mapping, link, initialFilter, onClose }: {
  mapping: string;
  link: KeyLink;
  initialFilter: string;
  onClose: () => void;
}) {
  const [filter, setFilter] = useState(initialFilter);
  const [page, setPage] = useState(0);
  const [reveal, setReveal] = useState(false);
  const [data, setData] = useState<KeyRows | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    Promise.resolve().then(() => { if (live) { setLoading(true); setError(null); } });
    keymapApi.rows({ mapping, column: link.column, filter, page, size: PAGE, reveal })
      .then((d) => { if (live) setData(d); })
      .catch((e) => { if (live) { setData(null); setError(e instanceof Error ? e.message : String(e)); } })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [mapping, link.column, filter, page, reveal]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const b = link.buckets!;
  const problems = PROBLEMS.reduce((n, k) => n + b[k], 0);
  const filters: { id: string; label: string; count: number }[] = [
    ...(problems ? [{ id: "problems", label: "All problems", count: problems }] : []),
    ...ORDER.filter((k) => b[k] > 0).map((k) => ({ id: k, label: BUCKET[k].label, count: b[k] })),
    { id: "all", label: "All rows", count: link.compared ?? 0 },
  ];
  const total = data?.total ?? 0;
  const from = total ? page * PAGE + 1 : 0;
  const to = Math.min(total, (page + 1) * PAGE);
  const meaning = link.mode === "meaning";
  const rowHeader = data?.method === "key" && data.key ? `Key · ${data.key.target}` : "Row";

  return (
    <div className="fixed inset-0 z-50 flex justify-end" role="dialog" aria-modal="true" aria-label={`Rows of ${link.column}`}>
      <button type="button" aria-label="Close" onClick={onClose} className="absolute inset-0 bg-black/30" />
      <div className="relative flex h-full w-full max-w-5xl flex-col border-l border-border bg-surface shadow-2xl">
        <header className="flex flex-wrap items-start justify-between gap-3 border-b border-border px-5 py-4">
          <div className="min-w-0">
            <p className="text-xs uppercase tracking-wide text-muted">Mapping check · rows read live now</p>
            <h2 className="truncate font-mono text-base font-semibold">{link.source} → {link.column}</h2>
            <p className="mt-0.5 text-xs text-muted">
              Each source value next to the id in the new table and the value that id points at ({link.via}).
              {data?.method === "columns" && " No column is unique on both sides, so rows are paired on their other columns."}
            </p>
          </div>
          <button type="button" onClick={onClose} className="rounded-md border border-border px-2.5 py-1 text-sm hover:border-accent hover:text-accent">
            Close
          </button>
        </header>

        <div className="flex flex-col gap-2 border-b border-border px-5 py-3">
          <div className="flex flex-wrap gap-1.5">
            {filters.map((f) => (
              <button key={f.id} type="button" aria-pressed={filter === f.id} onClick={() => { setFilter(f.id); setPage(0); }}
                className={`rounded-full border px-2.5 py-0.5 text-xs ${filter === f.id ? "border-accent bg-accent-soft text-accent" : "border-border text-muted hover:text-text"}`}>
                {f.label} <span className="num">{fmt(f.count)}</span>
              </button>
            ))}
          </div>
          {data?.sensitive && (
            <div className="flex items-center gap-2 rounded-lg bg-warn-soft px-3 py-1.5 text-xs text-warn">
              These columns look like contact details, so the values are {data.hidden ? "hidden" : "shown"}.
              <button type="button" onClick={() => setReveal(!reveal)} className="font-medium underline">
                {data.hidden ? "Show values" : "Hide values"}
              </button>
            </div>
          )}
        </div>

        <div className="min-h-0 flex-1 overflow-auto">
          {error && <div className="p-5"><ErrorBox>{error}</ErrorBox></div>}
          {loading && !data && <div className="p-5"><Spinner label="Reading rows…" /></div>}
          {data && (
            <table className={`w-full text-sm ${loading ? "opacity-50" : ""}`}>
              <thead className="sticky top-0 bg-surface-2 text-left text-xs text-muted">
                <tr>
                  <th className="px-4 py-2 font-medium">{rowHeader}</th>
                  {meaning && <th className="py-2 pr-3 font-medium">Old id · {link.source}</th>}
                  <th className="py-2 pr-3 font-medium">{meaning ? `Old value · ${link.old?.label}` : `Source · ${link.source}`}</th>
                  <th className="py-2 pr-3 font-medium">Target id · {link.column}</th>
                  <th className="py-2 pr-3 font-medium">Value of that id · {link.label}</th>
                  <th className="py-2 pr-4 font-medium">Result</th>
                </tr>
              </thead>
              <tbody>
                {data.rows.length === 0 && (
                  <tr><td colSpan={meaning ? 6 : 5} className="px-4 py-10 text-center text-muted">No rows.</td></tr>
                )}
                {data.rows.map((r, i) => (
                  <tr key={`${r.row}|${i}`} className={`border-t border-border align-top ${BUCKET[r.status]?.tone === "bad" ? "bg-bad-soft/30" : ""}`}>
                    <td className="num px-4 py-1.5 font-mono text-xs text-muted">{r.row ?? <i>—</i>}</td>
                    {meaning && <td className="py-1.5 pr-3"><Value v={r.old} /></td>}
                    <td className="py-1.5 pr-3"><Value v={r.source} /></td>
                    <td className="num py-1.5 pr-3"><Value v={r.target_id} /></td>
                    <td className="py-1.5 pr-3"><Value v={r.target_value} /></td>
                    <td className="py-1.5 pr-4"><Badge tone={BUCKET[r.status]?.tone ?? "neutral"}>{BUCKET[r.status]?.label ?? r.status}</Badge></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        {data && total > 0 && (
          <footer className="flex items-center justify-between gap-2 border-t border-border px-5 py-2.5 text-xs">
            <span className="num text-muted">{fmt(from)}–{fmt(to)} of {fmt(total)} rows</span>
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
