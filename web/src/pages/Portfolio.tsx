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
// only way an option leg's strike/expiry ever reaches this page (live-broker
// OPT positions can't carry them; see routers/portfolio.py's module
// docstring for the same limit).
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
  n_positions: number;
  unrealized_pnl: number | null;
}

interface Account {
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

  return (
    <div className="space-y-3 max-w-[1600px]">
      {/* Ledger essentials: account values */}
      <Panel title="Ledger" note={note}>
        {data.account ? (
          <div className="grid grid-cols-4 gap-x-4 gap-y-2">
            {(
              [
                ["Net liquidation", data.account.net_liquidation],
                ["Total cash", data.account.total_cash_value],
                ["Gross position value", data.account.gross_position_value],
                ["Buying power", data.account.buying_power],
              ] as [string, number | null][]
            ).map(([label, v]) => (
              <div key={label}>
                <div className="text-[10px] tracking-wider uppercase text-muted">{label}</div>
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
                  Total ({data.totals.n_positions})
                </td>
                <td />
                <td />
                <td />
                <td className="num py-1.5 text-right text-you" data-testid="totals-unrealized-pnl">
                  {fmtNum(data.totals.unrealized_pnl)}
                </td>
                <td className="num py-1.5 text-right text-you">{fmtNum(data.totals.market_value)}</td>
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
      <Panel title="Options Sleeve" note="Γ / vega / θ · spot x vol stress">
        {!data.options_sleeve.available ? (
          <p className="text-muted text-[12px]">{data.options_sleeve.reason}</p>
        ) : (
          <div className="space-y-3">
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
        <div className="grid grid-cols-4 gap-3">
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
            <div className="grid grid-cols-3 gap-x-4 gap-y-2">
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
