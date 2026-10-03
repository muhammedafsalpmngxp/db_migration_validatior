"use client";

import { useCallback, useEffect, useState } from "react";
import { fmt, tableChecksApi, type TableCheck } from "@/lib/api";
import { Badge, Card, Dot, ErrorBox, Spinner, type Tone } from "./ui";

const STATUS: Record<TableCheck["status"], { label: string; tone: Tone }> = {
  ok: { label: "In order", tone: "ok" },
  review: { label: "Review", tone: "warn" },
  problems: { label: "Problems", tone: "bad" },
  skipped: { label: "Not checked", tone: "neutral" },
  locked: { label: "Table locked", tone: "warn" },
  error: { label: "Check failed", tone: "bad" },
  timeout: { label: "Timed out", tone: "bad" },
};
const FINDING: Record<TableCheck["findings"][number]["severity"], Tone> = { error: "bad", review: "warn", info: "neutral" };

/** The target table on its own: primary key, duplicate rows, foreign keys in both directions. */
export function TableChecks({ mappingId }: { mappingId: string }) {
  const [result, setResult] = useState<TableCheck | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [available, setAvailable] = useState(true);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    tableChecksApi.saved(mappingId)
      .then((r) => { if (live) { setResult(r.result); setLoaded(true); } })
      .catch((e) => {
        if (!live) return;
        const text = e instanceof Error ? e.message : String(e);
        if (text.startsWith("404")) setAvailable(false);   // a backend without the table checks
        else setError(text);
        setLoaded(true);
      });
    return () => { live = false; };
  }, [mappingId]);

  const run = useCallback(async () => {
    setRunning(true);
    setError(null);
    try {
      setResult(await tableChecksApi.run(mappingId));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  }, [mappingId]);

  if (!available) return null;
  const s = result ? STATUS[result.status] : null;
  return (
    <Card
      title={<span className="flex items-center gap-2">Target table checks {s && <Badge tone={s.tone}>{s.label}</Badge>}</span>}
      aside={
        <button type="button" onClick={run} disabled={running}
          title="Primary key, rows that repeat another row exactly, and every foreign key (both directions) of the target table"
          className="rounded-lg border border-accent px-3 py-1.5 text-xs font-medium text-accent hover:bg-accent-soft disabled:opacity-50">
          {running ? <Spinner small label="Checking…" /> : result ? "Check again" : "Check the target table"}
        </button>
      }
    >
      {error && <ErrorBox>{error}</ErrorBox>}
      {!loaded ? <Spinner small label="Reading…" /> : !result ? (
        <p className="text-sm text-muted">
          Not checked yet. Looks at the target table itself: does it have a primary key, do rows repeat another row
          exactly, and does every foreign key (its own and the ones pointing at it) point at a row that exists.
          {" "}It also runs as part of “Re-run all data checks”.
        </p>
      ) : (
        <div className="flex flex-col gap-3">
          <p className="text-xs text-muted">
            Checked {new Date(result.checked_at).toLocaleString()} in {result.seconds}s · {result.headline}
          </p>
          {result.tables.filter((t) => t.exists).map((t) => (
            <div key={t.table} className="rounded-lg border border-border p-3 text-sm">
              <div className="mb-2 font-medium">{t.table}</div>
              <div className="grid grid-cols-1 gap-1 text-xs sm:grid-cols-2">
                <span>
                  <span className="text-muted">Primary key: </span>
                  {t.primary_key ? t.primary_key.join(", ") : <span className="text-warn">none</span>}
                  {t.source_primary_key !== undefined && (
                    <span className="text-muted"> (source: {t.source_primary_key ? t.source_primary_key.join(", ") : "none"})</span>
                  )}
                </span>
                {(t.identity ?? []).map((id) => (
                  <span key={id.column}>
                    <span className="text-muted">Identity {id.column}: </span>
                    {id.behind ? (
                      <span className="text-bad">behind: next {fmt(id.next_value)}, but {fmt(id.increment > 0 ? id.highest : id.lowest)} is used</span>
                    ) : (
                      <span>next {fmt(id.next_value)} (highest used {fmt(id.highest)})</span>
                    )}
                  </span>
                ))}
                {t.duplicates && (
                  <span>
                    <span className="text-muted">Rows that repeat another row exactly: </span>
                    <span className={t.duplicates.duplicate_rows ? "text-bad" : ""}>{fmt(t.duplicates.duplicate_rows)}</span>
                    {t.source_duplicates && <span className="text-muted"> (source: {fmt(t.source_duplicates.duplicate_rows)})</span>}
                  </span>
                )}
              </div>
              {(t.foreign_keys?.length ?? 0) > 0 && (
                <div className="mt-2 overflow-x-auto">
                  <table className="w-full min-w-[520px] text-xs">
                    <thead className="text-left text-muted">
                      <tr>
                        <th className="py-1 pr-3 font-medium">Foreign key</th>
                        <th className="py-1 pr-3 font-medium">Direction</th>
                        <th className="py-1 pr-3 font-medium">State</th>
                        <th className="py-1 pr-3 text-right font-medium">Orphan rows</th>
                      </tr>
                    </thead>
                    <tbody>
                      {t.foreign_keys!.map((f) => (
                        <tr key={`${f.name}|${f.direction}`} className="border-t border-border">
                          <td className="py-1 pr-3 font-mono">{f.child}({f.columns.join(", ")}) → {f.references}</td>
                          <td className="py-1 pr-3 text-muted">{f.direction === "outgoing" ? "this table points out" : "points at this table"}</td>
                          <td className="py-1 pr-3">
                            {!f.enabled ? <Badge tone="warn">disabled</Badge> : !f.trusted ? <Badge tone="warn">not trusted</Badge> : <Badge tone="ok">enforced</Badge>}
                          </td>
                          <td className={`num py-1 pr-3 text-right ${f.orphans ? "text-bad" : ""}`}
                            title={f.counted ? "Counted" : "An enabled, trusted key cannot have orphans"}>
                            {f.orphans == null ? "—" : fmt(f.orphans)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          ))}
          {result.findings.length > 0 && (
            <ul className="flex flex-col gap-1">
              {result.findings.map((f, i) => (
                <li key={i} className="flex items-start gap-2 text-sm">
                  <span className="pt-1.5"><Dot tone={FINDING[f.severity]} label={f.severity} /></span>{f.text}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </Card>
  );
}
