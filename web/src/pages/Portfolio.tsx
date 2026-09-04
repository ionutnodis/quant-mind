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
import { Panel, Skeleton } from "../components/Panel";
import { PortfolioAttributionChart, type AttributionPoint } from "../components/PortfolioAttributionChart";
import { PortfolioStressGrid, type StressGrid } from "../components/PortfolioStressGrid";
import { readActiveBookRef } from "../lib/book";
import { request } from "../lib/api";

interface Position {
  con_id: number;
  symbol: string;
  qty: number;
  sec_type: string;
  multiplier: number;
  last_close: number | null;
  market_value: number | null;
  weight: number | null;
  avg_cost: number | null;
  unrealized_pnl: number | null;
}

interface Totals {
  market_value: number | null;
  priced_market_value: number | null;
  n_positions: number;
  priced_positions: number;
  valuation_status: "empty" | "partial" | "complete";
  unrealized_pnl: number | null;
  reported_unrealized_pnl: number | null;
  pnl_status: "empty" | "partial" | "complete";
}

interface Account {
  currency: string;
  net_liquidation: number | null;
  total_cash_value: number | null;
  gross_position_value: number | null;
  buying_power: number | null;
}

interface UnderlyingExposure {
  underlier: string;
  spot: number | null;
  net_delta: number | null;
  dollar_delta: number | null;
  beta: number | null;
  spy_equivalent_notional: number | null;
  beta_note: string | null;
}

interface SleeveUnderlying {
  underlier: string;
  gamma: number | null;
  vega: number | null;
  theta: number | null;
}

interface OptionsSleeve {
  available: boolean;
  status?: "complete" | "partial" | "unavailable";
  total_positions?: number;
  priced_positions?: number;
  missing_positions?: number;
  chain_as_of?: string | null;
  chain_age_days?: number | null;
  chain_stale?: boolean | null;
  reason: string | null;
  underlyings: SleeveUnderlying[];
  stress_grid: StressGrid | null;
}

interface ExpiryLeg {
  symbol: string;
  expiry: string;
  right: string;
  strike: number;
  qty: number;
  days_to_expiry: number;
}

interface ExpiryBuckets {
  le_7d: ExpiryLeg[];
  le_30d: ExpiryLeg[];
  le_90d: ExpiryLeg[];
  later: ExpiryLeg[];
}

interface Attribution {
  available: boolean;
  reason: string | null;
  window_days: number;
  beta: number | null;
  n_obs: number;
  total_pnl: number | null;
  core_pnl: number | null;
  overlay_pnl: number | null;
  core_share: number | null;
  overlay_share: number | null;
  series: AttributionPoint[];
}

interface PortfolioResponse {
  snapshot_id: string;
  valuation_ts: string;
  base_currency: string;
  positions: Position[];
  totals: Totals;
  account: Account | null;
  account_note: string | null;
  exposure: UnderlyingExposure[];
  options_sleeve: OptionsSleeve;
  expiry_buckets: ExpiryBuckets;
  attribution: Attribution;
}

function fetchPortfolio(bookRef: string | null): Promise<PortfolioResponse> {
  const path = bookRef ? `/api/portfolio?book_ref=${encodeURIComponent(bookRef)}` : "/api/portfolio";
  return request<PortfolioResponse>(path);
}

function fmtNum(v: number | null, digits = 2): string {
  return v === null || !Number.isFinite(v) ? "—" : v.toFixed(digits);
}

function fmtWeight(v: number | null): string {
  return v === null ? "—" : `${(v * 100).toFixed(1)}%`;
}

function fmtPct(v: number | null, digits = 1): string {
  return v === null || !Number.isFinite(v) ? "—" : `${(v * 100).toFixed(digits)}%`;
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

  const note = `snapshot ${data.snapshot_id} · as of ${data.valuation_ts.slice(0, 10)}${bookRef ? ` · book_ref ${bookRef}` : ""}`;
  const hasNoOptionPositions = (data.options_sleeve.total_positions ?? 0) === 0;
  const accountCurrency = data.account?.currency ?? data.base_currency;
  const valuationComplete = data.totals.valuation_status === "complete";
  const pnlComplete = data.totals.pnl_status === "complete";
  const footerMarketValue = valuationComplete
    ? data.totals.market_value
    : data.totals.priced_market_value;
  const footerUnrealizedPnl = pnlComplete
    ? data.totals.unrealized_pnl
    : data.totals.reported_unrealized_pnl;

  return (
    <div className="w-full space-y-3">
      <header className="flex items-center justify-between gap-3 border-b border-hairline pb-3">
        <div>
          <div className="text-[10px] uppercase tracking-[0.18em] text-muted">Current book</div>
          <h1 className="mt-1 text-2xl font-medium">Portfolio</h1>
        </div>
        <a
          href="/book/setup"
          className="qm-target num inline-flex items-center border border-hairline px-3 py-1.5 text-[12px] text-muted hover:border-market hover:text-ink"
        >
          Setup status
        </a>
      </header>
      {/* Ledger essentials: account values */}
      <Panel title="Ledger" note={note}>
        {data.account ? (
          <div className="grid grid-cols-2 gap-x-4 gap-y-2 lg:grid-cols-4">
            {(
              [
                ["Net liquidation", data.account.net_liquidation],
                ["Total cash", data.account.total_cash_value],
                ["Gross position value", data.account.gross_position_value],
                ["Buying power", data.account.buying_power],
              ] as [string, number | null][]
            ).map(([label, v]) => (
              <div key={label}>
                <div className="text-[10px] tracking-wider uppercase text-muted">
                  {label} ({accountCurrency})
                </div>
                <div className="num text-lg text-you">{fmtNum(v, 0)}</div>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-muted text-[12px]">{data.account_note ?? "No account data."}</p>
        )}
      </Panel>

      {/* Positions table */}
      {data.positions.length === 0 ? (
        <Panel title="Positions">
          <p className="text-muted">
            No positions in the paper book yet — the table fills in once the broker connects and
            reports a book.
          </p>
        </Panel>
      ) : (
        <Panel title="Positions" note={`${data.totals.n_positions} positions`}>
          {(!valuationComplete || !pnlComplete) && (
            <div
              className="mb-3 space-y-1 border border-warning/40 px-3 py-2 text-[11px] text-warning"
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
                  P&amp;L incomplete — the footer shows reported unrealized P&amp;L only, excluding
                  positions without reported cost basis.
                </p>
              )}
            </div>
          )}
          <table className="w-full text-[12px]" data-testid="positions-table">
            <thead>
              <tr className="text-[10px] tracking-wider uppercase text-muted border-b border-hairline">
                <th className="text-left py-1.5 font-normal">Symbol</th>
                <th className="text-left py-1.5 font-normal">Type</th>
                <th className="text-right py-1.5 font-normal">Qty</th>
                <th className="text-right py-1.5 font-normal">Last</th>
                <th className="text-right py-1.5 font-normal">Avg cost</th>
                <th className="text-right py-1.5 font-normal">Unrealized P&L</th>
                <th className="text-right py-1.5 font-normal">Mkt value</th>
                <th className="text-right py-1.5 font-normal">Weight</th>
              </tr>
            </thead>
            <tbody>
              {data.positions.map((p) => (
                <tr key={`${p.con_id}-${p.symbol}-${p.qty}`} className="border-b border-hairline/60">
                  <td className="py-1.5 text-ink">{p.symbol}</td>
                  <td className="py-1.5 text-muted">{p.sec_type}</td>
                  <td className="num py-1.5 text-right">{fmtNum(p.qty, 0)}</td>
                  <td className="num py-1.5 text-right">{fmtNum(p.last_close)}</td>
                  <td className="num py-1.5 text-right">{fmtNum(p.avg_cost)}</td>
                  <td className="num py-1.5 text-right text-you">{fmtNum(p.unrealized_pnl)}</td>
                  <td className="num py-1.5 text-right text-you">{fmtNum(p.market_value)}</td>
                  <td className="num py-1.5 text-right text-you">{fmtWeight(p.weight)}</td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr className="border-t border-hairline">
                <td className="py-1.5 text-ink" colSpan={2}>
                  {valuationComplete
                    ? `Total (${data.totals.n_positions})`
                    : `Priced subtotal (${data.totals.priced_positions}/${data.totals.n_positions})`}
                </td>
                <td />
                <td />
                <td />
                <td className="num py-1.5 text-right text-you" data-testid="totals-unrealized-pnl">
                  <div>{fmtNum(footerUnrealizedPnl)}</div>
                  {!pnlComplete && <div className="text-[9px] text-warning">reported only</div>}
                </td>
                <td className="num py-1.5 text-right text-you" data-testid="totals-market-value">
                  <div>{fmtNum(footerMarketValue)}</div>
                  {!valuationComplete && <div className="text-[9px] text-warning">priced only</div>}
                </td>
                <td />
              </tr>
            </tfoot>
          </table>
        </Panel>
      )}

      {/* Delta-adjusted exposure */}
      <Panel title="Delta-Adjusted Exposure" note="per underlier · shares + option legs">
        {data.exposure.length === 0 ? (
          <p className="text-muted text-[12px]">No priceable positions yet.</p>
        ) : (
          <table className="w-full text-[12px]">
            <thead>
              <tr className="text-[10px] tracking-wider uppercase text-muted border-b border-hairline">
                <th className="text-left py-1.5 font-normal">Underlier</th>
                <th className="text-right py-1.5 font-normal">Spot</th>
                <th className="text-right py-1.5 font-normal">Net delta</th>
                <th className="text-right py-1.5 font-normal">Dollar delta</th>
                <th className="text-right py-1.5 font-normal">Beta</th>
                <th className="text-right py-1.5 font-normal">SPY-equiv notional</th>
              </tr>
            </thead>
            <tbody>
              {data.exposure.map((e) => (
                <tr key={e.underlier} className="border-b border-hairline/60">
                  <td className="py-1.5 text-ink">{e.underlier}</td>
                  <td className="num py-1.5 text-right">{fmtNum(e.spot)}</td>
                  <td className="num py-1.5 text-right text-you">{fmtNum(e.net_delta)}</td>
                  <td className="num py-1.5 text-right text-you">{fmtNum(e.dollar_delta, 0)}</td>
                  <td className="num py-1.5 text-right" title={e.beta_note ?? undefined}>
                    {fmtNum(e.beta, 2)}
                  </td>
                  <td className="num py-1.5 text-right text-you">{fmtNum(e.spy_equivalent_notional, 0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>

      {/* Options sleeve: per-underlying Greeks + stress grid */}
      <Panel
        title="Options Sleeve"
        note={`${data.options_sleeve.priced_positions ?? 0}/${data.options_sleeve.total_positions ?? 0} priced${data.options_sleeve.chain_as_of ? ` · chain ${data.options_sleeve.chain_as_of}` : ""}`}
      >
        {!data.options_sleeve.available ? (
          <p className={`text-[12px] ${hasNoOptionPositions ? "text-market" : "text-warning"}`}>
            {data.options_sleeve.reason}
          </p>
        ) : (
          <div className="space-y-3">
            {(data.options_sleeve.status === "partial" || data.options_sleeve.chain_stale) && (
              <p className="border border-warning/40 px-3 py-2 text-[12px] text-warning">
                {data.options_sleeve.chain_stale
                  ? `Option evidence is stale${data.options_sleeve.chain_age_days != null ? ` (${data.options_sleeve.chain_age_days} days old)` : ""}. `
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
                      <li key={`${leg.symbol}-${leg.expiry}-${leg.strike}-${leg.right}`} className="text-[11px] num">
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
          <p className="text-muted text-[12px]">{data.attribution.reason}</p>
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
