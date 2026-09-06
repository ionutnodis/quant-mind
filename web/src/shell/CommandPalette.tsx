// ⌘K palette: the navigation spine (DESIGN.md). Pages now; Lab verbs later
// ("fit ou on 10y", "what if sell 100 qqq").
import { useEffect, useState } from "react";
import { Command } from "cmdk";
import { useNavigate } from "@tanstack/react-router";
import { PAGES } from "./Shell";
import { readActiveBookRef } from "../lib/book";

export function CommandPalette() {
  const [open, setOpen] = useState(false);
  const navigate = useNavigate();

  useEffect(() => {
    const down = (e: KeyboardEvent) => {
      if (e.key === "k" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        setOpen((o) => !o);
      }
    };
    document.addEventListener("keydown", down);
    return () => document.removeEventListener("keydown", down);
  }, []);

  if (!open) return null;
  return (
    <div
      className="fixed inset-0 bg-black/60 flex items-start justify-center pt-[18vh] z-50"
      onClick={() => setOpen(false)}
    >
      <div onClick={(e) => e.stopPropagation()} className="w-[min(440px,90vw)]" role="dialog" aria-modal="true" aria-label="Command palette">
        <Command
          label="Command palette"
          className="bg-elevated border border-hairline rounded-md overflow-hidden"
        >
          <Command.Input
            autoFocus
            placeholder="Go to…"
            className="w-full bg-transparent px-4 py-3 text-ink border-b border-hairline placeholder:text-muted focus-visible:outline focus-visible:outline-1 focus-visible:outline-market"
          />
          <Command.List className="max-h-72 overflow-auto py-1">
            <Command.Empty className="px-4 py-3 text-muted">No match.</Command.Empty>
            {PAGES.map((p) => (
              <Command.Item
                key={p.path}
                value={p.label}
                onSelect={() => {
                  setOpen(false);
                  const bookRef = readActiveBookRef();
                  navigate({ to: p.path, search: () => bookRef ? { book_ref: bookRef } : {} });
                }}
                className="px-4 py-2 text-[13px] text-ink data-[selected=true]:bg-elevated data-[selected=true]:text-ink cursor-pointer"
              >
                {p.label}
              </Command.Item>
            ))}
          </Command.List>
        </Command>
      </div>
    </div>
  );
}
