"use client";

import { useEffect, useState } from "react";
import { aiApi, type AiStatus, type AiSummary as Summary } from "@/lib/api";
import { Badge, Card, ErrorBox, Spinner, type Tone } from "./ui";

const STATUS: Record<Summary["status"], { tone: Tone; icon: string }> = {
  ok: { tone: "ok", icon: "✓" },
  review: { tone: "warn", icon: "!" },
  problem: { tone: "bad", icon: "✕" },
  not_checked: { tone: "neutral", icon: "–" },
};

function when(iso: string | null) {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? iso : d.toLocaleString();
}

function FactsView({ text, tokens }: { text: string; tokens?: number }) {
  return (
    <div className="mt-2">
      <p className="mb-1 text-xs text-muted">
        Exactly what is sent to the AI{tokens ? ` · about ${tokens} tokens` : ""}. No passwords, no whole tables, no email or phone values.
      </p>
      <pre className="max-h-80 overflow-auto whitespace-pre-wrap rounded-lg border border-border bg-surface-2 px-3 py-2 font-mono text-xs leading-relaxed">
        {text}
      </pre>
    </div>
  );
}

/** A plain-words paragraph about the mapping, written by the AI from the measured facts. */
export function AiSummary({ mappingId }: { mappingId: string }) {
  const [state, setState] = useState<AiStatus | null>(null);
  const [result, setResult] = useState<Summary | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showFacts, setShowFacts] = useState(false);
  const [facts, setFacts] = useState<{ text: string; tokens: number } | null>(null);

  useEffect(() => {
    let live = true;
    aiApi.status().then((s) => {
      if (!live) return;
      setState(s);
      if (!s.enabled) { setLoaded(true); return; }
      aiApi.saved(mappingId)
        .then((d) => { if (live) setResult(d.result); })
        .catch(() => {})
        .finally(() => { if (live) setLoaded(true); });
    }).catch((e) => { if (live) { setError(e instanceof Error ? e.message : String(e)); setLoaded(true); } });
    return () => { live = false; };
  }, [mappingId]);

  async function generate(refresh: boolean) {
    setBusy(true);
    setError(null);
    try {
      setResult(await aiApi.generate(mappingId, refresh));
      setFacts(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function toggleFacts() {
    const next = !showFacts;
    setShowFacts(next);
    if (next && !result && !facts) {
      try {
        const f = await aiApi.facts(mappingId);
        setFacts({ text: f.facts, tokens: f.approx_tokens });
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    }
  }

  const title = <>AI summary <span className="font-normal text-muted">· in plain words</span></>;

  if (!loaded) return <Card title={title}><Spinner label="Loading…" /></Card>;

  if (state && !state.enabled) {
    return (
      <Card title={title}>
        <p className="text-sm text-muted">{state.reason}</p>
        <button type="button" onClick={toggleFacts} className="mt-2 text-xs text-accent hover:underline">
          {showFacts ? "Hide what would be sent to the AI" : "See what would be sent to the AI"}
        </button>
        {showFacts && (facts ? <FactsView text={facts.text} tokens={facts.tokens} /> : <Spinner small />)}
      </Card>
    );
  }

  const st = result ? STATUS[result.status] ?? STATUS.not_checked : null;

  return (
    <Card
      title={title}
      aside={
        <div className="flex items-center gap-3">
          {result && !busy && <span className="text-xs text-muted">Written {when(result.generated_at)}</span>}
          <button
            type="button"
            onClick={() => generate(!!result)}
            disabled={busy}
            className="rounded-lg bg-accent px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50 dark:text-bg"
          >
            {busy ? "Writing…" : result ? "Regenerate" : "Generate summary"}
          </button>
        </div>
      }
    >
      {error && <div className="mb-3"><ErrorBox>{error}</ErrorBox></div>}
      {busy && <Spinner label="Reading the checks and writing a summary… this can take up to a minute." />}

      {!busy && !result && (
        <p className="text-sm text-muted">
          Writes a short, plain-words explanation of this table from the measured checks below: row counts, the value
          check, the column comparison, the keys and the mapping check. Nothing is written until you press Generate
          summary.
        </p>
      )}

      {!busy && result && st && (
        <div className="flex flex-col gap-3">
          {result.stale && (
            <p className="rounded-lg bg-warn-soft px-3 py-2 text-xs text-warn">
              The data check or the mapping check was run again after this summary was written. Regenerate to use
              the latest results.
            </p>
          )}
          <div>
            <Badge tone={st.tone}>{st.icon} {result.verdict}</Badge>
          </div>
          <p className="max-w-3xl text-[15px] leading-7">{result.summary}</p>
          {result.worth_checking.length > 0 && (
            <div>
              <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted">Worth checking</p>
              <ul className="list-disc space-y-1 pl-5 text-sm leading-6">
                {result.worth_checking.map((w, i) => <li key={i}>{w}</li>)}
              </ul>
            </div>
          )}
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-border pt-2 text-xs text-muted">
            {result.numbers_verified ? (
              <span className="text-ok">✓ Every number matches the measured facts</span>
            ) : (
              <span className="text-warn" title="These numbers are not in the facts sent to the AI">
                ! Numbers not found in the facts: {result.unverified_numbers.join(", ")} — trust the checks below
              </span>
            )}
            <span>From the data check of {when(result.data_check_at)}</span>
            <span>{result.model}{result.tokens?.input ? ` · ${result.tokens.input} + ${result.tokens.output ?? 0} tokens` : ""}</span>
          </div>
          <p className="text-[11px] text-muted">
            Written by AI from the checks on this page. The checks are the source of truth; the AI does not change any verdict.
          </p>
        </div>
      )}

      <button type="button" onClick={toggleFacts} className="mt-2 text-xs text-accent hover:underline">
        {showFacts ? "Hide what is sent to the AI" : "See what is sent to the AI"}
      </button>
      {showFacts && (result ? <FactsView text={result.facts} /> : facts ? <FactsView text={facts.text} tokens={facts.tokens} /> : <Spinner small />)}
    </Card>
  );
}
