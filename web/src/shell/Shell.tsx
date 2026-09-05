// App shell: sidebar (approved variant-C chrome) + topbar. Active nav state is
// the ONLY navigational use of the amber "you" accent (DESIGN.md).
import { Link, Outlet, useRouterState } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { CommandPalette } from "./CommandPalette";
import { readActiveBookRef } from "../lib/book";

export const PAGES = [
  { path: "/", label: "Today", navigation: true },
  { path: "/portfolio", label: "Portfolio", navigation: true },
  { path: "/risk", label: "Risk", navigation: true },
  { path: "/hedge", label: "Hedge Lab", navigation: true },
  { path: "/whatif", label: "What-If", navigation: true },
  { path: "/macro", label: "Macro", navigation: true },
  { path: "/world", label: "World", navigation: true },
  { path: "/lab", label: "Lab", navigation: true },
  { path: "/book/setup", label: "Setup", navigation: false },
] as const;

export function Shell() {
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const { data } = useQuery({ queryKey: ["brief"], queryFn: api.brief, staleTime: 60 * 60 * 1000 });
  return (
    <div className="flex min-h-screen max-w-full flex-col md:flex-row">
      <aside className="flex w-full min-w-0 shrink-0 flex-col border-b border-hairline md:w-48 md:border-r md:border-b-0">
        <div className="px-4 py-4 border-b border-hairline">
          <span className="font-semibold tracking-widest text-sm">
            QUANT<span className="text-you">MIND</span>
          </span>
        </div>
        <nav className="flex max-w-full flex-1 overflow-x-auto py-0 md:block md:py-2" aria-label="Pages">
          {PAGES.filter((page) => page.navigation).map((p) => (
            <Link
              key={p.path}
              to={p.path}
              search={() => {
                const bookRef = readActiveBookRef();
                return bookRef ? { book_ref: bookRef } : {};
              }}
              className={`qm-target block shrink-0 whitespace-nowrap border-b-2 px-3 py-2 text-[12px] md:border-b-0 md:border-l-2 md:px-4 md:py-1.5 md:text-[13px] ${
                pathname === p.path
                  ? "border-you bg-surface text-you"
                  : "border-transparent text-muted hover:text-ink"
              }`}
            >
              {p.label}
            </Link>
          ))}
        </nav>
        <div className="hidden px-4 py-3 border-t border-hairline text-[10px] text-muted num md:block">
          local · read-only
        </div>
      </aside>
      <div className="flex w-full min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between gap-3 border-b border-hairline px-3 py-2.5 sm:px-5">
          <span className="hidden text-muted text-[11px] num sm:inline">⌘K — command palette</span>
          <span className="text-muted text-[11px] num" data-testid="topbar-asof">
            {data?.as_of ? `data as of ${data.as_of.slice(0, 10)}` : ""}
          </span>
        </header>
        <main className="min-w-0 flex-1 p-3 sm:p-5">
          <Outlet />
        </main>
      </div>
      <CommandPalette />
    </div>
  );
}
