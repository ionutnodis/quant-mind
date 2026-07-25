// App shell: sidebar (approved variant-C chrome) + topbar. Active nav state is
// the ONLY navigational use of the amber "you" accent (DESIGN.md).
import { Link, Outlet, useRouterState } from "@tanstack/react-router";
import { CommandPalette } from "./CommandPalette";

export const PAGES = [
  { path: "/", label: "Today" },
  { path: "/portfolio", label: "Portfolio" },
  { path: "/risk", label: "Risk" },
  { path: "/hedge", label: "Hedge Lab" },
  { path: "/whatif", label: "What-If" },
  { path: "/macro", label: "Macro" },
  { path: "/lab", label: "Lab" },
] as const;

export function Shell() {
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  return (
    <div className="flex min-h-screen">
      <aside className="w-48 shrink-0 border-r border-hairline flex flex-col">
        <div className="px-4 py-4 border-b border-hairline">
          <span className="font-semibold tracking-widest text-sm">
            QUANT<span className="text-you">MIND</span>
          </span>
        </div>
        <nav className="flex-1 py-2" aria-label="Pages">
          {PAGES.map((p) => (
            <Link
              key={p.path}
              to={p.path}
              className={`block px-4 py-1.5 text-[13px] ${
                pathname === p.path
                  ? "text-you border-l-2 border-you bg-surface"
                  : "text-muted hover:text-ink"
              }`}
            >
              {p.label}
            </Link>
          ))}
        </nav>
        <div className="px-4 py-3 border-t border-hairline text-[10px] text-muted num">
          local · read-only
        </div>
      </aside>
      <div className="flex-1 flex flex-col min-w-0">
        <header className="flex items-center justify-between px-5 py-2.5 border-b border-hairline">
          <span className="text-muted text-[11px] num">⌘K — command palette</span>
          <span className="text-muted text-[11px] num" data-testid="topbar-asof" />
        </header>
        <main className="flex-1 p-5">
          <Outlet />
        </main>
      </div>
      <CommandPalette />
    </div>
  );
}
