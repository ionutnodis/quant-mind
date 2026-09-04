// Panel: the approved workbench chrome — surface, hairline border, structured
// header row with title + per-panel freshness/note slot. Every zone uses this.
import type { ReactNode } from "react";

export function Panel({
  title,
  note,
  children,
  className = "",
}: {
  title: string;
  note?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`min-w-0 bg-surface border border-hairline ${className}`}>
      <header className="flex items-baseline justify-between px-3 py-2 border-b border-hairline">
        <h2 className="text-[11px] tracking-widest uppercase text-muted">{title}</h2>
        {note && <span className="text-[10px] num text-muted">{note}</span>}
      </header>
      <div className="min-w-0 overflow-x-auto p-3">{children}</div>
    </section>
  );
}

export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`animate-pulse bg-elevated rounded-sm ${className}`} />;
}
