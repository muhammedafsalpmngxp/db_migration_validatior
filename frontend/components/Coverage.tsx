"use client";

import { useState } from "react";
import { coverageApi, fmt, type CoverageDb } from "@/lib/api";
import { Card, ErrorBox, SideTag, Spinner } from "./ui";

/** The tables of each database that no mapping of the plan mentions (read on demand). */
export function Coverage() {
  const [data, setData] = useState<{ databases: CoverageDb[]; ignore: string[] } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      setData(await coverageApi.get());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  return (
    <Card
      title={<>Coverage <span className="font-normal text-muted">· tables no mapping mentions</span></>}
      aside={
        <button type="button" onClick={load} disabled={loading}
          className="rounded-lg border border-border px-3 py-1.5 text-xs font-medium hover:border-accent hover:text-accent disabled:opacity-50">
          {loading ? <Spinner small label="Reading…" /> : data ? "Read again" : "Show"}
        </button>
      }
    >
      {error && <ErrorBox>{error}</ErrorBox>}
      {!data ? (
        <p className="text-sm text-muted">
          Lists every table of each database that the migration plan does not mention, so nothing is left out without
          anyone noticing. Reads only the table lists.
        </p>
      ) : (
        <div className="flex flex-col gap-3">
          {data.databases.map((d) => (
            <div key={d.side} className="rounded-lg border border-border p-3 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <SideTag side={d.side} />
                <span className="font-medium">{d.name}</span>
                {d.error ? <span className="text-xs text-warn">{d.error}</span> : (
                  <span className="text-xs text-muted">
                    {fmt(d.tables)} tables · {fmt(d.in_plan)} in the plan · <span className={d.not_in_plan.length ? "text-warn" : ""}>{fmt(d.not_in_plan.length)} not in the plan</span>
                    {d.ignored ? ` · ${fmt(d.ignored)} ignored (COVERAGE_IGNORE)` : ""}
                  </span>
                )}
                {d.not_in_plan.length > 0 && (
                  <button type="button" onClick={() => setOpen(open === d.side ? null : d.side)} className="ml-auto text-xs text-accent hover:underline">
                    {open === d.side ? "Hide" : "List them"}
                  </button>
                )}
              </div>
              {open === d.side && (
                <ul className="mt-2 grid max-h-64 grid-cols-1 gap-x-4 overflow-y-auto text-xs sm:grid-cols-2">
                  {d.not_in_plan.map((t) => (
                    <li key={`${t.schema}.${t.table}`} className="flex justify-between gap-2 border-b border-border py-0.5">
                      <span className="truncate font-mono">{t.schema}.{t.table}</span>
                      <span className="num text-muted">{t.rows == null ? "—" : `${fmt(t.rows)} rows`}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          ))}
          <p className="text-xs text-muted">
            Tables can be left out of this list with COVERAGE_IGNORE in backend/.env (e.g. dbo.awsdms_*).
            {data.ignore.length > 0 && ` Now ignored: ${data.ignore.join(", ")}.`}
          </p>
        </div>
      )}
    </Card>
  );
}
