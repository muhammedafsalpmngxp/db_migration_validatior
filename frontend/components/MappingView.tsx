"use client";

import { useState } from "react";
import {
  api, fmt, fmtDelta, fmtSize, MAPPING_TYPE_HINT, MAPPING_TYPE_LABEL,
  type CompareResult, type Member, type RowCheck,
} from "@/lib/api";
import { Badge, Card, CHECK, SideTag, Spinner } from "./ui";

type Exact = { state: "loading" } | { state: "done"; rows: number; seconds: number } | { state: "error"; message: string };

const BIG_TABLE = 10_000_000;

function MemberCard({ m, onSelect }: { m: Member; onSelect?: (ref: string) => void }) {
  const clickable = !!onSelect && !m.selected && m.side !== "T";
  const body = (
    <>
      <div className="flex items-center gap-2">
        <SideTag side={m.side} />
        <span className="min-w-0 flex-1 truncate text-sm font-medium" title={`${m.database}.${m.schema}.${m.table}`}>
          <span className="font-normal text-muted">{m.schema}.</span>{m.table}
        </span>
        {m.role && <Badge tone="neutral">{m.role}</Badge>}
        {!m.exists && <Badge tone="bad">missing</Badge>}
        {m.locked && <Badge tone="warn" title="Held by another session, probably a load in progress">locked</Badge>}
      </div>
      <div className="num mt-1 flex flex-wrap gap-x-3 pl-7 text-xs text-muted">
        <span>{fmt(m.rows)} rows</span>
        <span>{m.columns ?? "—"} columns</span>
        <span>{fmtSize(m.size_kb)}</span>
      </div>
    </>
  );
  const cls = `block w-full rounded-lg border px-3 py-2 text-left ${
    m.selected ? "border-accent bg-accent-soft" : "border-border bg-surface"
  }`;
  return (
    <li>
      {clickable ? (
        <button type="button" onClick={() => onSelect!(m.ref)} className={`${cls} transition-colors hover:border-accent/60`} title="Compare this table">
          {body}
        </button>
      ) : (
        <div className={cls}>{body}</div>
      )}
    </li>
  );
}

function CheckPanel({ check, label }: { check: RowCheck; label: string }) {
  const c = CHECK[check.status];
  return (
    <div className="flex flex-wrap items-center gap-x-6 gap-y-2 rounded-lg border border-border bg-surface-2 px-4 py-3">
      <div className="flex flex-col gap-1">
        <span className="text-[11px] font-medium uppercase tracking-wide text-muted">{label}</span>
        <span className="flex items-center gap-2"><Badge tone={c.tone}>{c.label}</Badge><span className="text-sm text-muted">{check.rule}</span></span>
      </div>
      {(check.status === "match" || check.status === "mismatch") && check.expected != null && (
        <dl className="num ml-auto flex gap-6 text-sm">
          <div><dt className="text-xs text-muted">Expected</dt><dd className="font-medium">{fmt(check.expected)}</dd></div>
          <div><dt className="text-xs text-muted">Target</dt><dd className="font-medium">{fmt(check.actual)}</dd></div>
          <div>
            <dt className="text-xs text-muted">Difference</dt>
            <dd className={`font-medium ${check.delta ? "text-bad" : "text-ok"}`}>{fmtDelta(check.delta, check.expected)}</dd>
          </div>
        </dl>
      )}
    </div>
  );
}

/** The mapping's count rule applied to exact counts (mirrors backend compare.row_check). */
function exactCheck(result: CompareResult, exact: Record<string, Exact>): RowCheck | null {
  const { mapping, row_check } = result;
  if (!["one_to_one", "union", "merge"].includes(mapping.type)) return null;
  const n = (m: Member) => {
    const e = exact[m.ref];
    return e?.state === "done" ? e.rows : null;
  };
  const involved = mapping.type === "merge"
    ? [mapping.sources.find((s) => s.role === "driving")!, ...mapping.targets]
    : [...mapping.sources, ...mapping.targets];
  if (involved.some((m) => n(m) == null)) return null;
  const expected = mapping.type === "merge"
    ? n(involved[0])!
    : mapping.sources.reduce((a, s) => a + n(s)!, 0);
  const actual = mapping.targets.reduce((a, t) => a + n(t)!, 0);
  const delta = actual - expected;
  return { rule: row_check.rule, expected, actual, delta, delta_pct: expected ? (delta * 100) / expected : null, status: delta === 0 ? "match" : "mismatch" };
}

export function MappingView({ result, onSelect }: { result: CompareResult; onSelect: (ref: string) => void }) {
  const m = result.mapping;
  const members = [
    ...m.sources.map((s) => ({ ...s, kind: "Source" })),
    ...m.targets.map((t) => ({ ...t, kind: "Target" })),
  ];
  const [exact, setExact] = useState<Record<string, Exact>>({});
  const counting = Object.values(exact).some((e) => e.state === "loading");
  const big = members.filter((x) => (x.rows ?? 0) > BIG_TABLE);

  async function countAll() {
    const todo = members.filter((x) => x.exists && !x.locked);
    setExact(Object.fromEntries(todo.map((x) => [x.ref, { state: "loading" } as Exact])));
    // Three at a time: enough to be quick, few enough not to load the server.
    let next = 0;
    async function worker() {
      while (next < todo.length) {
        const x = todo[next++];
        try {
          const r = await api.exactCount(x.ref);
          setExact((e) => ({ ...e, [x.ref]: { state: "done", rows: r.rows, seconds: r.seconds } }));
        } catch (err) {
          setExact((e) => ({ ...e, [x.ref]: { state: "error", message: err instanceof Error ? err.message : String(err) } }));
        }
      }
    }
    await Promise.all([worker(), worker(), worker()]);
  }

  const exactResult = exactCheck(result, exact);

  return (
    <div className="flex flex-col gap-4">
      <Card
        title={<span className="flex items-center gap-2">Mapping <code className="font-mono text-xs font-normal text-muted">{m.id}</code></span>}
        aside={<Badge tone={m.type === "excluded" ? "neutral" : "accent"}>{MAPPING_TYPE_LABEL[m.type]} · {m.sources.length} → {m.targets.length}</Badge>}
      >
        <p className="mb-3 text-sm text-muted">
          {MAPPING_TYPE_HINT[m.type]}
          {m.note && <span className="mt-1 block text-text">{m.note}</span>}
        </p>

        <div className={`grid items-start gap-3 ${m.targets.length ? "md:grid-cols-[1fr_auto_1fr]" : ""}`}>
          <div>
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
              Source{m.sources.length > 1 ? `s (${m.sources.length})` : ""}
              {m.sources.length > 1 && <span className="ml-2 font-normal normal-case tracking-normal">click one to compare it</span>}
            </h3>
            <ul className="flex max-h-96 flex-col gap-2 overflow-auto pr-1">
              {m.sources.map((s) => <MemberCard key={s.ref} m={s} onSelect={onSelect} />)}
            </ul>
          </div>
          {m.targets.length > 0 && (
            <>
              <div aria-hidden className="flex justify-center self-center text-2xl text-muted md:px-2">
                <span className="md:hidden">↓</span><span className="hidden md:inline">→</span>
              </div>
              <div>
                <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
                  Target{m.targets.length > 1 ? `s (${m.targets.length})` : ""}
                </h3>
                <ul className="flex flex-col gap-2">
                  {m.targets.map((t) => <MemberCard key={t.ref} m={t} />)}
                </ul>
              </div>
            </>
          )}
        </div>
      </Card>

      <Card
        title="Row counts"
        aside={
          <button
            type="button"
            onClick={countAll}
            disabled={counting}
            className="rounded-lg border border-border px-3 py-1.5 text-xs font-medium hover:border-accent hover:text-accent disabled:opacity-50"
            title="Run SELECT COUNT_BIG(*) on every table in this mapping"
          >
            {counting ? <Spinner small label="Counting…" /> : "Count exactly"}
          </button>
        }
      >
        <div className="flex flex-col gap-2">
          <CheckPanel check={result.row_check} label="Live (table metadata)" />
          {exactResult && <CheckPanel check={exactResult} label="Exact (COUNT(*))" />}
        </div>
        {big.length > 0 && (
          <p className="mt-2 text-xs text-warn">
            {big.map((b) => b.table).join(", ")} {big.length === 1 ? "has" : "have"} over {fmt(BIG_TABLE)} rows; an exact count can take a few minutes.
          </p>
        )}

        <div className="mt-3 overflow-x-auto">
          <table className="num w-full min-w-[720px] text-sm">
            <thead>
              <tr className="border-b border-border text-left text-xs text-muted">
                <th className="py-2 pr-3 font-medium">Table</th>
                <th className="py-2 pr-3 font-medium">Role</th>
                <th className="py-2 pr-3 text-right font-medium" title="From sys.partitions: instant, current as of the last refresh">Rows</th>
                <th className="py-2 pr-3 text-right font-medium" title="SELECT COUNT_BIG(*)">Exact rows</th>
                <th className="py-2 pr-3 text-right font-medium">Columns</th>
                <th className="py-2 pr-3 text-right font-medium">Size</th>
                <th className="py-2 text-right font-medium">Created</th>
              </tr>
            </thead>
            <tbody>
              {members.map((x) => {
                const e = exact[x.ref];
                return (
                  <tr key={x.ref} className={`border-b border-border last:border-0 ${x.selected ? "bg-accent-soft/60" : ""}`}>
                    <td className="py-2 pr-3">
                      <span className="flex items-center gap-2">
                        <SideTag side={x.side} />
                        <span><span className="text-muted">{x.schema}.</span>{x.table}</span>
                      </span>
                    </td>
                    <td className="py-2 pr-3 text-muted">{x.kind}{x.role ? ` · ${x.role}` : ""}</td>
                    <td className="py-2 pr-3 text-right font-medium">{fmt(x.rows)}</td>
                    <td className="py-2 pr-3 text-right">
                      {!e ? <span className="text-muted">—</span>
                        : e.state === "loading" ? <Spinner small />
                        : e.state === "error" ? <span className="text-bad" title={e.message}>failed</span>
                        : (
                          <span title={`${e.seconds}s`} className={e.rows !== x.rows ? "font-medium text-warn" : ""}>
                            {fmt(e.rows)}
                          </span>
                        )}
                    </td>
                    <td className="py-2 pr-3 text-right">{x.columns ?? "—"}</td>
                    <td className="py-2 pr-3 text-right text-muted">{fmtSize(x.size_kb)}</td>
                    <td className="py-2 text-right text-muted">{x.created ? x.created.slice(0, 10) : "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <p className="mt-2 text-xs text-muted">
          Rows come from SQL Server&apos;s partition metadata, read live. <strong className="font-medium">Count exactly</strong> runs COUNT(*) to confirm them.
        </p>
      </Card>
    </div>
  );
}
