// RotationHeatmap: replaces the static CorrelationHeatmap on Today
// (wave-3B Today task — "the corr heatmap is useless as-is; make it a
// rotation instrument"). Universe/window/lookback pickers drive
// POST /api/rotation; the underlying Plotly heatmap draw is reused from
// CorrelationHeatmap.tsx (same {symbols, matrix} shape, single Plotly
// theme — DESIGN.md), just fed the backend's CLUSTERED symbol order so
// comovers land adjacent, rather than being retired: "leave the file, just
// stop using it [directly on Today]" — this component is exactly the
// reuse that instruction anticipated.
//
// "Other side of the trade" mode: clicking a symbol's return badge sets it
// as the anchor; the backend re-scores the rest of the universe by
// (negative corr) x (positive recent return) — "where is the money
// flowing?" — typically clicked on a symbol that's down, though nothing
// here enforces that; the ranking is honest either way.
import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { request } from "../lib/api";
import { CorrelationHeatmap } from "./CorrelationHeatmap";
import { InstrumentHover } from "./InstrumentHover";

type Universe = "sectors" | "factors" | "world" | "custom";
type CorrWindow = 20 | 60 | 120;

const CORR_WINDOWS: CorrWindow[] = [20, 60, 120];
const RETURN_DAYS_PRESETS = [1, 5, 10, 21];

export interface RotationSymbolReturn {
  symbol: string;
  ret: number | null;
}

export interface OtherSideRow {
  symbol: string;
  corr: number | null;
  ret: number | null;
  score: number | null;
}

export interface RotationResponse {
  universe: string;
  symbols: string[];
  matrix: (number | null)[][];
  corr_window: number;
  return_days: number;
  returns: RotationSymbolReturn[];
  anchor: string | null;
  other_side: OtherSideRow[] | null;
  as_of: string | null;
  missing: string[];
}

function postRotation(body: {
  universe: Universe;
  symbols?: string[];
  corr_window: CorrWindow;
  return_days: number;
  anchor?: string;
}): Promise<RotationResponse> {
  return request<RotationResponse>("/api/rotation", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

function pct(x: number | null): string {
  if (x === null) return "—";
  return `${(x * 100).toFixed(2)}%`;
}

function num4(x: number | null): string {
  if (x === null) return "—";
  return x.toFixed(4);
}

export function RotationHeatmap({ onAsOf }: { onAsOf?: (asOf: string | null) => void } = {}) {
  const [universe, setUniverse] = useState<Universe>("sectors");
  const [corrWindow, setCorrWindow] = useState<CorrWindow>(60);
  const [returnDays, setReturnDays] = useState(5);
  const [customText, setCustomText] = useState("");
  const [anchor, setAnchor] = useState<string | null>(null);

  const customSymbols = useMemo(
    () =>
      customText
        .split(",")
        .map((s) => s.trim().toUpperCase())
        .filter(Boolean),
    [customText]
  );

  function selectUniverse(u: Universe) {
    setUniverse(u);
    setAnchor(null);
  }

  const enabled = universe !== "custom" || customSymbols.length > 0;

  const { data, isLoading, error } = useQuery({
    queryKey: [
      "rotation",
      universe,
      corrWindow,
      returnDays,
      universe === "custom" ? customSymbols.join(",") : null,
      anchor,
    ],
    queryFn: () =>
      postRotation({
        universe,
        symbols: universe === "custom" ? customSymbols : undefined,
        corr_window: corrWindow,
        return_days: returnDays,
        anchor: anchor ?? undefined,
      }),
    enabled,
    staleTime: 30 * 1000,
    retry: false,
  });

  // Report the data's as-of upward so Today's Rotation Panel note can carry
  // the stamp (DESIGN.md: every data panel carries one — F7).
  const asOf = data?.as_of ?? null;
  useEffect(() => {
    onAsOf?.(asOf);
  }, [asOf, onAsOf]);

  return (
    <div data-testid="rotation-heatmap">
      <div className="flex flex-wrap items-center gap-2 mb-2 text-[11px]">
        <select
          data-testid="rotation-universe"
          value={universe}
          onChange={(e) => selectUniverse(e.target.value as Universe)}
          className="bg-surface border border-hairline px-1.5 py-1 text-ink"
        >
          <option value="sectors">Sectors</option>
          <option value="factors">Factors</option>
          <option value="world">World</option>
          <option value="custom">Custom</option>
        </select>
        <select
          data-testid="rotation-corr-window"
          value={corrWindow}
          onChange={(e) => setCorrWindow(Number(e.target.value) as CorrWindow)}
          className="num bg-surface border border-hairline px-1.5 py-1 text-ink"
        >
          {CORR_WINDOWS.map((w) => (
            <option key={w} value={w}>
              {w}d corr
            </option>
          ))}
        </select>
        <select
          data-testid="rotation-return-days"
          value={returnDays}
          onChange={(e) => setReturnDays(Number(e.target.value))}
          className="num bg-surface border border-hairline px-1.5 py-1 text-ink"
        >
          {RETURN_DAYS_PRESETS.map((d) => (
            <option key={d} value={d}>
              {d}d return
            </option>
          ))}
        </select>
        {universe === "custom" && (
          <input
            data-testid="rotation-custom-symbols"
            value={customText}
            onChange={(e) => setCustomText(e.target.value)}
            placeholder="SPY, QQQ, GLD…"
            className="num bg-surface border border-hairline px-1.5 py-1 text-ink flex-1 min-w-[160px]"
          />
        )}
        {anchor && (
          <button
            type="button"
            data-testid="rotation-clear-anchor"
            onClick={() => setAnchor(null)}
            className="border border-hairline px-2 py-1 text-muted hover:text-ink"
          >
            Clear {anchor} ✕
          </button>
        )}
      </div>

      {isLoading && <p className="text-muted text-[11px]">Loading rotation…</p>}
      {error && (
        <p className="text-down text-[11px]">
          Rotation unavailable: {(error as Error).message ?? String(error)}
        </p>
      )}

      {data && data.symbols.length === 0 && (
        <p className="text-muted text-[11px]">
          No cached data for this universe yet
          {data.missing.length > 0 ? ` (missing: ${data.missing.join(", ")})` : ""}.
        </p>
      )}

      {data && data.symbols.length > 0 && (
        <>
          <CorrelationHeatmap data={{ symbols: data.symbols, matrix: data.matrix }} />

          <div className="flex flex-wrap gap-1.5 mt-2" data-testid="rotation-symbol-strip">
            {data.returns.map((r) => {
              const isUp = r.ret !== null && r.ret >= 0;
              const isAnchor = anchor === r.symbol;
              return (
                <div
                  key={r.symbol}
                  className={`flex items-center gap-1 border px-1.5 py-1 ${
                    // Anchor selection is MARKET data, never book — amber is
                    // book-only (CLAUDE.md core law), so the selected chip
                    // gets the steel market accent, not `you`.
                    isAnchor ? "border-market bg-elevated" : "border-hairline"
                  }`}
                >
                  <InstrumentHover symbol={r.symbol}>
                    <span className="text-[11px]">{r.symbol}</span>
                  </InstrumentHover>
                  <button
                    type="button"
                    data-testid={`rotation-symbol-${r.symbol}`}
                    onClick={() => setAnchor(r.symbol)}
                    title="Find the other side of the trade"
                    className={`num text-[11px] ${
                      // null return is missing data, not a loss (F5)
                      isAnchor ? "text-ink" : r.ret === null ? "text-muted" : isUp ? "text-up" : "text-down"
                    }`}
                  >
                    {pct(r.ret)}
                  </button>
                </div>
              );
            })}
          </div>

          {anchor && data.other_side && (
            <div className="mt-3 border-t border-hairline pt-2" data-testid="rotation-other-side">
              <p className="text-[10px] tracking-wider uppercase text-muted mb-1.5">
                Other side of {anchor} — where is the money flowing?
              </p>
              <div className="space-y-1">
                {data.other_side.map((row) => (
                  <div
                    key={row.symbol}
                    data-testid={`rotation-other-side-${row.symbol}`}
                    className="flex items-center justify-between gap-3 text-[11px]"
                  >
                    <InstrumentHover symbol={row.symbol}>
                      <span>{row.symbol}</span>
                    </InstrumentHover>
                    <span className="num text-muted">corr {row.corr === null ? "—" : row.corr.toFixed(2)}</span>
                    <span className={`num ${row.ret === null ? "text-muted" : row.ret >= 0 ? "text-up" : "text-down"}`}>
                      {pct(row.ret)}
                    </span>
                    <span className="num text-market">score {num4(row.score)}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
