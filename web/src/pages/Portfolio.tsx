// Portfolio: the truth about the book (DESIGN.md IA #2). Ledger essentials
// (positions + cost basis + account values), delta-adjusted exposure (the
// number the user manages to), the options sleeve (Greeks + stress grid),
// expiry buckets, and the core-vs-overlay P&L attribution — all from
// GET /api/portfolio. Amber marks the user's own book everywhere here (this
// page IS the book); market facts (symbol/type/last close) stay neutral.
//
// Book source (Task A1's book-flow spine): defaults to the LIVE broker book;
// an `?book_ref=` in the URL (set by another page's "open in…" link, see
// lib/book.ts) re-points the whole page at a pinned snapshot instead — the
// stable analysis path shared by Setup and the downstream workbenches.
import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { Panel, Skeleton } from "../components/Panel";
import { PortfolioAttributionChart } from "../components/PortfolioAttributionChart";
import { PortfolioStressGrid } from "../components/PortfolioStressGrid";
import { readActiveBookRef } from "../lib/book";
import { request } from "../lib/api";
import type { components } from "../lib/api-types";

type ApiPortfolioResponse = components["schemas"]["PortfolioResponse"];
type ReconciliationPosition = ApiPortfolioResponse["positions"][number];
type PortfolioResponse = ApiPortfolioResponse;
type ExpiryBuckets = components["schemas"]["ExpiryBucketsOut"];

function fetchPortfolio(bookRef: string | null): Promise<PortfolioResponse> {
  const path = bookRef ? `/api/portfolio?book_ref=${encodeURIComponent(bookRef)}` : "/api/portfolio";
  return request<PortfolioResponse>(path);
}

function fmtNum(v: number | null | undefined, digits = 2): string {
  return v == null || !Number.isFinite(v) ? "—" : v.toFixed(digits);
}

function fmtWeight(v: number | null | undefined): string {
  return v == null ? "—" : `${(v * 100).toFixed(1)}%`;
}

function fmtPct(v: number | null | undefined, digits = 1): string {
  return v == null || !Number.isFinite(v) ? "—" : `${(v * 100).toFixed(digits)}%`;
}

function PositionIdentity({ position }: { position: ReconciliationPosition }) {
  const side = position.right === "C" ? "Call" : position.right === "P" ? "Put" : null;
  return (
    <div data-testid={`position-identity-${position.con_id}`} className="min-w-0">
      <div className="break-words text-[14px] font-medium text-ink">{position.symbol}</div>
      <div className="num mt-1 break-words text-[12px] leading-relaxed text-muted">
        conId {position.con_id} · {position.sec_type} · {position.exchange ?? "exchange —"} · ×{fmtNum(position.multiplier, 0)}
      </div>
      {position.sec_type === "OPT" && (
        <div className={`num mt-1 break-words text-[12px] leading-relaxed ${side && position.strike != null && position.expiry ? "text-market" : "text-warning"}`}>
          {side && position.strike != null && position.expiry
            ? `${side} · strike ${fmtNum(position.strike)} · expiry ${position.expiry}`
            : "Option contract terms unavailable"}
        </div>
      )}
    </div>
  );
}

function MobileLabel({ children }: { children: ReactNode }) {
  return <span className="mb-1 block text-[11px] uppercase tracking-wider text-muted lg:hidden">{children}</span>;
}

// Book P&L values are AMBER, sign carried by the number itself (fix-round-1:
// DESIGN.md's amber law covers "P&L attribution", and the established
// precedent — Lab's Apply-to-Book results, WhatIf's book-risk blocks — wraps
// book P&L in text-you regardless of sign; green/red stays reserved for
// market up/down data, e.g. Today's overnight strip).

const EXPIRY_BUCKETS: { key: keyof ExpiryBuckets; label: string }[] = [
  { key: "le_7d", label: "≤ 7d" },
  { key: "le_30d", label: "≤ 30d" },
  { key: "le_90d", label: "≤ 90d" },
  { key: "later", label: "later" },
];

export function Portfolio() {
  const bookRef = readActiveBookRef();
  const { data, isLoading, error } = useQuery({
    queryKey: ["portfolio", bookRef],
    queryFn: () => fetchPortfolio(bookRef),
    staleTime: 60 * 1000,
  });

  if (isLoading) return <Skeleton className="h-48" />;
  if (error) return <p className="text-down">Portfolio unavailable: {String(error)}</p>;
  if (!data) return null;

  const account = data.account;
  const note = `snapshot ${data.snapshot_id} · book ${bookRef ? "pinned" : "calculated"} ${data.valuation_ts.slice(0, 10)} · oldest mark ${data.market_data_as_of ?? "unavailable"}`;
  const hasNoOptionPositions = (data.options_sleeve.total_positions ?? 0) === 0;
  const valuationComplete = data.totals.valuation_status === "complete";
  const pnlComplete = data.totals.pnl_status === "complete";
  const footerMarketValue = valuationComplete
    ? data.totals.market_value
    : data.totals.priced_market_value;
  const footerUnrealizedPnl = pnlComplete
    ? data.totals.unrealized_pnl
    : data.totals.reported_unrealized_pnl;
  const fxLabel = data.fx.source
    ? data.fx.source.replaceAll("_", " ").toUpperCase()
    : "No FX conversion";
  const fxStatus = data.fx.status === "incomplete"
    ? "▲ Incomplete"
    : data.fx.status === "converted"
      ? "● Converted"
      : "◇ No conversion required";
  const fxStatusTone = data.fx.status === "incomplete"
    ? "text-warning"
    : data.fx.status === "converted"
      ? "text-up"
      : "text-market";

  return (
    <div className="mx-auto w-full max-w-[1800px] space-y-3">
      <header className="flex items-center justify-between gap-3 border-b border-hairline pb-3">
        <div>
          <div className="text-[10px] uppercase tracking-[0.18em] text-muted">Current book</div>
          <h1 className="mt-1 text-2xl font-medium">Portfolio</h1>
        </div>
        <a
          href="/book/setup"
          className="qm-target num inline-flex items-center border border-hairline px-3 py-1.5 text-[14px] text-muted hover:border-market hover:text-ink"
        >
          Setup status
        </a>
      </header>
      <div
        data-testid="fx-evidence"
        className={`flex flex-col gap-1 border px-3 py-2 text-[14px] sm:flex-row sm:items-center sm:justify-between ${
          data.fx.status === "incomplete"
            ? "border-warning/40 text-warning"
            : "border-hairline text-muted"
        }`}
      >
        <span><span className={`num mr-2 ${fxStatusTone}`}>{fxStatus}</span>{data.fx.note}</span>
        <span className="num shrink-0">
          {fxLabel}{data.fx.as_of ? ` · ${data.fx.as_of}` : ""}
        </span>
      </div>
      {/* Ledger essentials: account values */}
      <Panel title="Ledger" note={note}>
        {account ? (
          <div className="grid grid-cols-2 gap-x-4 gap-y-2 lg:grid-cols-4">
            {(
              [
                ["Net liquidation", account.net_liquidation_base, account.net_liquidation],
                ["Total cash", account.total_cash_value_base, account.total_cash_value],
                ["Gross position value", account.gross_position_value_base, account.gross_position_value],
                ["Buying power", account.buying_power_base, account.buying_power],
              ] as [string, number | null, number | null][]
            ).map(([label, baseValue, localValue]) => {
              const showBase = baseValue != null;
              return (
                <div key={label}>
                  <div className="text-[10px] tracking-wider uppercase text-muted">
                    {label} ({showBase ? data.base_currency : account.source_currency})
                  </div>
                  <div className="num text-lg text-you">
                    {fmtNum(showBase ? baseValue : localValue, 0)}
                  </div>
                </div>
              );
            })}
            {data.account_note ? (
              <p className="col-span-2 border border-warning/40 px-2 py-1.5 text-[14px] text-warning lg:col-span-4">
                ▲ {data.account_note}
              </p>
            ) : account.currency !== data.base_currency && (
              <p className="col-span-2 text-[14px] text-muted lg:col-span-4">
                Broker totals remain available in {account.currency}; the figures above are normalized to {data.base_currency}.
              </p>
            )}
          </div>
        ) : (
          <p className="text-[14px] text-muted">{data.account_note ?? "No account data."}</p>
        )}
      </Panel>

      {/* Positions table */}
      {data.positions.length === 0 ? (
        <Panel title="Positions">
          <p className="text-[14px] leading-relaxed text-muted">
            No positions were reported for this book. Confirm the active account and broker connection in{" "}
            <a href="/book/setup" className="text-market underline underline-offset-2 hover:text-ink">
              Check Setup
            </a>.
          </p>
        </Panel>
      ) : (
        <Panel title="Positions" note={`${data.totals.n_positions} positions`}>
          {(!valuationComplete || !pnlComplete) && (
            <div
              className="mb-3 space-y-1 border border-warning/40 px-3 py-2 text-[14px] leading-relaxed text-warning"
              data-testid="portfolio-completeness-warning"
            >
              {!valuationComplete && (
                <p>
                  Pricing incomplete — {data.totals.priced_positions} of {data.totals.n_positions}{" "}
                  positions priced. Total market value and portfolio weights are unavailable; the
                  footer shows the priced subtotal only.
                </p>
              )}
              {!pnlComplete && (
                <p>
                  P&amp;L incomplete — the footer shows base-currency unrealized P&amp;L only, excluding
                  positions without cost basis and foreign positions without acquisition-date FX.
                </p>
              )}
            </div>
          )}
          <div
            role="region"
            aria-label="Position valuation table"
            tabIndex={0}
            className="overflow-x-auto focus-visible:outline focus-visible:outline-1 focus-visible:outline-market"
          >
            <p className="mb-2 hidden text-[13px] text-muted lg:block xl:hidden">
              Scroll horizontally to inspect all valuation columns; position identity stays pinned.
            </p>
            <table className="block w-full text-[13px] lg:table lg:min-w-[960px] lg:table-fixed" data-testid="positions-table">
              <thead className="sr-only lg:not-sr-only lg:table-header-group">
                <tr className="border-b border-hairline text-[11px] uppercase tracking-wider text-muted">
                  <th className="sticky left-0 z-10 w-[30%] bg-surface py-2 pr-4 text-left font-normal">Position identity</th>
                  <th className="py-2 text-left font-normal">Ccy</th>
                  <th className="py-2 text-right font-normal">Qty</th>
                  <th className="py-2 text-right font-normal">Last (local)</th>
                  <th className="py-2 text-right font-normal">Avg cost (local)</th>
                  <th className="py-2 text-right font-normal">Unrealized P&amp;L ({data.base_currency})</th>
                  <th className="py-2 text-right font-normal">Market value ({data.base_currency})</th>
                  <th className="py-2 text-right font-normal">Weight</th>
                </tr>
              </thead>
              <tbody className="grid gap-3 lg:table-row-group">
                {data.positions.map((p) => (
                  <tr
                    key={`${p.con_id}-${p.symbol}-${p.qty}`}
                    className="grid grid-cols-2 gap-x-4 gap-y-3 border border-hairline p-3 lg:table-row lg:border-0 lg:p-0"
                  >
                    <td className="col-span-2 border-b border-hairline pb-3 lg:sticky lg:left-0 lg:z-10 lg:table-cell lg:border-b lg:bg-surface lg:py-2 lg:pr-4">
                      <PositionIdentity position={p} />
                    </td>
                    <td className="num lg:table-cell lg:border-b lg:border-hairline/60 lg:py-2 lg:text-muted">
                      <MobileLabel>Currency</MobileLabel>{p.currency ?? "—"}
                    </td>
                    <td className="num text-right lg:table-cell lg:border-b lg:border-hairline/60 lg:py-2">
                      <MobileLabel>Quantity</MobileLabel>{fmtNum(p.qty, 0)}
                    </td>
                    <td className="num lg:table-cell lg:border-b lg:border-hairline/60 lg:py-2 lg:text-right">
                      <MobileLabel>Last (local)</MobileLabel>
                      <div>{fmtNum(p.last_close)}</div>
                      {p.mark_as_of && (
                        <div className="mt-1 text-[11px] text-muted">mark {p.mark_as_of}</div>
                      )}
                    </td>
                    <td className="num text-right lg:table-cell lg:border-b lg:border-hairline/60 lg:py-2">
                      <MobileLabel>Avg cost (local)</MobileLabel>{fmtNum(p.avg_cost)}
                    </td>
                    <td className="num text-you lg:table-cell lg:border-b lg:border-hairline/60 lg:py-2 lg:text-right">
                      <MobileLabel>Unrealized P&amp;L ({data.base_currency})</MobileLabel>
                      <div>{fmtNum(p.unrealized_pnl)}</div>
                      {p.currency && p.currency !== data.base_currency && p.unrealized_pnl_local != null && (
                        <div className="mt-1 text-[12px] text-muted">Local {fmtNum(p.unrealized_pnl_local)} {p.currency}</div>
                      )}
                    </td>
                    <td className="num text-right text-you lg:table-cell lg:border-b lg:border-hairline/60 lg:py-2">
                      <MobileLabel>Market value ({data.base_currency})</MobileLabel>
                      <div>{fmtNum(p.market_value)}</div>
                      {p.currency && p.currency !== data.base_currency && p.local_market_value != null && (
                        <div className="mt-1 text-[12px] text-muted">Local {fmtNum(p.local_market_value)} {p.currency}</div>
                      )}
                    </td>
                    <td className="num text-right text-you lg:table-cell lg:border-b lg:border-hairline/60 lg:py-2">
                      <MobileLabel>Weight</MobileLabel>{fmtWeight(p.weight)}
                    </td>
                  </tr>
                ))}
              </tbody>
              <tfoot className="mt-3 block lg:table-footer-group">
                <tr className="grid grid-cols-2 gap-3 border-t border-hairline pt-3 lg:table-row lg:p-0">
                  <td className="col-span-2 text-[14px] text-ink lg:table-cell lg:py-2" colSpan={5}>
                    {valuationComplete
                      ? `Total (${data.totals.n_positions})`
                      : `Priced subtotal (${data.totals.priced_positions}/${data.totals.n_positions})`}
                  </td>
                  <td className="num text-you lg:table-cell lg:py-2 lg:text-right" data-testid="totals-unrealized-pnl">
                    <MobileLabel>Unrealized P&amp;L ({data.base_currency})</MobileLabel>
                    <div>{fmtNum(footerUnrealizedPnl)}</div>
                    {!pnlComplete && <div className="text-[11px] text-warning">reported only</div>}
                  </td>
                  <td className="num text-right text-you lg:table-cell lg:py-2" data-testid="totals-market-value">
                    <MobileLabel>Market value ({data.base_currency})</MobileLabel>
                    <div>{fmtNum(footerMarketValue)}</div>
                    {!valuationComplete && <div className="text-[11px] text-warning">priced only</div>}
                  </td>
                  <td className="hidden lg:table-cell" />
                </tr>
              </tfoot>
            </table>
          </div>
        </Panel>
      )}

      {/* Delta-adjusted exposure */}
      <Panel title="Delta-Adjusted Exposure" note="per underlier · shares + option legs">
        {data.exposure.length === 0 ? (
          <p className="text-[14px] text-muted">No priceable positions yet.</p>
        ) : (
          <div
            role="region"
            aria-label="Delta-adjusted exposure table"
            tabIndex={0}
            className="overflow-x-auto focus-visible:outline focus-visible:outline-1 focus-visible:outline-market"
          >
            <p className="mb-2 hidden text-[13px] text-muted lg:block xl:hidden">
              Scroll horizontally to inspect all exposure columns; underlier stays pinned.
            </p>
            <table className="block w-full text-[13px] lg:table lg:min-w-[760px] lg:table-fixed">
              <thead className="sr-only lg:not-sr-only lg:table-header-group">
                <tr className="border-b border-hairline text-[11px] uppercase tracking-wider text-muted">
                  <th className="sticky left-0 z-10 w-[24%] bg-surface py-2 pr-4 text-left font-normal">Underlier</th>
                  <th className="py-2 text-right font-normal">Spot (local)</th>
                  <th className="py-2 text-right font-normal">Net delta</th>
                  <th className="py-2 text-right font-normal">Delta ({data.base_currency})</th>
                  <th className="py-2 text-right font-normal">Beta</th>
                  <th className="py-2 text-right font-normal">{data.base_currency} benchmark notional</th>
                </tr>
              </thead>
              <tbody className="grid gap-3 sm:grid-cols-2 lg:table-row-group">
                {data.exposure.map((e) => (
                  <tr
                    key={e.underlier}
                    className="grid grid-cols-2 gap-x-4 gap-y-3 border border-hairline p-3 lg:table-row lg:border-0 lg:p-0"
                  >
                    <td className="col-span-2 text-[14px] font-medium text-ink lg:sticky lg:left-0 lg:z-10 lg:table-cell lg:border-b lg:border-hairline/60 lg:bg-surface lg:py-2 lg:pr-4">
                      {e.underlier}
                    </td>
                    <td className="num lg:table-cell lg:border-b lg:border-hairline/60 lg:py-2 lg:text-right">
                      <MobileLabel>Spot (local)</MobileLabel>{fmtNum(e.spot)} {e.currency ?? ""}
                    </td>
                    <td className="num text-right text-you lg:table-cell lg:border-b lg:border-hairline/60 lg:py-2">
                      <MobileLabel>Net delta</MobileLabel>{fmtNum(e.net_delta)}
                    </td>
                    <td className="num text-you lg:table-cell lg:border-b lg:border-hairline/60 lg:py-2 lg:text-right">
                      <MobileLabel>Delta ({data.base_currency})</MobileLabel>{fmtNum(e.dollar_delta, 0)}
                    </td>
                    <td className="num text-right lg:table-cell lg:border-b lg:border-hairline/60 lg:py-2" title={e.beta_note ?? undefined}>
                      <MobileLabel>Beta</MobileLabel>{fmtNum(e.beta, 2)}
                    </td>
                    <td className="num col-span-2 text-you lg:table-cell lg:border-b lg:border-hairline/60 lg:py-2 lg:text-right">
                      <MobileLabel>{data.base_currency} benchmark notional</MobileLabel>{fmtNum(e.spy_equivalent_notional, 0)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      {/* Options sleeve: per-underlying Greeks + stress grid */}
      <Panel
        title="Options Sleeve"
        note={`${data.options_sleeve.priced_positions ?? 0}/${data.options_sleeve.total_positions ?? 0} priced${data.options_sleeve.chain_as_of ? ` · chain ${data.options_sleeve.chain_as_of}` : ""}`}
      >
        {!data.options_sleeve.available ? (
          <p className={`text-[14px] ${hasNoOptionPositions ? "text-market" : "text-warning"}`}>
            {data.options_sleeve.reason}
          </p>
        ) : (
          <div className="space-y-3">
            {(data.options_sleeve.status === "partial" || data.options_sleeve.chain_stale) && (
              <p className="border border-warning/40 px-3 py-2 text-[14px] leading-relaxed text-warning">
                {data.options_sleeve.chain_stale
                  ? `Option evidence is stale${data.options_sleeve.chain_age_days != null ? ` (${data.options_sleeve.chain_age_days} business days old)` : ""}. `
                  : ""}
                {data.options_sleeve.reason ?? "Refresh the option chains before relying on full-book Greeks."}
              </p>
            )}
            <table className="w-full text-[12px]">
              <thead>
                <tr className="text-[10px] tracking-wider uppercase text-muted border-b border-hairline">
                  <th className="text-left py-1.5 font-normal">Underlier</th>
                  <th className="text-right py-1.5 font-normal">Gamma</th>
                  <th className="text-right py-1.5 font-normal">Vega</th>
                  <th className="text-right py-1.5 font-normal">Theta</th>
                </tr>
              </thead>
              <tbody>
                {data.options_sleeve.underlyings.map((u) => (
                  <tr key={u.underlier} className="border-b border-hairline/60">
                    <td className="py-1.5 text-ink">{u.underlier}</td>
                    <td className="num py-1.5 text-right">{fmtNum(u.gamma, 4)}</td>
                    <td className="num py-1.5 text-right">{fmtNum(u.vega, 2)}</td>
                    <td className="num py-1.5 text-right">{fmtNum(u.theta, 2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {data.options_sleeve.stress_grid && <PortfolioStressGrid grid={data.options_sleeve.stress_grid} />}
          </div>
        )}
      </Panel>

      {/* Expiry buckets */}
      <Panel title="Expiry Buckets" note="option legs by days-to-expiry">
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          {EXPIRY_BUCKETS.map(({ key, label }) => {
            const legs = data.expiry_buckets[key];
            return (
              <div key={key}>
                <div className="text-[10px] tracking-wider uppercase text-muted mb-1">{label}</div>
                {legs.length === 0 ? (
                  <p className="text-muted text-[11px]">—</p>
                ) : (
                  <ul className="space-y-1">
                    {legs.map((leg) => (
                      <li key={`${leg.symbol}-${leg.expiry}-${leg.strike}-${leg.right}`} className="num text-[13px]">
                        {`${leg.symbol} ${leg.right}${leg.strike} ${leg.expiry} (${leg.days_to_expiry}d) qty ${fmtNum(leg.qty, 0)}`}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            );
          })}
        </div>
      </Panel>

      {/* Core-vs-overlay P&L attribution */}
      <Panel
        title="Core vs Overlay P&L"
        note={data.attribution.available ? `${data.attribution.window_days}d window · beta ${fmtNum(data.attribution.beta, 2)}` : undefined}
      >
        {!data.attribution.available ? (
          <p className="text-[14px] text-muted">{data.attribution.reason}</p>
        ) : (
          <div className="space-y-3">
            <div className="grid grid-cols-1 gap-x-4 gap-y-2 sm:grid-cols-3">
              <div>
                <div className="text-[10px] tracking-wider uppercase text-muted">Total P&L</div>
                <div className="num text-lg text-you" data-testid="attribution-total-pnl">{fmtNum(data.attribution.total_pnl, 0)}</div>
              </div>
              <div>
                <div className="text-[10px] tracking-wider uppercase text-muted">Core (beta x bench)</div>
                <div className="num text-lg text-market">
                  {fmtNum(data.attribution.core_pnl, 0)}
                  <span className="text-muted text-[11px] ml-1">{fmtPct(data.attribution.core_share)}</span>
                </div>
              </div>
              <div>
                <div className="text-[10px] tracking-wider uppercase text-muted">Overlay (residual)</div>
                <div className="num text-lg text-you">
                  {fmtNum(data.attribution.overlay_pnl, 0)}
                  <span className="text-muted text-[11px] ml-1">{fmtPct(data.attribution.overlay_share)}</span>
                </div>
              </div>
            </div>
            <PortfolioAttributionChart series={data.attribution.series} />
          </div>
        )}
      </Panel>
    </div>
  );
}
