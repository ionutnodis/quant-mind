// InstrumentSheet: the floating drill-down window (DESIGN.md "sheets for
// drill-downs") — candle chart from GET /api/instruments/{symbol}/candles,
// stats + description from GET /api/instruments/{symbol}, and TradingView +
// issuer link-outs (the augmentation posture made literal, Macro.tsx
// precedent). Market data throughout — steel/muted/up/down, never amber.
import { useQuery } from "@tanstack/react-query";
import { type RefObject, useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { request } from "../lib/api";
import { ariaIdToken } from "../lib/aria";
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

function fact(value: string | null): string {
  return value?.trim() || "—";
}

function sourcedPercentage(value: string | null): string {
  if (value === null) return "—";
  const parsed = Number(value);
  return Number.isFinite(parsed) ? `${parsed.toFixed(2)}%` : "—";
}

export function InstrumentSheet({
  symbol,
  onClose,
  returnFocusRef,
}: {
  symbol: string;
  onClose: () => void;
  returnFocusRef?: RefObject<HTMLElement | null>;
}) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
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
  const profile = instrument.data?.ucits_profile ?? null;
  const dialogTitleId = `instrument-sheet-title-${ariaIdToken(symbol)}`;

  useEffect(() => {
    const previouslyFocused = document.activeElement as HTMLElement | null;
    const returnFocus = returnFocusRef?.current ?? previouslyFocused;
    const appRoot = document.getElementById("root");
    const rootWasInert = appRoot?.hasAttribute("inert") ?? false;
    const previousOverflow = document.body.style.overflow;
    appRoot?.setAttribute("inert", "");
    document.body.style.overflow = "hidden";
    closeRef.current?.focus();

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab" || !dialogRef.current) return;
      const focusable = Array.from(
        dialogRef.current.querySelectorAll<HTMLElement>(
          'button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])',
        ),
      );
      if (focusable.length === 0) {
        event.preventDefault();
        dialogRef.current.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      document.body.style.overflow = previousOverflow;
      if (!rootWasInert) appRoot?.removeAttribute("inert");
      returnFocus?.focus();
    };
  }, [onClose, returnFocusRef]);

  return createPortal(
    <div
      ref={dialogRef}
      data-testid={`instrument-sheet-${symbol}`}
      role="dialog"
      aria-modal="true"
      aria-labelledby={dialogTitleId}
      tabIndex={-1}
      className="fixed inset-0 z-30 flex items-start justify-center overflow-y-auto bg-ground/70 px-2 py-4 sm:pt-16"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="w-full max-w-[640px]">
        <h2 id={dialogTitleId} className="sr-only">
          {symbol} instrument detail
        </h2>
        <Panel
          title={symbol}
          note={
            <span className="inline-flex items-center gap-2">
              <span>
                {instrument.data?.as_of
                  ? `as of ${instrument.data.as_of.slice(0, 10)}`
                  : "market data"}
              </span>
              <button
                ref={closeRef}
                type="button"
                data-testid="instrument-sheet-close"
                onClick={onClose}
                aria-label="Close"
                className="inline-flex min-h-11 min-w-11 shrink-0 items-center justify-center border border-transparent text-[13px] text-muted hover:text-ink focus-visible:border-market focus-visible:text-ink"
              >
                ✕
              </button>
            </span>
          }
        >
          {instrument.isLoading && !instrument.data ? (
            <div
              role="status"
              aria-live="polite"
              aria-label={`Loading ${symbol} instrument`}
            >
              <span className="sr-only">Loading {symbol} instrument metadata.</span>
              <Skeleton className="h-16" />
            </div>
          ) : instrument.error && !instrument.data ? (
            <p className="text-down text-[12px]">
              Instrument unavailable: {String((instrument.error as Error).message ?? instrument.error)}
            </p>
          ) : instrument.data ? (
            <>
              <p className="text-muted text-[12px] mb-1">
                {instrument.data.long_name ?? "No metadata cached yet"}
              </p>
              <p className="text-muted text-[11px] mb-3">
                {instrument.data.sec_type ?? "—"} · {instrument.data.primary_exchange ?? instrument.data.exchange ?? "—"} ·{" "}
                {instrument.data.currency ?? "—"}
                {instrument.data.region ? ` · ${instrument.data.region}` : ""}
                {instrument.data.isin ? ` · ${instrument.data.isin}` : ""}
              </p>
            </>
          ) : null}

          {candles.isLoading && !candles.data ? (
            <div
              role="status"
              aria-live="polite"
              aria-label={`Loading ${symbol} chart`}
            >
              <span className="sr-only">Loading {symbol} chart.</span>
              <Skeleton className="h-40" />
            </div>
          ) : candles.error && !candles.data ? (
            <div className="flex flex-wrap items-center gap-3 text-[12px] text-down" role="alert">
              <span>× Candle data failed to load.</span>
              <button
                type="button"
                className="qm-target border border-down/40 px-3 py-1.5 hover:border-down"
                onClick={() => void candles.refetch()}
              >
                Retry
              </button>
            </div>
          ) : candles.data && candles.data.candles.length > 0 ? (
            <CandleChart candles={candles.data.candles} />
          ) : (
            <p className="text-muted text-[12px]">No cached candles yet — sync the universe.</p>
          )}

          {instrument.data ? (
            <>
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

              {instrument.data.risk ? (
                <section
                  className="mt-3 border-t border-hairline pt-3"
                  aria-label="Risk evidence"
                >
                  <div
                    className={`text-[10px] uppercase tracking-widest ${
                      instrument.data.risk.status === "ready" ? "text-up" : "text-warning"
                    }`}
                  >
                    Risk {instrument.data.risk.status}
                  </div>
                  <p className="mt-1 text-[12px] text-muted">
                    {instrument.data.risk.note}
                  </p>
                  <p className="num mt-1 text-[11px] text-muted">
                    Reporting {instrument.data.risk.base_currency} · FX{" "}
                    {instrument.data.risk.fx.source ?? "identity"} · as of{" "}
                    {instrument.data.risk.fx.as_of ?? "not required"}
                  </p>
                </section>
              ) : null}

              {profile ? (
                <section className="mt-3 border-t border-hairline pt-3" aria-label="European ETF sourced profile">
                  <div className="flex flex-wrap items-baseline justify-between gap-2">
                    <h3 className="text-base uppercase tracking-widest text-muted md:text-[12px]">European ETF sourced profile</h3>
                    <span className="num text-base text-up md:text-[12px]">● Source cache fresh</span>
                  </div>
                  <p className="mt-1 text-base text-ink md:text-[12px]">{fact(profile.fund_name)}</p>
                  <div className="mt-2 grid grid-cols-2 gap-x-3 gap-y-2 sm:grid-cols-4">
                    {[
                      ["Issuer", fact(profile.issuer)],
                      ["TER", sourcedPercentage(profile.ter_pct)],
                      ["Distribution", profile.distribution_policy.toLowerCase()],
                      ["Domicile", fact(profile.domicile)],
                      ["Replication", fact(profile.replication_method)],
                      ["Benchmark", fact(profile.benchmark_name)],
                    ].map(([label, value]) => (
                      <div key={label} className={label === "Replication" || label === "Benchmark" ? "col-span-2" : ""}>
                        <div className="text-base uppercase tracking-wider text-muted md:text-[12px]">{label}</div>
                        <div className="text-base text-market md:text-[13px]">{value}</div>
                      </div>
                    ))}
                  </div>
                  <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-base text-muted md:text-[12px]">
                    <span className="num">ISIN {profile.isin}</span>
                    <span>checked {profile.provenance.fetched_at_utc.slice(0, 10)}</span>
                    <a
                      href={profile.provenance.source_url}
                      target="_blank"
                      rel="noreferrer"
                      className="underline underline-offset-2 hover:text-market"
                    >
                      justETF source ↗
                    </a>
                  </div>
                </section>
              ) : instrument.data?.ucits_profile_status ? (
                <section className="mt-3 border-t border-hairline pt-3" aria-label="European ETF sourced-profile status">
                  <div
                    className={`text-base uppercase tracking-widest md:text-[12px] ${instrument.data.ucits_profile_status === "STALE" ? "text-warning" : "text-down"}`}
                  >
                    {instrument.data.ucits_profile_status === "STALE" ? "▲ Stale" : "× Missing"} European ETF sourced profile
                  </div>
                  <p className="mt-1 text-base text-muted md:text-[13px]">
                    {instrument.data.ucits_profile_reason ?? "Run metadata sync to refresh this profile."}
                  </p>
                </section>
              ) : null}

            </>
          ) : null}

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
              href={issuerInfoUrl(symbol, instrument.data?.primary_exchange ?? instrument.data?.exchange ?? null)}
              target="_blank"
              rel="noreferrer"
              className="text-muted hover:text-market underline underline-offset-2"
            >
              Issuer / info ↗
            </a>
          </div>
        </Panel>
      </div>
    </div>,
    document.body,
  );
}
