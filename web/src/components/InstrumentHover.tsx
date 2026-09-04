// InstrumentHover: wraps a symbol with a hover tooltip (name/type/exchange/
// 1d/vol/beta) fetched from GET /api/instruments/{symbol}, and a click that
// opens the InstrumentSheet floating window. Instrument data is market data,
// never book data (DESIGN.md amber law) — steel/muted/up/down only, never
// "you". No Radix/shadcn dependency here: web/package.json isn't owned by
// this task, so the tooltip/sheet are plain positioned divs styled to match
// Panel chrome, not a new library.
import { type ReactNode, useCallback, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { request } from "../lib/api";
import type { components } from "../lib/api-types";
import { InstrumentSheet } from "./InstrumentSheet";

export type InstrumentSummary = components["schemas"]["InstrumentResponse"];

export function getInstrument(symbol: string): Promise<InstrumentSummary> {
  return request<InstrumentSummary>(`/api/instruments/${encodeURIComponent(symbol)}`);
}

function pct(x: number | null): string {
  if (x === null) return "—";
  return `${(x * 100).toFixed(2)}%`;
}

function num(x: number | null, digits = 2): string {
  if (x === null) return "—";
  return x.toFixed(digits);
}

export function InstrumentHover({
  symbol,
  change1d = null,
  children,
}: {
  symbol: string;
  change1d?: number | null;
  children: ReactNode;
}) {
  const [hovered, setHovered] = useState(false);
  const [sheetOpen, setSheetOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const closeSheet = useCallback(() => setSheetOpen(false), []);
  const tooltipId = `instrument-tooltip-${symbol}`;
  const tooltipOpen = hovered && !sheetOpen;
  const { data, isLoading } = useQuery({
    queryKey: ["instrument", symbol],
    queryFn: () => getInstrument(symbol),
    enabled: hovered || sheetOpen,
    staleTime: 5 * 60 * 1000,
    retry: false,
  });

  return (
    <span
      className="relative inline-block"
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      <button
        ref={triggerRef}
        type="button"
        data-testid={`instrument-trigger-${symbol}`}
        aria-describedby={tooltipOpen ? tooltipId : undefined}
        onFocus={() => setHovered(true)}
        onBlur={() => setHovered(false)}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.preventDefault();
            setHovered(false);
          }
        }}
        onClick={() => {
          setHovered(false);
          setSheetOpen(true);
        }}
        className="cursor-pointer decoration-dotted underline decoration-hairline underline-offset-2 hover:decoration-market text-inherit"
      >
        {children}
      </button>

      {tooltipOpen && (
        <div
          id={tooltipId}
          data-testid={`instrument-hover-${symbol}`}
          role="tooltip"
          className="absolute z-20 top-full left-0 mt-1 w-56 bg-surface border border-hairline p-2.5 text-[11px] shadow-lg"
        >
          {isLoading || !data ? (
            <p
              role="status"
              aria-live="polite"
              aria-label={`Loading ${symbol} instrument`}
              className="text-muted"
            >
              Loading {symbol}…
            </p>
          ) : (
            <>
              <div className="flex items-baseline justify-between">
                <span className="num text-ink">{data.symbol}</span>
                <span className="text-muted text-[10px]">{data.sec_type ?? "—"}</span>
              </div>
              <p className="text-muted mt-0.5 truncate" title={data.long_name ?? undefined}>
                {data.long_name ?? "No metadata cached yet"}
              </p>
              <p className="text-muted text-[10px]">
                {data.primary_exchange ?? data.exchange ?? "—"} · {data.currency ?? "—"}
              </p>
              <div className="grid grid-cols-3 gap-x-2 mt-1.5 pt-1.5 border-t border-hairline">
                <div>
                  <div className="text-[9px] tracking-wider uppercase text-muted">1D</div>
                  <div className={`num ${change1d === null ? "text-muted" : change1d >= 0 ? "text-up" : "text-down"}`}>
                    {change1d === null ? "—" : pct(change1d)}
                  </div>
                </div>
                <div>
                  <div className="text-[9px] tracking-wider uppercase text-muted">Ann. vol</div>
                  <div className="num text-market">{pct(data.ann_vol)}</div>
                </div>
                <div>
                  <div className="text-[9px] tracking-wider uppercase text-muted">
                    β·{data.beta_benchmark}
                  </div>
                  <div className="num text-market">{num(data.beta)}</div>
                </div>
              </div>
              <p className="text-muted text-[10px] mt-1.5">Click for chart & stats.</p>
            </>
          )}
        </div>
      )}

      {sheetOpen && (
        <InstrumentSheet
          symbol={symbol}
          onClose={closeSheet}
          returnFocusRef={triggerRef}
        />
      )}
    </span>
  );
}
