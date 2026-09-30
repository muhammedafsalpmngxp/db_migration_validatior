"use client";

import { useEffect, useRef, useState } from "react";
import { fmt } from "@/lib/api";
import { Dot } from "../ui";
import type { Job, LogEntry } from "./api";

const LEVEL_TONE = { info: "neutral", ok: "ok", warn: "warn", error: "bad" } as const;

/** The current time, ticking every second while `live`. */
function useNow(live: boolean) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!live) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [live]);
  return now;
}

function since(iso: string | null | undefined, now: number) {
  if (!iso) return null;
  const s = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ${String(s % 60).padStart(2, "0")} s`;
  return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min`;
}

const time = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

/** What the check is doing now, and everything it has done in this run. */
export function RunLog({ job, entries }: { job: Job | null; entries: LogEntry[] }) {
  const running = !!job?.running;
  const now = useNow(running);
  const [open, setOpen] = useState(true);
  const list = useRef<HTMLOListElement>(null);
  const pinned = useRef(true);     // follow new entries unless the reader scrolled up

  useEffect(() => {
    const el = list.current;
    if (el && pinned.current) el.scrollTop = el.scrollHeight;
  }, [entries, open]);

  if (!running && entries.length === 0) return null;
  const results = Object.entries(job?.results ?? {}).filter(([, n]) => n);

  return (
    <section className="rounded-xl border border-border bg-surface">
      <header className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-2.5">
        <h2 className="flex items-center gap-2 text-sm font-semibold">
          {running && <span className="h-2 w-2 animate-pulse rounded-full bg-accent" aria-hidden />}
          {running ? "Checking now" : "Last run"}
          <span className="num text-xs font-normal text-muted">
            {job?.total ? `${job.done} of ${job.total} tables` : ""}
            {running && job?.started_at ? ` · running for ${since(job.started_at, now)}` : ""}
            {!running && job?.finished_at ? ` · finished ${new Date(job.finished_at).toLocaleString()}` : ""}
          </span>
        </h2>
        <div className="flex items-center gap-3 text-xs text-muted">
          {results.map(([k, n]) => <span key={k} className="num">{n} {k}</span>)}
          <button type="button" onClick={() => setOpen(!open)} className="text-accent hover:underline">
            {open ? "Hide activity" : `Show activity (${entries.length})`}
          </button>
        </div>
      </header>

      {running && (
        <div className="flex flex-col gap-1 border-b border-border bg-accent-soft/40 px-4 py-3">
          {job?.current ? (
            <p className="text-sm">
              <span className="text-muted">Table </span>
              <span className="font-semibold">{job.current}</span>
              {job.database && <span className="text-muted"> in {job.database}</span>}
              {job.table_rows != null && <span className="num text-muted"> · about {fmt(job.table_rows)} rows</span>}
              {job.table_started_at && <span className="num text-muted"> · on it for {since(job.table_started_at, now)}</span>}
            </p>
          ) : null}
          {job?.step && (
            <p className="text-sm">
              <span className="text-muted">Now: </span>{job.step}
              {job.step_started_at && <span className="num text-muted"> · {since(job.step_started_at, now)}</span>}
            </p>
          )}
          {job?.cancelling && <p className="text-xs text-warn">Stopping: cancelling the queries on both servers…</p>}
        </div>
      )}

      {open && (
        <ol
          ref={list}
          onScroll={(e) => {
            const el = e.currentTarget;
            pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
          }}
          className="max-h-72 overflow-y-auto px-4 py-2 font-mono text-xs"
        >
          {entries.map((e) => (
            <li key={e.seq} className="flex items-start gap-2 py-0.5">
              <span className="num shrink-0 text-muted">{time(e.at)}</span>
              <span className="pt-1"><Dot tone={LEVEL_TONE[e.level]} label={e.level} /></span>
              <span className={`min-w-0 ${e.level === "error" ? "text-bad" : e.level === "warn" ? "text-warn" : ""}`}>
                {e.table && <span className="font-semibold">{e.table}: </span>}
                {!e.table && e.database && <span className="font-semibold">{e.database}: </span>}
                {e.text}
              </span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
