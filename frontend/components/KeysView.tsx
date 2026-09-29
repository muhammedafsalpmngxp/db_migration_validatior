"use client";

import { Fragment, useEffect, useState } from "react";
import {
  fmt, keymapApi, keysApi, type ForeignKey, type ForeignKeyCheck, type KeyLink, type KeyMapping, type LinkedTable,
  type Member, type TableKeys,
} from "@/lib/api";
import { KeyRowsPanel, LinkResult, MappingSummary } from "./KeyMapping";
import { Badge, Card, ErrorBox, SideTag, Spinner } from "./ui";

type Check = { state: "loading" } | { state: "done"; data: ForeignKeyCheck } | { state: "error"; message: string };

/** The mapping check (does each link point at the right row?) of the table, when it has one. */
type MappingState = {
  mappingId: string;
  result: KeyMapping | null;
  loaded: boolean;
  running: boolean;
  error: string | null;
  run: () => void;
};

function TableName({ t }: { t: LinkedTable }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <SideTag side="T" />
      <span><span className="text-muted">{t.schema}.</span>{t.table}</span>
      {!t.in_plan && <span className="text-[10px] text-muted" title="Not a target of the migration plan">(reference)</span>}
    </span>
  );
}

function Cols({ names }: { names: string[] }) {
  return <span className="font-mono text-[13px]">{names.join(", ")}</span>;
}

function FkState({ fk }: { fk: ForeignKey }) {
  if (!fk.enabled) return <Badge tone="bad" title="The constraint is disabled: values are not checked">disabled</Badge>;
  if (!fk.trusted) return <Badge tone="warn" title="Enabled, but existing rows were never checked (WITH NOCHECK)">not trusted</Badge>;
  return <Badge tone="ok" title="Enabled and trusted: every value is guaranteed to exist in the referenced table">enforced</Badge>;
}

function Usage({ check, fk }: { check?: Check; fk: ForeignKey }) {
  if (!check) return <span className="text-muted">—</span>;
  if (check.state === "loading") return <Spinner small />;
  if (check.state === "error") return <span className="text-bad" title={check.message}>failed</span>;
  const d = check.data;
  return (
    <div className="num flex flex-col gap-0.5 text-xs" title={`${d.seconds}s`}>
      <span><strong className="font-medium">{fmt(d.filled_rows)}</strong> of {fmt(d.child_rows)} rows set · {fmt(d.null_rows)} NULL</span>
      {d.distinct_values != null && (
        <span><strong className="font-medium">{fmt(d.distinct_values)}</strong> of {fmt(fk.referenced.rows)} {fk.referenced.table} rows used</span>
      )}
      <span className={d.orphan_rows ? "font-medium text-bad" : "text-ok"}>
        {d.orphan_rows ? `${fmt(d.orphan_rows)} orphan rows (value missing in ${fk.referenced.table})` : "no orphans"}
      </span>
    </div>
  );
}

function TargetKeys({ target, mapping }: { target: Member; mapping?: MappingState }) {
  const [data, setData] = useState<TableKeys | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [checks, setChecks] = useState<Record<string, Check>>({});
  const [rows, setRows] = useState<{ link: KeyLink; filter: string } | null>(null);

  useEffect(() => {
    let live = true;
    keysApi.keys(target.ref)
      .then((d) => live && setData(d))
      .catch((e) => live && setError(e instanceof Error ? e.message : String(e)));
    return () => { live = false; };
  }, [target.ref]);

  async function check(fk: ForeignKey) {
    setChecks((c) => ({ ...c, [fk.name]: { state: "loading" } }));
    try {
      const r = await keysApi.check(target.ref, fk.name);
      setChecks((c) => ({ ...c, [fk.name]: { state: "done", data: r } }));
    } catch (e) {
      setChecks((c) => ({ ...c, [fk.name]: { state: "error", message: e instanceof Error ? e.message : String(e) } }));
    }
  }

  const title = <>Keys <span className="font-normal text-muted">· {target.schema}.{target.table}</span></>;
  if (error) return <Card title={title}><ErrorBox>{error}</ErrorBox></Card>;
  if (!data) return <Card title={title}><Spinner label="Reading keys…" /></Card>;
  if (data.locked) {
    return (
      <Card title={title}>
        <p className="text-sm text-warn">The table is locked by another session, so its keys cannot be read right now.</p>
      </Card>
    );
  }

  const outgoing = data.foreign_keys.filter((f) => f.direction === "outgoing");
  const incoming = data.foreign_keys.filter((f) => f.direction === "incoming");
  const pk = data.keys.find((k) => k.kind === "primary");
  const checking = Object.values(checks).some((c) => c.state === "loading");
  const showMapping = !!mapping && outgoing.length > 0;
  // A column can carry the same key twice (two identical constraints): one result, shown once.
  const firstFk = new Map<string, string>();
  for (const f of outgoing) {
    const c = f.columns.length === 1 ? f.columns[0].toLowerCase() : null;
    if (c && !firstFk.has(c)) firstFk.set(c, f.name);
  }
  const linkOf = (fk: ForeignKey) =>
    fk.columns.length === 1
      ? mapping?.result?.links?.find((l) => l.column.toLowerCase() === fk.columns[0].toLowerCase())
      : undefined;
  const repeatOf = (fk: ForeignKey) => fk.columns.length === 1 && firstFk.get(fk.columns[0].toLowerCase()) !== fk.name;

  return (
    <Card
      title={title}
      aside={outgoing.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          {showMapping && (
            <button
              type="button"
              disabled={mapping.running}
              onClick={mapping.run}
              className="rounded-lg bg-accent px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50 dark:text-bg"
              title="For every link column: find the source column it was made from and check, row by row, that the id points at the row holding the source value"
            >
              {mapping.running ? "Checking…" : mapping.result ? "Check mapping again" : "Check mapping"}
            </button>
          )}
          <button
            type="button"
            disabled={checking}
            onClick={() => outgoing.forEach(check)}
            className="rounded-lg border border-border px-3 py-1.5 text-xs font-medium hover:border-accent hover:text-accent disabled:opacity-50"
            title="Count, for each foreign key, how many rows use it and whether any value is missing from the referenced table"
          >
            {checking ? <Spinner small label="Checking…" /> : "Check references"}
          </button>
        </div>
      )}
    >
      <div className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="text-xs font-semibold uppercase tracking-wide text-muted">Primary key</span>
          {pk ? (
            <span className="inline-flex items-center gap-2 rounded-md border border-border px-2 py-1">
              <span aria-hidden>🔑</span><Cols names={pk.columns} />
              <span className="text-xs text-muted">{pk.name} · {pk.index}</span>
            </span>
          ) : (
            <Badge tone="warn" title="Rows cannot be matched one to one without a key">No primary key</Badge>
          )}
          {data.keys.filter((k) => k.kind === "unique").map((k) => (
            <span key={k.name} className="inline-flex items-center gap-2 rounded-md border border-border px-2 py-1">
              <span className="text-xs text-muted">unique</span><Cols names={k.columns} />
            </span>
          ))}
        </div>

        {showMapping && (
          <MappingSummary result={mapping.result} loaded={mapping.loaded} running={mapping.running} error={mapping.error} />
        )}

        <div>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
            Foreign keys <span className="font-normal normal-case tracking-normal">· this table → referenced table ({outgoing.length})</span>
          </h3>
          {outgoing.length === 0 ? (
            <p className="text-sm text-muted">This table references no other table.</p>
          ) : (
            <div className="overflow-x-auto rounded-lg border border-border">
              <table className="w-full min-w-[820px] text-sm">
                <thead className="bg-surface-2 text-left text-xs text-muted">
                  <tr>
                    <th className="px-3 py-2 font-medium">Column</th>
                    <th className="py-2 pr-3 font-medium">References</th>
                    <th className="py-2 pr-3 text-right font-medium">Referenced rows</th>
                    <th className="py-2 pr-3 font-medium">Constraint</th>
                    <th className="py-2 pr-3 font-medium">Usage (exact)</th>
                    <th className="py-2 pr-3" />
                  </tr>
                </thead>
                <tbody>
                  {outgoing.map((fk) => {
                    const link = linkOf(fk);
                    const repeat = repeatOf(fk);
                    return (
                      <Fragment key={fk.name}>
                        <tr className="border-t border-border align-top">
                          <td className="px-3 py-2"><Cols names={fk.columns} /></td>
                          <td className="py-2 pr-3">
                            <TableName t={fk.referenced} />
                            <div className="mt-0.5 pl-7 text-xs text-muted">. <Cols names={fk.ref_columns} /></div>
                          </td>
                          <td className="num py-2 pr-3 text-right font-medium">{fmt(fk.referenced.rows)}</td>
                          <td className="py-2 pr-3">
                            <FkState fk={fk} />
                            <div className="mt-1 max-w-56 truncate text-[11px] text-muted" title={`${fk.name} · on delete ${fk.on_delete} · on update ${fk.on_update}`}>{fk.name}</div>
                          </td>
                          <td className="py-2 pr-3"><Usage check={checks[fk.name]} fk={fk} /></td>
                          <td className="py-2 pr-3 text-right">
                            <button
                              type="button"
                              onClick={() => check(fk)}
                              disabled={checks[fk.name]?.state === "loading"}
                              className="text-xs text-accent hover:underline disabled:opacity-50"
                            >
                              Check
                            </button>
                          </td>
                        </tr>
                        {link && !mapping?.running && (
                          <tr className="align-top">
                            <td colSpan={6} className="px-3 pb-3">
                              {repeat ? (
                                <p className="text-xs text-muted">
                                  Mapping: the same column as the key above, so its result there covers this one.
                                </p>
                              ) : (
                                <div className="rounded-lg border border-border bg-surface-2/50 px-3 py-2">
                                  <LinkResult link={link} onOpen={(filter) => setRows({ link, filter })} />
                                </div>
                              )}
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
        {rows && mapping && (
          <KeyRowsPanel mapping={mapping.mappingId} link={rows.link} initialFilter={rows.filter} onClose={() => setRows(null)} />
        )}

        {incoming.length > 0 && (
          <div>
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
              Referenced by <span className="font-normal normal-case tracking-normal">· other tables → this table ({incoming.length})</span>
            </h3>
            <ul className="flex flex-col gap-1.5 text-sm">
              {incoming.map((fk) => (
                <li key={fk.name} className="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <TableName t={fk.parent} />
                  <span className="text-muted">.</span><Cols names={fk.columns} />
                  <span aria-hidden className="text-muted">→</span>
                  <Cols names={fk.ref_columns} />
                  <span className="num text-xs text-muted">· {fmt(fk.parent.rows)} rows</span>
                  <FkState fk={fk} />
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </Card>
  );
}

/** Keys of every target table of the mapping: primary key, foreign keys with the row
 *  counts of the tables they reference, and the tables that reference it. With a mapping
 *  id, also the mapping check: does every link point at the row holding the source value? */
export function KeysView({ targets, mappingId }: { targets: Member[]; mappingId?: string }) {
  const shown = targets.filter((t) => t.exists);
  const [result, setResult] = useState<KeyMapping | null>(null);
  const [loaded, setLoaded] = useState(!mappingId);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!mappingId) return;
    let live = true;
    keymapApi.saved(mappingId)
      .then((d) => { if (live) setResult(d.result); })
      .catch((e) => { if (live) setError(e instanceof Error ? e.message : String(e)); })
      .finally(() => { if (live) setLoaded(true); });
    return () => { live = false; };
  }, [mappingId]);

  async function run() {
    if (!mappingId) return;
    setRunning(true);
    setError(null);
    try {
      setResult(await keymapApi.run(mappingId));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  }

  if (!shown.length) return null;
  // The check covers the mapping's first target: the table its rows are copied into.
  const checked = targets[0];
  const state: MappingState | undefined = mappingId ? { mappingId, result, loaded, running, error, run } : undefined;
  return (
    <div className="flex flex-col gap-4">
      {shown.map((t) => <TargetKeys key={t.ref} target={t} mapping={t.ref === checked?.ref ? state : undefined} />)}
    </div>
  );
}
