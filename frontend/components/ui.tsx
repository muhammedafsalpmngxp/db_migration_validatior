import type { ReactNode } from "react";
import type { CheckStatus, ColumnStatus, Side } from "@/lib/api";

export function Card({ title, aside, children, className = "", flush = false }: {
  title?: ReactNode;
  aside?: ReactNode;
  children: ReactNode;
  className?: string;
  flush?: boolean;
}) {
  return (
    <section className={`min-w-0 rounded-xl border border-border bg-surface ${className}`}>
      {(title || aside) && (
        <header className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-3">
          <h2 className="text-sm font-semibold">{title}</h2>
          {aside}
        </header>
      )}
      <div className={flush ? "" : "p-4"}>{children}</div>
    </section>
  );
}

const SIDE_CLASS: Record<Side, string> = {
  A: "text-side-a border-side-a/40 bg-side-a/10",
  B: "text-side-b border-side-b/40 bg-side-b/10",
  T: "text-side-t border-side-t/40 bg-side-t/10",
};

export function SideTag({ side }: { side: Side }) {
  return (
    <span
      className={`inline-flex h-5 min-w-5 shrink-0 items-center justify-center rounded border px-1 font-mono text-[11px] font-semibold ${SIDE_CLASS[side]}`}
      title={side === "T" ? "Target database" : `Source database ${side}`}
    >
      {side}
    </span>
  );
}

export type Tone = "ok" | "warn" | "bad" | "info" | "accent" | "neutral";

const TONE_CLASS: Record<Tone, string> = {
  ok: "bg-ok-soft text-ok",
  warn: "bg-warn-soft text-warn",
  bad: "bg-bad-soft text-bad",
  info: "bg-info-soft text-info",
  accent: "bg-accent-soft text-accent",
  neutral: "bg-surface-2 text-muted",
};

const DOT_CLASS: Record<Tone, string> = {
  ok: "bg-ok",
  warn: "bg-warn",
  bad: "bg-bad",
  info: "bg-info",
  accent: "bg-accent",
  neutral: "bg-muted/50",
};

export function Badge({ tone = "neutral", children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span
      title={title}
      className={`inline-flex items-center whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium ${TONE_CLASS[tone]}`}
    >
      {children}
    </span>
  );
}

export function Dot({ tone, label }: { tone: Tone; label: string }) {
  return <span role="img" aria-label={label} title={label} className={`inline-block h-2 w-2 shrink-0 rounded-full ${DOT_CLASS[tone]}`} />;
}

export const CHECK: Record<CheckStatus, { label: string; tone: Tone }> = {
  match: { label: "Counts match", tone: "ok" },
  mismatch: { label: "Counts differ", tone: "bad" },
  info: { label: "No count rule", tone: "info" },
  unknown: { label: "Table missing", tone: "warn" },
  excluded: { label: "Excluded", tone: "neutral" },
  locked: { label: "Table locked", tone: "warn" },
};

export const COLUMN_STATUS: Record<ColumnStatus, { label: string; tone: Tone; hint: string }> = {
  match: { label: "Match", tone: "ok", hint: "Same name, type, nullability and key" },
  renamed: { label: "Renamed", tone: "info", hint: "Paired under a different name; definition unchanged" },
  changed: { label: "Changed", tone: "warn", hint: "Paired, but type, nullability or key differs" },
  source_only: { label: "Only in source", tone: "bad", hint: "No target column found for it" },
  target_only: { label: "Only in target", tone: "accent", hint: "New in the target, or renamed beyond recognition" },
};

export function Spinner({ label, small = false }: { label?: string; small?: boolean }) {
  return (
    <span className="inline-flex items-center gap-2 text-sm text-muted">
      <span className={`${small ? "h-3 w-3" : "h-4 w-4"} animate-spin rounded-full border-2 border-border border-t-accent`} />
      {label}
    </span>
  );
}

export function ErrorBox({ children }: { children: ReactNode }) {
  return (
    <div role="alert" className="rounded-lg border border-bad/30 bg-bad-soft px-4 py-3 text-sm text-bad">
      {children}
    </div>
  );
}

export function Stat({ label, value, tone, hint }: { label: string; value: ReactNode; tone?: Tone; hint?: string }) {
  return (
    <div className="rounded-xl border border-border bg-surface px-4 py-3" title={hint}>
      <div className="flex items-center gap-2 text-xs text-muted">
        {tone && <Dot tone={tone} label={label} />}
        {label}
      </div>
      <div className="num mt-1 text-2xl font-semibold tracking-tight">{value}</div>
    </div>
  );
}
