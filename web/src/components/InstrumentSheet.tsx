// InstrumentSheet: the floating drill-down window (DESIGN.md "sheets for
// drill-downs") — candle chart from GET /api/instruments/{symbol}/candles,
// stats + description from GET /api/instruments/{symbol}, and TradingView +
// issuer link-outs (the augmentation posture made literal, Macro.tsx
// precedent). Market data throughout — steel/muted/up/down, never amber.
import { useQuery } from "@tanstack/react-query";
import { request } from "../lib/api";
import { CandleChart, type Candle } from "./CandleChart";
import { Panel, Skeleton } from "./Panel";
import { getInstrument } from "./InstrumentHover";

interface CandlesResponse {
  symbol: string;
  days: number;
  candles: Candle[];
}

function getCandles(symbol: string, days = 180): Promise<CandlesResponse> {
  return request<CandlesResponse>(
    `/api/instruments/${encodeURIComponent(symbol)}/candles?days=${days}`
  );
}

function tradingViewUrl(symbol: string): string {
  return `https://www.tradingview.com/chart/?symbol=${encodeURIComponent(symbol)}`;
}

// Best-effort issuer/info link-out — Google Finance's SYMBOL:EXCHANGE
// convention doesn't always match IBKR's exchange codes 1:1, so this is an
// augmentation link (opens in a new tab), not a guaranteed-correct deep link.
function issuerInfoUrl(symbol: string, exchange: string | null): string {
  const q = exchange ? `${symbol}:${exchange}` : symbol;
  return `https://www.google.com/finance/quote/${encodeURIComponent(q)}`;
}

function pct(x: number | null): string {
  if (x === null) return "—";
  return `${(x * 100).toFixed(2)}%`;
}

function num(x: number | null, digits = 2): string {
  if (x === null) return "—";
  return x.toFixed(digits);
}

export function InstrumentSheet({ symbol, onClose }: { symbol: string; onClose: () => void }) {
  const instrument = useQuery({
    queryKey: ["instrument", symbol],
    queryFn: () => getInstrument(symbol),
    staleTime: 5 * 60 * 1000,
  });
  const candles = useQuery({
    queryKey: ["instrument-candles", symbol],
    queryFn: () => getCandles(symbol, 180),
    staleTime: 5 * 60 * 1000,
  });

  return (
    <div
      data-testid={`instrument-sheet-${symbol}`}
      role="dialog"
      aria-label={`${symbol} detail`}
      className="fixed inset-0 z-30 flex items-start justify-center bg-ground/70 pt-16"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="w-[560px] max-w-[92vw]">
        <Panel
          title={symbol}
          note={instrument.data?.as_of ? `as of ${instrument.data.as_of.slice(0, 10)}` : "market data"}
          className="relative"
        >
          <button
            type="button"
            data-testid="instrument-sheet-close"
            onClick={onClose}
            aria-label="Close"
            className="absolute top-2 right-3 text-muted hover:text-ink text-[13px]"
          >
            ✕
          </button>

          {instrument.isLoading || candles.isLoading ? (
            <Skeleton className="h-56" />
          ) : instrument.error ? (
            <p className="text-down text-[12px]">
              Instrument unavailable: {String((instrument.error as Error).message ?? instrument.error)}
            </p>
          ) : (
            <>
              <p className="text-muted text-[12px] mb-1">
                {instrument.data?.long_name ?? "No metadata cached yet"}
              </p>
              <p className="text-muted text-[11px] mb-3">
                {instrument.data?.sec_type ?? "—"} · {instrument.data?.exchange ?? "—"} ·{" "}
                {instrument.data?.currency ?? "—"}
                {instrument.data?.region ? ` · ${instrument.data.region}` : ""}
              </p>

              {candles.data && candles.data.candles.length > 0 ? (
                <CandleChart candles={candles.data.candles} />
              ) : (
                <p className="text-muted text-[12px]">No cached candles yet — sync the universe.</p>
              )}

              <div className="mt-3 grid grid-cols-2 gap-x-3 gap-y-2 border-t border-hairline pt-3 sm:grid-cols-4">
                <div>
                  <div className="text-[9px] tracking-wider uppercase text-muted">Last</div>
                  <div className="num text-market">{num(instrument.data?.last_close ?? null)}</div>
                </div>
                <div>
                  <div className="text-[9px] tracking-wider uppercase text-muted">52w high</div>
                  <div className="num text-market">
                    {num(instrument.data?.high_52w ?? null)}{" "}
                    <span className="text-muted text-[10px]">
                      ({pct(instrument.data?.pct_from_52w_high ?? null)})
                    </span>
                  </div>
                </div>
                <div>
                  <div className="text-[9px] tracking-wider uppercase text-muted">52w low</div>
                  <div className="num text-market">
                    {num(instrument.data?.low_52w ?? null)}{" "}
                    <span className="text-muted text-[10px]">
                      ({pct(instrument.data?.pct_from_52w_low ?? null)})
                    </span>
                  </div>
                </div>
                <div>
                  <div className="text-[9px] tracking-wider uppercase text-muted">Ann. vol</div>
                  <div className="num text-market">{pct(instrument.data?.ann_vol ?? null)}</div>
                </div>
                <div className="col-span-2">
                  <div className="text-[9px] tracking-wider uppercase text-muted">
                    β vs {instrument.data?.beta_benchmark ?? "—"}
                  </div>
                  <div className="num text-market">{num(instrument.data?.beta ?? null)}</div>
                </div>
                <div className="col-span-2">
                  <div className="text-[9px] tracking-wider uppercase text-muted">Provider</div>
                  <div className="num text-muted">{instrument.data?.provider ?? "—"}</div>
                </div>
              </div>

              <div className="flex gap-4 mt-3 pt-3 border-t border-hairline text-[11px]">
                <a
                  href={tradingViewUrl(symbol)}
                  target="_blank"
                  rel="noreferrer"
                  className="text-muted hover:text-market underline underline-offset-2"
                >
                  TradingView ↗
                </a>
                <a
                  href={issuerInfoUrl(symbol, instrument.data?.exchange ?? null)}
                  target="_blank"
                  rel="noreferrer"
                  className="text-muted hover:text-market underline underline-offset-2"
                >
                  Issuer / info ↗
                </a>
              </div>
            </>
          )}
        </Panel>
      </div>
    </div>
  );
}
