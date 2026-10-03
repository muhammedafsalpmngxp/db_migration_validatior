"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { fmt } from "@/lib/api";
import { SectionTabs } from "../ATNM/SectionTabs";
import { duration } from "../ATNM/RunLog";
import { Badge, Card, Dot, ErrorBox, Spinner, type Tone } from "../ui";
import { reportApi, type Preflight, type ReportLogEntry, type ReportMeta, type ReportStatus, type Stage } from "./api";

const OVERALL_TONE: Record<string, Tone> = { "NOT READY": "bad", INCOMPLETE: "warn", READY: "ok" };
const STAGE_TONE: Record<Stage["status"], Tone> = {
  waiting: "neutral", running: "accent", done: "ok", failed: "bad", skipped: "neutral", stopped: "warn",
};
const LEVEL_TONE: Record<ReportLogEntry["level"], Tone> = { info: "neutral", ok: "ok", warn: "warn", error: "bad" };
const when = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleString() : "—");
const time = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

function Check({ ok, label, detail }: { ok: boolean; label: string; detail?: string | null }) {
  return (
    <li className="flex items-start gap-2 text-sm">
      <span className="pt-1.5"><Dot tone={ok ? "ok" : "bad"} label={ok ? "ok" : "problem"} /></span>
      <span><span className="font-medium">{label}</span>{detail ? <span className="text-muted"> · {detail}</span> : null}</span>
    </li>
  );
}

function PreflightCard({ pf, onRefresh }: { pf: Preflight | null; onRefresh: () => void }) {
  return (
    <Card title="Before you start" aside={<button type="button" onClick={onRefresh} className="text-xs text-accent hover:underline">Check again</button>}>
      {!pf ? <Spinner small label="Checking…" /> : (
        <ul className="flex flex-col gap-1">
          <Check ok={pf.atnm.ok} label={`${pf.atnm.label} server (client)`} detail={pf.atnm.ok ? "reachable" : pf.atnm.error} />
          <Check ok={pf.rds.ok} label={`${pf.rds.label} server`} detail={pf.rds.ok ? "reachable" : pf.rds.error} />
          <Check ok={!!pf.plan?.ok} label="Migration plan"
            detail={pf.plan?.ok ? `${pf.plan.tables} required tables (${pf.plan.databases?.map((d) => `${d.source_db} ${d.tables}`).join(", ")}) · ${pf.plan.mappings} mappings` : pf.plan?.error} />
          <Check ok={pf.busy.length === 0} label="Other runs" detail={pf.busy.length ? `${pf.busy.join(" and ")} is running` : "none"} />
          <Check ok label="Summary" detail={pf.ai.enabled ? `written by AI (${pf.ai.model}) from the facts, every number checked` : `plain sentences (${pf.ai.reason})`} />
          {pf.test && (
            <li className="mt-1 rounded bg-warn-soft px-2 py-1 text-xs text-warn">
              TEST MODE: only the {pf.test.tables} smallest of {pf.test.of_tables} tables (Section 1) and {pf.test.mappings} smallest
              of {pf.test.of_mappings} mappings (Section 2) are checked, and the files are marked TEST. For the full report set
              REPORT_TEST_TABLES=0 in backend/.env and restart the backend.
            </li>
          )}
        </ul>
      )}
    </Card>
  );
}

function Bar({ fraction, tone = "accent" }: { fraction: number; tone?: "accent" | "ok" | "warn" }) {
  const color = tone === "ok" ? "bg-ok" : tone === "warn" ? "bg-warn" : "bg-accent";
  return (
    <span className="block h-1.5 w-full rounded-full bg-surface-2">
      <span className={`block h-1.5 rounded-full ${color} transition-all`} style={{ width: `${Math.round(Math.min(1, Math.max(0, fraction)) * 100)}%` }} />
    </span>
  );
}

function StageRow({ s }: { s: Stage }) {
  const frac = s.status === "done" ? 1 : s.fraction ?? (s.total ? s.done / s.total : 0);
  const counting = (s.key === "part1" || s.key === "part2") && s.total > 0;
  return (
    <li className="flex flex-col gap-1 rounded-lg border border-border px-3 py-2">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <Dot tone={STAGE_TONE[s.status]} label={s.status} />
        <span className="font-medium">{s.title}</span>
        <Badge tone={STAGE_TONE[s.status]}>{s.status}</Badge>
        {counting && <span className="num text-xs text-muted">{fmt(s.done)} of {fmt(s.total)} {s.key === "part1" ? "tables" : "mappings"}</span>}
        {s.status === "running" && s.eta_seconds != null && <span className="text-xs text-muted">· about {duration(s.eta_seconds)} left</span>}
        {s.note && s.status !== "running" && <span className="text-xs text-muted">· {s.note}</span>}
      </div>
      {(s.status === "running" || (counting && s.status !== "waiting")) && <Bar fraction={frac} tone={s.status === "done" ? "ok" : "accent"} />}
      {s.status === "running" && (s.current || s.step) && (
        <div className="text-xs">
          {s.current && <span className="font-medium">Now: {s.current}</span>}
          {s.step && <span className="text-muted">{s.current ? " · " : ""}{s.step}</span>}
        </div>
      )}
      {s.status === "running" && s.waiting && <div className="rounded bg-warn-soft px-2 py-1 text-xs text-warn">{s.waiting}</div>}
    </li>
  );
}

export function ReportPage() {
  const [pf, setPf] = useState<Preflight | null>(null);
  const [st, setSt] = useState<ReportStatus | null>(null);
  const [log, setLog] = useState<ReportLogEntry[]>([]);
  const [reports, setReports] = useState<ReportMeta[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [acting, setActing] = useState(false);
  const seen = useRef(0);
  const runOf = useRef<string | null>(null);
  const wasRunning = useRef(false);
  // The run that finished while this page was open: a short "ready" note; the report itself is in the list.
  const [justDone, setJustDone] = useState<{ result: ReportMeta | null; error: string | null } | null>(null);

  const takeStatus = useCallback((s: ReportStatus) => {
    if (wasRunning.current && !s.running) setJustDone({ result: s.result, error: s.error });
    if (s.running) setJustDone(null);
    wasRunning.current = s.running;
    if (s.run_id !== runOf.current || s.log_seq < seen.current) {
      runOf.current = s.run_id;
      seen.current = 0;
      setLog([]);
    }
    if (s.log?.length) {
      const fresh = s.log.filter((e) => e.seq > seen.current);
      seen.current = Math.max(seen.current, ...s.log.map((e) => e.seq));
      if (fresh.length) setLog((l) => [...l, ...fresh].slice(-500));
    }
    setSt(s);
  }, []);

  const loadPf = useCallback(() => {
    setPf(null);
    reportApi.preflight().then(setPf).catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, []);
  const loadList = useCallback(() => {
    reportApi.list().then((r) => setReports(r.reports)).catch(() => undefined);
  }, []);

  useEffect(() => {
    Promise.resolve().then(() => {
      loadPf();
      loadList();
      reportApi.status(0).then(takeStatus).catch((e) => setError(e instanceof Error ? e.message : String(e)));
    });
  }, [loadPf, loadList, takeStatus]);

  // While a run goes, follow it; when it ends, read the list again.
  useEffect(() => {
    if (!st?.running) return;
    let busy = false;
    const timer = setInterval(async () => {
      if (busy) return;
      busy = true;
      try {
        const s = await reportApi.status(seen.current);
        takeStatus(s);
        if (!s.running) {
          loadList();
          loadPf();
        }
      } catch {
        // try again next tick
      } finally {
        busy = false;
      }
    }, 2000);
    return () => clearInterval(timer);
  }, [st?.running, takeStatus, loadList, loadPf]);

  async function act(fn: () => Promise<unknown>) {
    setActing(true);
    setError(null);
    try {
      await fn();
      takeStatus(await reportApi.status(0));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setActing(false);
    }
  }

  const running = !!st?.running;
  const allowed = st?.allowed ?? pf?.allowed ?? false;
  const canStart = allowed && !running && !!pf?.ok;
  const why = !allowed || running ? null : !pf ? "Checking the servers…" : pf.ok ? null : [
    pf.atnm.ok ? null : `${pf.atnm.label}: ${pf.atnm.error}`,
    pf.rds.ok ? null : `${pf.rds.label}: ${pf.rds.error}`,
    pf.plan?.ok ? null : pf.plan?.error,
    pf.busy.length ? `Wait until ${pf.busy.join(" and ")} has finished.` : null,
  ].filter(Boolean).join(" ");

  return (
    <div className="min-h-screen">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-border bg-surface px-4 py-3">
        <div className="flex flex-wrap items-center gap-3">
          <span className="text-base font-semibold tracking-tight">Migration Validator</span>
          <SectionTabs current="report" />
        </div>
      </header>

      <main className="mx-auto flex w-full max-w-5xl flex-col gap-4 px-4 py-5 sm:px-6">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Migration report</h1>
          <p className="text-sm text-muted">
            Checks everything live - Section 1: the required tables copied from ATNM to RDS; Section 2: every mapping into the
            new system - then writes one Word report and one Excel workbook, in plain words. Read-only, one table at a time.
          </p>
        </div>

        {error && <ErrorBox>{error}</ErrorBox>}
        {!running && <PreflightCard pf={pf} onRefresh={loadPf} />}

        <Card
          title={running ? "Generating the report" : "Generate the report"}
          aside={
            <div className="flex items-center gap-2">
              {running ? (
                <button type="button" onClick={() => act(reportApi.cancel)} disabled={acting || st?.cancelling || !allowed}
                  className="rounded-lg border border-border px-3 py-1.5 text-xs hover:border-bad hover:text-bad disabled:opacity-50">
                  {st?.cancelling ? "Stopping…" : "Stop"}
                </button>
              ) : (
                <>
                  {st?.interrupted && (
                    <button type="button" onClick={() => act(() => reportApi.start(true))} disabled={acting || !canStart}
                      className="rounded-lg border border-accent px-3 py-1.5 text-xs font-medium text-accent hover:bg-accent-soft disabled:opacity-50">
                      Resume run {st.interrupted.run_id}
                    </button>
                  )}
                  <button type="button" onClick={() => act(() => reportApi.start(false))} disabled={acting || !canStart}
                    title={!allowed ? "Only on the computer that runs the app" : !pf?.ok ? "See 'Before you start'" : "Check everything live and write the report"}
                    className="rounded-lg bg-accent px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50 dark:text-bg">
                    {acting ? <Spinner small label="Starting…" /> : pf?.test ? "Generate test report" : "Generate report"}
                  </button>
                </>
              )}
            </div>
          }
        >
          {!running ? (
            <div className="flex flex-col gap-3">
              <p className="text-sm text-muted">
                {allowed ? (pf?.test ? `Press Generate test report. Only ${pf.test.tables} + ${pf.test.mappings} small tables are checked, so it takes a few minutes.`
                  : "Press Generate report. It takes about 20–40 minutes; you can leave this page and come back.")
                  : "Reports are started on the computer that runs the app, opened as http://localhost:3000 (a run reads the shared servers for a while). You can follow a run and download the files here."}
              </p>
              {why && <p className="rounded bg-warn-soft px-2 py-1 text-xs text-warn">Can&apos;t start yet: {why}</p>}
              {justDone?.error && <ErrorBox>The last run did not finish: {justDone.error}</ErrorBox>}
              {justDone?.result && (
                <div className="flex flex-wrap items-center gap-2 rounded-lg bg-ok-soft px-3 py-2 text-sm">
                  <span className="font-medium text-ok">Report ready</span>
                  <Badge tone={OVERALL_TONE[justDone.result.overall]}>{justDone.result.overall}</Badge>
                  <a className="text-accent hover:underline" href={reportApi.download(justDone.result.run_id, "docx")}>Word</a>
                  <span className="text-muted">·</span>
                  <a className="text-accent hover:underline" href={reportApi.download(justDone.result.run_id, "xlsx")}>Excel</a>
                  <span className="text-xs text-muted">- also in Reports below</span>
                  {justDone.result.self_check.length > 0 && <span className="text-xs text-bad">(self-check problems: see Reports)</span>}
                </div>
              )}
            </div>
          ) : (
            <div className="flex flex-col gap-3">
              {st.fraction != null && (
                <div className="flex items-center gap-3">
                  <Bar fraction={st.fraction} tone={running ? "accent" : st.error ? "warn" : "ok"} />
                  <span className="num w-12 text-right text-xs text-muted">{Math.round(st.fraction * 100)}%</span>
                </div>
              )}
              <ol className="flex flex-col gap-2">{st.stages.map((s) => <StageRow key={s.key} s={s} />)}</ol>
              {st.error && <ErrorBox>{st.error}</ErrorBox>}
              <details className="rounded-lg border border-border" open={running}>
                <summary className="cursor-pointer px-3 py-2 text-xs font-medium text-muted">Activity ({log.length})</summary>
                <ul className="max-h-72 overflow-y-auto border-t border-border px-3 py-2 text-xs">
                  {[...log].reverse().map((e) => (
                    <li key={e.seq} className="flex gap-2 py-0.5">
                      <span className="num shrink-0 text-muted">{time(e.at)}</span>
                      <span className="pt-1"><Dot tone={LEVEL_TONE[e.level]} label={e.level} /></span>
                      <span>{e.text}</span>
                    </li>
                  ))}
                </ul>
              </details>
            </div>
          )}
        </Card>

        <Card title="Reports" aside={<button type="button" onClick={loadList} className="text-xs text-accent hover:underline">Refresh</button>}>
          {reports.length === 0 ? <p className="text-sm text-muted">No report yet.</p> : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[640px] text-sm">
                <thead className="text-left text-xs text-muted">
                  <tr>
                    <th className="py-1.5 pr-3 font-medium">Checked</th>
                    <th className="py-1.5 pr-3 font-medium">Result</th>
                    <th className="py-1.5 pr-3 font-medium">Must fix · decision · not checked</th>
                    <th className="py-1.5 pr-3 font-medium">Files</th>
                    <th className="py-1.5 font-medium" />
                  </tr>
                </thead>
                <tbody>
                  {reports.map((r) => (
                    <tr key={r.run_id} className="border-t border-border">
                      <td className="py-1.5 pr-3 text-xs">{when(r.started_at)}{r.mode && r.mode !== "Full live check" ? <span className="block text-muted">{r.mode}</span> : null}</td>
                      <td className="py-1.5 pr-3"><Badge tone={OVERALL_TONE[r.overall]}>{r.overall}</Badge>{r.test ? <span className="ml-1"><Badge tone="warn">Test</Badge></span> : null}</td>
                      <td className="num py-1.5 pr-3 text-xs">{fmt(r.totals.issues["Must fix"])} · {fmt(r.totals.issues["Needs a decision"])} · {fmt(r.not_checked)}</td>
                      <td className="py-1.5 pr-3 text-xs">
                        <a className="text-accent hover:underline" href={reportApi.download(r.run_id, "docx")}>Word</a>{" · "}
                        <a className="text-accent hover:underline" href={reportApi.download(r.run_id, "xlsx")}>Excel</a>
                        {r.self_check.length > 0 && <span className="ml-1 text-bad" title={r.self_check.join(" ")}>(self-check problems)</span>}
                      </td>
                      <td className="py-1.5 text-right">
                        {allowed && !running && (
                          <span className="flex justify-end gap-3">
                            <button type="button" onClick={() => act(() => reportApi.reanalyse(r.run_id))} disabled={acting}
                              title="Analyse this run again from the results its checks measured: the final count check, analysis, summary and files are redone; no table is checked again. Use it when the VPN was lost at the end of a run."
                              className="text-xs text-muted hover:text-accent disabled:opacity-50">Analyse again</button>
                            <button type="button" onClick={() => act(async () => { await reportApi.rebuild(r.run_id); loadList(); })}
                              title="Write the files again from this run's results (no database is read)"
                              className="text-xs text-muted hover:text-accent">Rebuild files</button>
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </main>
    </div>
  );
}
