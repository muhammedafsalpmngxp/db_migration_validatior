"use client";

import { useEffect, useState, type ReactNode } from "react";
import { fmt } from "@/lib/api";
import { Badge, Dot, ErrorBox, Spinner } from "../ui";
import {
  atnmApi, LEVEL_TONE, type ColumnCompare, type ConstraintSummary, type DataResult, type Job, type TableDetail as Detail,
} from "./api";

const COLUMN_STATUS: Record<ColumnCompare["status"], { label: string; tone: "ok" | "warn" | "bad" | "accent" | "info" }> = {
  same: { label: "Same", tone: "ok" },
  missing: { label: "Missing in RDS", tone: "bad" },
  extra: { label: "Only in RDS", tone: "accent" },
  changed: { label: "Changed", tone: "warn" },
  renamed: { label: "Renamed", tone: "info" },
};

const CONSTRAINT_STATUS = {
  same: { label: "Kept", tone: "ok" },
  missing: { label: "Not in RDS", tone: "bad" },
  changed: { label: "Changed", tone: "warn" },
  extra: { label: "Only in RDS", tone: "accent" },
} as const;

const FINDING_TONE = { ok: "ok", info: "neutral", review: "warn", problem: "bad", error: "bad" } as const;

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="rounded-lg bg-surface-2 px-3 py-2">
      <div className="text-[11px] text-muted">{label}</div>
      <div className="num mt-0.5 text-sm font-semibold">{children}</div>
    </div>
  );
}

function Columns({ columns, labels }: { columns: ColumnCompare[]; labels: { source: string; target: string } }) {
  const different = columns.filter((c) => c.status !== "same");
  const [all, setAll] = useState(different.length === 0);
  const shown = all ? columns : different;
  return (
    <section>
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-muted">
          Columns · {different.length ? `${different.length} differ` : "all the same"}
        </h4>
        <button type="button" onClick={() => setAll(!all)} className="text-xs text-accent hover:underline">
          {all ? "Show only differences" : `Show all ${columns.length} columns`}
        </button>
      </div>
      {shown.length === 0 ? (
        <p className="text-sm text-muted">Every column has the same name, type and nullability on both sides.</p>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full min-w-[640px] text-sm">
            <thead className="bg-surface-2 text-left text-xs text-muted">
              <tr>
                <th className="px-3 py-2 font-medium">Column</th>
                <th className="px-3 py-2 font-medium">{labels.source} type</th>
                <th className="px-3 py-2 font-medium">{labels.target} type</th>
                <th className="px-3 py-2 font-medium">Empty allowed</th>
                <th className="px-3 py-2 font-medium">Result</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((c) => (
                <tr key={c.name} className="border-t border-border align-top">
                  <td className="px-3 py-2 font-medium">
                    {c.name}
                    {c.status === "renamed" && c.target && <span className="font-normal text-muted"> → {c.target.name}</span>}
                  </td>
                  <td className="px-3 py-2 font-mono text-xs">{c.source?.type ?? "—"}</td>
                  <td className={`px-3 py-2 font-mono text-xs ${c.source && c.target && c.source.type !== c.target.type ? "text-bad" : ""}`}>
                    {c.target?.type ?? "—"}
                  </td>
                  <td className="px-3 py-2 text-xs text-muted">
                    {c.source ? (c.source.nullable ? "yes" : "no") : "—"} → {c.target ? (c.target.nullable ? "yes" : "no") : "—"}
                  </td>
                  <td className="px-3 py-2">
                    <Badge tone={COLUMN_STATUS[c.status].tone}>{COLUMN_STATUS[c.status].label}</Badge>
                    {c.notes.length > 0 && c.status !== "missing" && c.status !== "extra" && (
                      <div className="mt-1 text-xs text-muted">{c.notes.join("; ")}</div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

/** Primary, unique and foreign keys, default values and check rules: kept in RDS or not. */
function Constraints({ c }: { c: ConstraintSummary | null | undefined }) {
  const [all, setAll] = useState(false);
  if (c === undefined) return null;
  const notKept = c ? c.rows.filter((r) => r.status === "missing" || r.status === "changed") : [];
  const shown = c ? (all ? c.rows : notKept) : [];
  const title = c === null ? "could not be read" : c.rows.length === 0 ? "none on either side"
    : notKept.length ? `${notKept.length} not kept in RDS` : `all ${c.total} kept`;
  return (
    <section>
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-muted">Keys and rules · {title}</h4>
        {c && c.rows.length > 0 && (
          <button type="button" onClick={() => setAll(!all)} className="text-xs text-accent hover:underline">
            {all ? "Show only differences" : `Show all ${c.rows.length}`}
          </button>
        )}
      </div>
      {c === null ? (
        <p className="text-sm text-muted">
          The keys and rules could not be read this time (another session held the catalog). Refresh to try again.
        </p>
      ) : shown.length === 0 ? (
        <p className="text-sm text-muted">
          {c.rows.length === 0
            ? "Neither table has a primary key, unique key, foreign key, default value or check rule."
            : "Every primary key, unique key, foreign key, default value and check rule of ATNM is kept in RDS."}
        </p>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full min-w-[560px] text-sm">
            <thead className="bg-surface-2 text-left text-xs text-muted">
              <tr>
                <th className="px-3 py-2 font-medium">Kind</th>
                <th className="px-3 py-2 font-medium">What</th>
                <th className="px-3 py-2 font-medium">Result</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((r, i) => (
                <tr key={i} className="border-t border-border align-top">
                  <td className="whitespace-nowrap px-3 py-2 text-xs text-muted">{r.kind}</td>
                  <td className="px-3 py-2 font-mono text-xs">{r.what}</td>
                  <td className="px-3 py-2">
                    <Badge tone={CONSTRAINT_STATUS[r.status].tone}>{CONSTRAINT_STATUS[r.status].label}</Badge>
                    {r.note && <div className="mt-1 text-xs text-muted">{r.note}</div>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

type Empty = { nulls: number; blanks: number | null } | null;

/** NULL and blank values of every column on both servers, from the last data check. */
function EmptyValues({ profile, labels }: {
  profile: NonNullable<DataResult["profile"]>;
  labels: { source: string; target: string };
}) {
  const [all, setAll] = useState(false);
  const count = (e: Empty) => (e ? e.nulls + (e.blanks ?? 0) : null);
  const differ = profile.columns.filter((c) => c.source && c.target && count(c.source) !== count(c.target));
  const shown = all ? profile.columns : differ;
  const cell = (e: Empty) => (e ? `${fmt(e.nulls)} NULL${e.blanks ? ` · ${fmt(e.blanks)} blank` : ""}` : "—");
  return (
    <section>
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-muted">
          Empty values · {differ.length ? `${differ.length} columns differ` : "the same in every shared column"}
        </h4>
        <button type="button" onClick={() => setAll(!all)} className="text-xs text-accent hover:underline">
          {all ? "Show only differences" : `Show all ${profile.columns.length} columns`}
        </button>
      </div>
      {shown.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full min-w-[480px] text-sm">
            <thead className="bg-surface-2 text-left text-xs text-muted">
              <tr>
                <th className="px-3 py-2 font-medium">Column</th>
                <th className="px-3 py-2 text-right font-medium">{labels.source} ({fmt(profile.rows.source)} rows)</th>
                <th className="px-3 py-2 text-right font-medium">{labels.target} ({fmt(profile.rows.target)} rows)</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((c) => {
                const off = c.source && c.target && count(c.source) !== count(c.target);
                return (
                  <tr key={c.name} className="border-t border-border">
                    <td className="px-3 py-2 font-medium">{c.name}</td>
                    <td className="num px-3 py-2 text-right text-xs">{cell(c.source)}</td>
                    <td className={`num px-3 py-2 text-right text-xs ${off ? "text-bad" : ""}`}>{cell(c.target)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function RowsTable({ rows }: { rows: Record<string, string | null>[] }) {
  const cols = Object.keys(rows[0] ?? {});
  return (
    <div className="overflow-x-auto rounded-lg border border-border">
      <table className="text-xs">
        <thead className="bg-surface-2 text-left text-muted">
          <tr>{cols.map((c) => <th key={c} className="whitespace-nowrap px-2 py-1.5 font-medium">{c}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-t border-border">
              {cols.map((c) => (
                <td key={c} className="max-w-56 truncate whitespace-nowrap px-2 py-1.5" title={r[c] ?? "empty"}>
                  {r[c] ?? <span className="text-muted">empty</span>}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Values({ data, labels }: { data: DataResult | null; labels: { source: string; target: string } }) {
  if (!data) {
    return <p className="text-sm text-muted">The values of this table have not been checked yet.</p>;
  }
  const ex = data.examples ?? {};
  const d = data.diff;
  return (
    <div className="flex flex-col gap-3">
      <p className="text-xs text-muted">
        Checked {new Date(data.checked_at).toLocaleString()} in {data.seconds}s
        {data.method?.key ? ` · rows matched on ${data.method.key_kind} ${data.method.key.join(", ")}` : " · no key: rows compared whole"}
        {data.compared_columns ? ` · ${data.compared_columns} columns compared` : ""}
      </p>
      <ul className="flex flex-col gap-1.5">
        {data.findings.map((f, i) => (
          <li key={i} className="flex items-start gap-2 text-sm">
            <span className="pt-1.5"><Dot tone={FINDING_TONE[f.severity]} label={f.severity} /></span>
            <span>{f.text}</span>
          </li>
        ))}
      </ul>

      {d && (d.missing != null || d.only_in_source != null) && (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          {d.missing != null ? (
            <>
              <Fact label={`Missing in ${labels.target}`}>{fmt(d.missing)}</Fact>
              <Fact label={`Extra in ${labels.target}`}>{fmt(d.extra ?? 0)}</Fact>
              <Fact label="Values changed">{fmt(d.changed ?? 0)}</Fact>
              <Fact label="Groups read row by row">{d.groups_examined} of {d.groups_differing}</Fact>
            </>
          ) : (
            <>
              <Fact label={`Only in ${labels.source}`}>{fmt(d.only_in_source ?? 0)}</Fact>
              <Fact label={`Only in ${labels.target}`}>{fmt(d.only_in_target ?? 0)}</Fact>
              <Fact label="Groups read row by row">{d.groups_examined} of {d.groups_differing}</Fact>
            </>
          )}
        </div>
      )}

      {(ex.changed?.length ?? 0) > 0 && (
        <div>
          <h5 className="mb-1.5 text-xs font-semibold text-muted">Rows with changed values (examples)</h5>
          <div className="flex flex-col gap-2">
            {ex.changed!.map((c) => (
              <div key={c.key} className="rounded-lg border border-border p-2 text-xs">
                <div className="mb-1 font-medium">Key {c.key}</div>
                <table className="w-full">
                  <tbody>
                    {c.columns.map((col) => (
                      <tr key={col.name} className="align-top">
                        <td className="w-40 py-0.5 pr-2 text-muted">{col.name}</td>
                        <td className="py-0.5 pr-2">{col.source ?? <span className="text-muted">empty</span>}</td>
                        <td className="py-0.5 pr-2 text-muted">→</td>
                        <td className="py-0.5 text-bad">{col.target ?? <span className="text-muted">empty</span>}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ))}
          </div>
        </div>
      )}
      {(ex.missing_keys?.length ?? 0) > 0 && (
        <p className="text-xs"><span className="font-semibold text-muted">Missing in {labels.target}, e.g. keys </span>{ex.missing_keys!.join(", ")}</p>
      )}
      {(ex.extra_keys?.length ?? 0) > 0 && (
        <p className="text-xs"><span className="font-semibold text-muted">Extra in {labels.target}, e.g. keys </span>{ex.extra_keys!.join(", ")}</p>
      )}
      {(ex.only_in_source_rows?.length ?? 0) > 0 && (
        <div>
          <h5 className="mb-1.5 text-xs font-semibold text-muted">Rows in {labels.source} with no identical row in {labels.target} (examples)</h5>
          <RowsTable rows={ex.only_in_source_rows!} />
        </div>
      )}
      {(ex.only_in_target_rows?.length ?? 0) > 0 && (
        <div>
          <h5 className="mb-1.5 text-xs font-semibold text-muted">Rows in {labels.target} with no identical row in {labels.source} (examples)</h5>
          <RowsTable rows={ex.only_in_target_rows!} />
        </div>
      )}
    </div>
  );
}

export function TableDetail({ pair, tableKey, labels, job, version, onCheck }: {
  pair: string;
  tableKey: string;
  labels: { source: string; target: string };
  job: Job | null;
  /** changes whenever results may have changed, so the detail is read again */
  version: number;
  onCheck: (table: string) => void;
}) {
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    atnmApi.table(pair, tableKey)
      .then((d) => { if (live) { setDetail(d); setError(null); } })
      .catch((e) => live && setError(e instanceof Error ? e.message : String(e)));
    return () => { live = false; };
  }, [pair, tableKey, version]);

  if (error) return <div className="p-4"><ErrorBox>{error}</ErrorBox></div>;
  if (!detail) return <div className="p-4"><Spinner small label="Reading the table…" /></div>;

  const t = detail.table;
  const running = !!job?.running;
  const checkingThis = running && job?.scope?.table === tableKey && job.scope.pairs.includes(pair);
  const bothSides = t.in_source && t.in_target;

  return (
    <div className="flex flex-col gap-5 border-t border-border px-4 py-4">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Fact label={`Rows in ${labels.source}`}>{fmt(t.rows.source)}</Fact>
        <Fact label={`Rows in ${labels.target}`}>
          <span className={t.rows.source !== t.rows.target && bothSides ? "text-bad" : ""}>{fmt(t.rows.target)}</span>
        </Fact>
        <Fact label="Row count">{t.rows.exact ? "Exact count" : "Table metadata"}</Fact>
        <Fact label="Row key">{t.row_key ? t.row_key.columns.join(", ") : "None"}</Fact>
      </div>

      {(t.reasons?.length ?? 0) > 0 && (
        <ul className="flex flex-col gap-1">
          {t.reasons!.map((r, i) => (
            <li key={i} className="flex items-start gap-2 text-sm">
              <span className="pt-1.5"><Dot tone={LEVEL_TONE[r.severity]} label={r.severity} /></span>{r.text}
            </li>
          ))}
        </ul>
      )}

      {bothSides && <Columns key={t.key} columns={detail.columns} labels={labels} />}

      {bothSides && <Constraints key={`c|${t.key}`} c={t.constraints} />}

      {bothSides && (
        <section>
          <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
            <h4 className="text-xs font-semibold uppercase tracking-wide text-muted">Values</h4>
            <button
              type="button"
              onClick={() => onCheck(tableKey)}
              disabled={running}
              title={running && !checkingThis ? "Another check is running" : "Compare every row and value of this table on both servers"}
              className="rounded-lg border border-border px-3 py-1.5 text-xs font-medium hover:border-accent hover:text-accent disabled:opacity-50"
            >
              {checkingThis ? <Spinner small label="Checking…" /> : detail.data ? "Check again" : "Check this table"}
            </button>
          </div>
          {detail.data && t.data?.stale && (
            <p className="mb-2 rounded-lg bg-warn-soft px-3 py-2 text-xs text-warn">
              {t.data.stale_reason ?? "The table changed after this check was made."} The result below is out of date: check again.
            </p>
          )}
          {t.data?.last_attempt && (
            <p className="mb-2 rounded-lg bg-bad-soft px-3 py-2 text-xs text-bad">
              The last check ({new Date(t.data.last_attempt.checked_at).toLocaleString()}) could not finish:{" "}
              {t.data.last_attempt.headline} The result below is the last one that was measured.
            </p>
          )}
          {t.data?.cutoff && (
            <p className="mb-2 rounded-lg bg-surface-2 px-3 py-2 text-xs text-muted">
              Compared only the rows with {t.data.cutoff.column} up to {t.data.cutoff.value} (cutoff), on both servers.
            </p>
          )}
          {t.live && (
            <p className="mb-2 rounded-lg bg-warn-soft px-3 py-2 text-xs text-warn">
              This table is live: its row count is still moving
              {t.live.source && ` in ${labels.source} (${fmt(t.live.source.rows_then)} → ${fmt(t.live.source.rows_now)} in ${t.live.source.minutes} min)`}
              {t.live.source && t.live.target && " and"}
              {t.live.target && ` in ${labels.target} (${fmt(t.live.target.rows_then)} → ${fmt(t.live.target.rows_now)} in ${t.live.target.minutes} min)`}.
              A check is a snapshot of one moment; for sign-off, compare at a cutoff or while the copy is paused.
            </p>
          )}
          <Values data={detail.data} labels={labels} />
        </section>
      )}

      {bothSides && detail.data?.profile && <EmptyValues key={`e|${t.key}`} profile={detail.data.profile} labels={labels} />}
    </div>
  );
}
