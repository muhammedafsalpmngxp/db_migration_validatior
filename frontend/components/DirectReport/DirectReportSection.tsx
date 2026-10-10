"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { fmt } from "@/lib/api";
import { Badge, Card, Dot, ErrorBox, Spinner, type Tone } from "../ui";
import { directApi, type DirectLogEntry, type DirectPreflight, type DirectReportMeta, type DirectStage, type DirectStatus } from "./api";

const OVERALL_TONE: Record<string, Tone> = { "NOT READY": "bad", INCOMPLETE: "warn", READY: "ok" };
const STAGE_TONE: Record<DirectStage["status"], Tone> = { waiting: "neutral", running: "accent", done: "ok", failed: "bad", stopped: "warn" };
const LEVEL_TONE: Record<DirectLogEntry["level"], Tone> = { info: "neutral", ok: "ok", warn: "warn", error: "bad" };
const when = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleString() : "—");
const time = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

function Check({ ok, label, detail }: { ok: boolean; label: string; detail?: string | null }) {
  return (
    <li className="flex items-start gap-2 text-sm">
      <span className="pt-1.5"><Dot tone={ok ? "ok" : "bad"} label={ok ? "ok" : "problem"} /></span>
      <span><span className="font-medium">{label}</span>{detail ? <span className="text-muted"> · {detail}</span> : null}</span>
    </li>
  );
}

function PreflightCard({ pf, onRefresh }: { pf: DirectPreflight | null; onRefresh: () => void }) {
  return (
    <Card title="Before you start · direct report" aside={<button type="button" onClick={onRefresh} className="text-xs text-accent hover:underline">Check again</button>}>
      {!pf ? <Spinner small label="Checking…" /> : (
        <ul className="flex flex-col gap-1">
          <Check ok={pf.atnm.ok} label={`${pf.atnm.label} server (client)`} detail={pf.atnm.ok ? "reachable" : pf.atnm.error} />
          <Check ok={pf.rds.ok} label={`${pf.rds.label} server (new system)`} detail={pf.rds.ok ? "reachable" : pf.rds.error} />
          <Check ok={!!pf.plan?.ok} label="Migration plan"
            detail={pf.plan?.ok ? `${pf.plan.mappings} mappings · reads ${pf.plan.databases?.map((d) => `${d.server} ${d.database}`).join(", ")}` : pf.plan?.error} />
          <Check ok={pf.busy.length === 0} label="Other runs"
            detail={pf.busy.length ? `${pf.busy.join(" and ")} is running - this run waits for its turn` : "none"} />
          {pf.test ? (
            <li className="mt-1 rounded bg-warn-soft px-2 py-1 text-xs text-warn">
              TEST MODE: only the {pf.test} smallest mappings are graded and the file is marked TEST. For the full report set
              DIRECT_TEST_MAPPINGS=0 in backend/.env and restart the backend.
            </li>
          ) : null}
        </ul>
      )}
    </Card>
  );
}

function Bar({ fraction, tone = "accent" }: { fraction: number; tone?: "accent" | "ok" }) {
  return (
    <span className="block h-1.5 w-full rounded-full bg-surface-2">
      <span className={`block h-1.5 rounded-full ${tone === "ok" ? "bg-ok" : "bg-accent"} transition-all`}
        style={{ width: `${Math.round(Math.min(1, Math.max(0, fraction)) * 100)}%` }} />
    </span>
  );
}

function StageRow({ s }: { s: DirectStage }) {
  const frac = s.status === "done" ? 1 : s.total ? s.done / s.total : 0;
  return (
    <li className="flex flex-col gap-1 rounded-lg border border-border px-3 py-2">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <Dot tone={STAGE_TONE[s.status]} label={s.status} />
        <span className="font-medium">{s.title}</span>
        <Badge tone={STAGE_TONE[s.status]}>{s.status}</Badge>
        {s.total > 0 && <span className="num text-xs text-muted">{fmt(s.done)} of {fmt(s.total)}</span>}
      </div>
      {s.status === "running" && <Bar fraction={frac} />}
      {s.status === "running" && s.current && <div className="text-xs font-medium">Now: {s.current}</div>}
    </li>
  );
}

/** The direct report (client ATNM → AlTasnimBI), shown on the Report page below the migration report. */
export function DirectReportSection() {
  const [pf, setPf] = useState<DirectPreflight | null>(null);
  const [st, setSt] = useState<DirectStatus | null>(null);
  const [log, setLog] = useState<DirectLogEntry[]>([]);
  const [reports, setReports] = useState<DirectReportMeta[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [acting, setActing] = useState(false);
  const seen = useRef(0);
  const runOf = useRef<string | null>(null);
  const wasRunning = useRef(false);
  const [justDone, setJustDone] = useState<{ result: DirectReportMeta | null; error: string | null } | null>(null);

  const takeStatus = useCallback((s: DirectStatus) => {
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
    directApi.preflight().then(setPf).catch((e) => setError(message(e)));
  }, []);
  const loadList = useCallback(() => {
    directApi.list().then((r) => setReports(r.reports)).catch(() => undefined);
  }, []);

  useEffect(() => {
    Promise.resolve().then(() => {
      loadPf();
      loadList();
      directApi.status(0).then(takeStatus).catch((e) => setError(message(e)));
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
        const s = await directApi.status(seen.current);
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
      takeStatus(await directApi.status(0));
    } catch (e) {
      setError(message(e));
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
  ].filter(Boolean).join(" ");

  return (
    <section aria-labelledby="direct-report" className="flex flex-col gap-4 border-t border-border pt-6">
        <div>
          <h2 id="direct-report" className="text-xl font-semibold tracking-tight">Direct migration report</h2>
          <p className="text-sm text-muted">
            The client&apos;s original databases on the ATNM server, checked straight against the new system (AlTasnimBI) with
            the migration plan&apos;s mappings - one to one, union and merge. This version reads the table lists only: which
            tables exist, the record counts by each mapping&apos;s rule and how the columns pair up. Values are not compared
            yet. Read-only; separate from the migration report.
          </p>
        </div>

        {error && <ErrorBox>{error}</ErrorBox>}
        {!running && <PreflightCard pf={pf} onRefresh={loadPf} />}

        <Card
          title={running ? "Generating the direct report" : "Generate the direct report"}
          aside={running ? (
            <button type="button" onClick={() => act(directApi.cancel)} disabled={acting || st?.cancelling || !allowed}
              className="rounded-lg border border-border px-3 py-1.5 text-xs hover:border-bad hover:text-bad disabled:opacity-50">
              {st?.cancelling ? "Stopping…" : "Stop"}
            </button>
          ) : (
            <button type="button" onClick={() => act(directApi.start)} disabled={acting || !canStart}
              title={!allowed ? "Only on the computer that runs the app" : !pf?.ok ? "See 'Before you start'" : "Read the table lists and write the report"}
              className="rounded-lg bg-accent px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50 dark:text-bg">
              {acting ? <Spinner small label="Starting…" /> : pf?.test ? "Generate test report" : "Generate direct report"}
            </button>
          )}
        >
          {!running ? (
            <div className="flex flex-col gap-3">
              <p className="text-sm text-muted">
                {allowed ? "Press Generate. It reads three table lists (two on ATNM, one on RDS), so it takes about a minute."
                  : "Reports are started on the computer that runs the app, opened as http://localhost:3000. You can follow a run and download the files here."}
              </p>
              {why && <p className="rounded bg-warn-soft px-2 py-1 text-xs text-warn">Can&apos;t start yet: {why}</p>}
              {justDone?.error && <ErrorBox>The last run did not finish: {justDone.error}</ErrorBox>}
              {justDone?.result && (
                <div className="flex flex-wrap items-center gap-2 rounded-lg bg-ok-soft px-3 py-2 text-sm">
                  <span className="font-medium text-ok">Report ready</span>
                  <Badge tone={OVERALL_TONE[justDone.result.overall]}>{justDone.result.overall}</Badge>
                  <a className="text-accent hover:underline" href={directApi.download(justDone.result.run_id)}>Excel</a>
                  <span className="text-xs text-muted">- also in Reports below</span>
                </div>
              )}
            </div>
          ) : (
            <div className="flex flex-col gap-3">
              <ol className="flex flex-col gap-2">{st.stages.map((s) => <StageRow key={s.key} s={s} />)}</ol>
              {st.waiting && <div className="rounded bg-warn-soft px-2 py-1 text-xs text-warn">{st.waiting}</div>}
              {st.error && <ErrorBox>{st.error}</ErrorBox>}
              <details className="rounded-lg border border-border" open>
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

        <Card title="Direct reports" aside={<button type="button" onClick={loadList} className="text-xs text-accent hover:underline">Refresh</button>}>
          {reports.length === 0 ? <p className="text-sm text-muted">No direct report yet.</p> : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[560px] text-sm">
                <thead className="text-left text-xs text-muted">
                  <tr>
                    <th className="py-1.5 pr-3 font-medium">Checked</th>
                    <th className="py-1.5 pr-3 font-medium">Result</th>
                    <th className="py-1.5 pr-3 font-medium">Need to fix · decision · not checked</th>
                    <th className="py-1.5 font-medium">File</th>
                  </tr>
                </thead>
                <tbody>
                  {reports.map((r) => (
                    <tr key={r.run_id} className="border-t border-border">
                      <td className="py-1.5 pr-3 text-xs">{when(r.started_at)}</td>
                      <td className="py-1.5 pr-3">
                        <Badge tone={OVERALL_TONE[r.overall]}>{r.overall}</Badge>
                        {r.test ? <span className="ml-1"><Badge tone="warn">Test</Badge></span> : null}
                      </td>
                      <td className="num py-1.5 pr-3 text-xs">
                        {fmt(r.totals["Must fix"])} · {fmt(r.totals["Needs a decision"])} · {fmt(r.totals["Not checked"])}
                      </td>
                      <td className="py-1.5 text-xs"><a className="text-accent hover:underline" href={directApi.download(r.run_id)}>Excel</a></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
    </section>
  );
}
