import Link from "next/link";

// The two stages of the migration, each its own section of the app.
const SECTIONS = [
  { key: "atnm", href: "/atnm", label: "ATNM", hint: "Client ATNM server → RDS copy" },
  { key: "rds", href: "/", label: "RDS", hint: "RDS sources → AlTasnimBI" },
] as const;

export function SectionTabs({ current }: { current: "atnm" | "rds" }) {
  return (
    <nav aria-label="Section" className="flex rounded-lg border border-border bg-surface-2 p-0.5 text-xs">
      {SECTIONS.map((s) => (
        <Link
          key={s.key}
          href={s.href}
          title={s.hint}
          aria-current={current === s.key ? "page" : undefined}
          className={`rounded-md px-3 py-1 font-medium transition-colors ${
            current === s.key ? "bg-surface text-accent shadow-sm" : "text-muted hover:text-text"
          }`}
        >
          {s.label}
        </Link>
      ))}
    </nav>
  );
}
