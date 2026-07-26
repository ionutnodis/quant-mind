// Macro: yields/curve, Fed net liquidity, sector & factor rotation — each
// with the user's exposure noted (DESIGN.md IA #6). Wave-3B "book-aware":
// every macro row carries the pinned book's estimated dollar response to a
// standard shock of that driver (rates +10bp, ETFs +1%, VIX +5 vol pts),
// with CI and regression-window labels. AMBER LAW: the amber (text-you)
// token marks ONLY those book-sensitivity figures; yields/curve/liquidity
// lines are market steel, returns use the conventional green/red glyph,
// regime stats are neutral. Every symbol row links OUT to TradingView — the
// augmentation posture made literal, not a chart QuantMind re-implements.
import { useQuery } from "@tanstack/react-query";
import { request } from "../lib/api";
import { readActiveBookRef } from "../lib/book";
import { InstrumentHover } from "../components/InstrumentHover";
import { Panel, Skeleton } from "../components/Panel";
import { SeriesChart, type SeriesPoint } from "../components/SeriesChart";

interface YieldsBlock {
  us10y: number | null;
  us2y: number | null;
  us3m: number | null;
  spread_2s10s: number | null;
  series: { us10y: SeriesPoint[]; us2y: SeriesPoint[]; us3m: SeriesPoint[] };
}

interface CurveTenor {
  tenor: string;
  years: number;
  today: number | null;
  m1: number | null;
  m3: number | null;
}

interface CurveBlock {
  tenors: CurveTenor[];
  spread_2s10s_today: number | null;
  spread_2s10s_m1: number | null;
  spread_2s10s_m3: number | null;
  note: string;
}

interface NetLiquidityBlock {
  latest_bn: number | null;
  series: SeriesPoint[];
  cadence_note: string;
}

interface RotationRow {
  symbol: string;
  ret_1d: number | null;
  ret_1m: number | null;
  ret_3m: number | null;
}

interface RegimeSymbolStat {
  symbol: string;
  mean_daily: number | null;
  se_daily: number | null;
}

interface RegimeBucket {
  bucket: string;
  lo: number | null;
  hi: number | null;
  n_days: number;
  rows: RegimeSymbolStat[];
}

interface RegimeRotationBlock {
  regime_note: string;
  buckets: RegimeBucket[];
  as_of: string | null;
  note: string | null;
}

interface SensitivityRow {
  driver: string;
  group: "rates" | "sectors" | "factors" | "vol";
  shock_label: string;
  dollar_response: number | null;
  se: number | null;
  ci_low: number | null;
  ci_high: number | null;
  beta: number | null;
  n_obs: number | null;
  note: string | null;
}

interface SensitivityBlock {
  book_ref: string;
  book_gross: number | null;
  excluded: string[];
  rows: SensitivityRow[];
  window_note: string;
  as_of: string | null;
  note: string | null;
}

interface MacroResponse {
  yields: YieldsBlock | null;
  curve?: CurveBlock | null;
  net_liquidity: NetLiquidityBlock | null;
  sectors: RotationRow[];
  factors: RotationRow[];
  regime_rotation?: RegimeRotationBlock | null;
  sensitivity?: SensitivityBlock | null;
  as_of: string | null;
  missing: string[];
}

function getMacro(bookRef: string | null): Promise<MacroResponse> {
  const qs = bookRef ? `?book_ref=${encodeURIComponent(bookRef)}` : "";
  return request<MacroResponse>(`/api/macro${qs}`);
}

function tradingViewUrl(symbol: string): string {
  return `https://www.tradingview.com/chart/?symbol=${encodeURIComponent(symbol)}`;
}

function pct(x: number | null): string {
  if (x === null) return "—";
  return `${(x * 100).toFixed(2)}%`;
}

function signedPct(x: number | null): string {
  if (x === null) return "—";
  const sign = x >= 0 ? "+" : "";
  return `${sign}${(x * 100).toFixed(2)}%`;
}

function fmtUsd(x: number): string {
  const sign = x < 0 ? "-" : "+";
  return `${sign}$${Math.round(Math.abs(x)).toLocaleString("en-US")}`;
}

const PIN_A_BOOK = "pin a book to see sensitivities";

// The ONE place the book accent is allowed on this page (DESIGN.md core law):
// a book-sensitivity dollar estimate + its CI. Everything else stays neutral.
function SensValue({ row }: { row: SensitivityRow }) {
  if (row.dollar_response === null) {
    return (
      <span className="num text-muted" title={row.note ?? undefined}>
        —
      </span>
    );
  }
  return (
    <span className="num text-you whitespace-nowrap">
      {fmtUsd(row.dollar_response)}{" "}
      <span className="text-[10px] opacity-70">
        [{row.ci_low === null ? "—" : fmtUsd(row.ci_low)}, {row.ci_high === null ? "—" : fmtUsd(row.ci_high)}]
      </span>
    </span>
  );
}

function ReturnCell({ value }: { value: number | null }) {
  if (value === null) return <span className="num text-muted">—</span>;
  const up = value >= 0;
  return (
    <span className={`num ${up ? "text-up" : "text-down"}`}>
      {up ? "▲" : "▼"} {(Math.abs(value) * 100).toFixed(2)}%
    </span>
  );
}

function RotationTable({
  rows,
  emptyNote,
  sensRows,
  hasBook,
}: {
  rows: RotationRow[];
  emptyNote: string;
  sensRows: SensitivityRow[];
  hasBook: boolean;
}) {
  if (rows.length === 0) return <p className="text-muted text-[12px]">{emptyNote}</p>;
  const bySymbol = new Map(sensRows.map((r) => [r.driver, r]));
  const shockLabel = sensRows[0]?.shock_label ?? "+1%";
  return (
    <>
      <table className="w-full text-[12px]">
        <thead>
          <tr className="text-[10px] tracking-wider uppercase text-muted border-b border-hairline">
            <th className="text-left font-normal py-1">Symbol</th>
            <th className="text-right font-normal py-1">1D</th>
            <th className="text-right font-normal py-1">1M</th>
            <th className="text-right font-normal py-1">3M</th>
            <th className="text-right font-normal py-1">Book / {shockLabel}</th>
            <th className="text-right font-normal py-1">TradingView</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const sens = bySymbol.get(row.symbol);
            return (
              <tr key={row.symbol} className="border-b border-hairline last:border-0">
                <td className="num py-1.5 text-ink">
                  <InstrumentHover symbol={row.symbol} change1d={row.ret_1d}>
                    {row.symbol}
                  </InstrumentHover>
                </td>
                <td className="text-right py-1.5">
                  <ReturnCell value={row.ret_1d} />
                </td>
                <td className="text-right py-1.5">
                  <ReturnCell value={row.ret_1m} />
                </td>
                <td className="text-right py-1.5">
                  <ReturnCell value={row.ret_3m} />
                </td>
                <td className="text-right py-1.5">
                  {hasBook && sens ? <SensValue row={sens} /> : <span className="num text-muted">—</span>}
                </td>
                <td className="text-right py-1.5">
                  <a
                    href={tradingViewUrl(row.symbol)}
                    target="_blank"
                    rel="noreferrer"
                    className="text-muted hover:text-market text-[11px] underline underline-offset-2"
                  >
                    chart ↗
                  </a>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {!hasBook && <p className="text-muted text-[11px] mt-2">{PIN_A_BOOK}</p>}
    </>
  );
}

// Book response to rate/vol standard shocks, shown under the yields numbers.
function BookSensitivityStrip({ sensitivity }: { sensitivity: SensitivityBlock | null }) {
  if (!sensitivity) return <p className="text-muted text-[11px] mt-3">{PIN_A_BOOK}</p>;
  const rows = sensitivity.rows.filter((r) => r.group === "rates" || r.group === "vol");
  return (
    <div className="mt-3 border-t border-hairline pt-2 space-y-1">
      {rows.length === 0 && (
        <p className="text-muted text-[11px]">{sensitivity.note ?? "no rate/vol sensitivities computable"}</p>
      )}
      {rows.map((r) => (
        <div key={r.driver} className="flex items-baseline justify-between text-[12px]">
          <span className="num text-muted">
            {r.driver} {r.shock_label} →
          </span>
          <SensValue row={r} />
        </div>
      ))}
      {sensitivity.excluded.length > 0 && (
        <p className="text-muted text-[10px]">excluded: {sensitivity.excluded.join("; ")}</p>
      )}
      <p className="text-muted text-[10px]">{sensitivity.window_note}</p>
    </div>
  );
}

const CURVE_SNAPSHOTS = [
  { key: "today" as const, label: "today", cls: "text-market", dash: undefined },
  { key: "m1" as const, label: "21d ago", cls: "text-muted", dash: undefined },
  { key: "m3" as const, label: "63d ago", cls: "text-muted opacity-50", dash: "4 3" },
];

// Today's curve overlaid with its 21/63-trading-day-ago snapshots. Market
// data throughout: steel + grays, never amber. Plain inline SVG — three
// points per line doesn't warrant a Plotly instance.
function CurveChart({ curve }: { curve: CurveBlock }) {
  const w = 320;
  const h = 110;
  const pad = 14;
  const values = curve.tenors
    .flatMap((t) => [t.today, t.m1, t.m3])
    .filter((v): v is number => v !== null);
  if (values.length < 2) return null;
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const span = hi - lo || 1e-6;
  const x = (i: number) => pad + (i * (w - 2 * pad)) / (curve.tenors.length - 1);
  const y = (v: number) => h - pad - ((v - lo) * (h - 2 * pad)) / span;
  return (
    <svg viewBox={`0 0 ${w} ${h + 14}`} className="w-full" role="img" aria-label="yield curve snapshots">
      {CURVE_SNAPSHOTS.map((s) => {
        const pts = curve.tenors
          .map((t, i) => ({ v: t[s.key], i }))
          .filter((p): p is { v: number; i: number } => p.v !== null);
        if (pts.length < 2) return null;
        return (
          <polyline
            key={s.key}
            className={s.cls}
            fill="none"
            stroke="currentColor"
            strokeWidth={s.key === "today" ? 1.8 : 1.2}
            strokeDasharray={s.dash}
            points={pts.map((p) => `${x(p.i)},${y(p.v)}`).join(" ")}
          />
        );
      })}
      {curve.tenors.map((t, i) => (
        <text
          key={t.tenor}
          x={x(i)}
          y={h + 10}
          textAnchor="middle"
          className="text-muted"
          fill="currentColor"
          fontSize={9}
        >
          {t.tenor.replace("US", "")}
        </text>
      ))}
    </svg>
  );
}

function CurvePanel({ curve, asOfNote }: { curve: CurveBlock; asOfNote: string }) {
  return (
    <Panel title="Curve" note={`${asOfNote} · ${curve.note}`}>
      <CurveChart curve={curve} />
      <table className="w-full text-[12px] mt-2">
        <thead>
          <tr className="text-[10px] tracking-wider uppercase text-muted border-b border-hairline">
            <th className="text-left font-normal py-1">Tenor</th>
            <th className="text-right font-normal py-1">today</th>
            <th className="text-right font-normal py-1">21d ago</th>
            <th className="text-right font-normal py-1">63d ago</th>
          </tr>
        </thead>
        <tbody>
          {curve.tenors.map((t) => (
            <tr key={t.tenor} className="border-b border-hairline">
              <td className="num py-1 text-muted">{t.tenor}</td>
              <td className="num text-right py-1">{pct(t.today)}</td>
              <td className="num text-right py-1 text-muted">{pct(t.m1)}</td>
              <td className="num text-right py-1 text-muted">{pct(t.m3)}</td>
            </tr>
          ))}
          {/* 2s10s highlighted: the curve's headline number — steel/ink emphasis, never amber */}
          <tr className="bg-elevated">
            <td className="num py-1 text-ink font-medium">2s10s</td>
            <td className="num text-right py-1 text-ink font-medium">{signedPct(curve.spread_2s10s_today)}</td>
            <td className="num text-right py-1">{signedPct(curve.spread_2s10s_m1)}</td>
            <td className="num text-right py-1">{signedPct(curve.spread_2s10s_m3)}</td>
          </tr>
        </tbody>
      </table>
    </Panel>
  );
}

function RegimePanel({ regime, asOfNote }: { regime: RegimeRotationBlock; asOfNote: string }) {
  return (
    <Panel title="Regime rotation" note={`${asOfNote} · ${regime.regime_note}`}>
      {regime.buckets.length === 0 ? (
        <p className="text-muted text-[12px]">
          {regime.note ?? "not enough aligned history to condition on regimes."}
        </p>
      ) : (
        <div className="grid grid-cols-3 gap-3">
          {regime.buckets.map((b) => (
            <div key={b.bucket}>
              <div className="text-[10px] tracking-wider uppercase text-muted border-b border-hairline pb-1 mb-1">
                {b.bucket} · VIX {b.lo === null ? "—" : b.lo.toFixed(1)}–{b.hi === null ? "—" : b.hi.toFixed(1)} ·{" "}
                {b.n_days}d
              </div>
              {b.rows.map((r) => (
                <div key={r.symbol} className="flex items-baseline justify-between text-[12px] py-0.5">
                  <span className="num text-ink">{r.symbol}</span>
                  <span
                    className={`num ${
                      r.mean_daily === null ? "text-muted" : r.mean_daily >= 0 ? "text-up" : "text-down"
                    }`}
                  >
                    {signedPct(r.mean_daily)} ±{r.se_daily === null ? "—" : `${(r.se_daily * 100).toFixed(2)}%`}
                  </span>
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
      <p className="text-muted text-[10px] mt-2">
        mean daily return ± SE per regime bucket; buckets are terciles of the regime variable.
      </p>
    </Panel>
  );
}

export function Macro() {
  const bookRef = readActiveBookRef();
  const { data, isLoading, error } = useQuery({
    queryKey: ["macro", bookRef],
    queryFn: () => getMacro(bookRef),
    staleTime: 60 * 60 * 1000,
  });

  if (isLoading)
    return (
      <div className="grid grid-cols-[1.4fr_1fr] gap-3">
        <Skeleton className="h-48" />
        <Skeleton className="h-48" />
        <Skeleton className="h-56 col-span-2" />
      </div>
    );
  if (error) return <p className="text-down">Macro unavailable: {String((error as Error).message ?? error)}</p>;
  if (!data) return null;

  const asOfNote = data.as_of ? `as of ${data.as_of.slice(0, 10)}` : "no cached data";
  const missingNote = data.missing.length > 0 ? `missing: ${data.missing.join(", ")}` : undefined;
  const sensitivity = data.sensitivity ?? null;
  const hasBook = sensitivity !== null;
  const sensFor = (group: SensitivityRow["group"]) =>
    sensitivity ? sensitivity.rows.filter((r) => r.group === group) : [];

  return (
    <div className="space-y-3 max-w-[1400px]">
      {missingNote && (
        <p className="text-warning text-[12px] num border border-warning/40 px-3 py-1.5">
          Some series aren't cached yet — {missingNote}. Sync to fill them in.
        </p>
      )}

      <div className="grid grid-cols-[1.4fr_1fr] gap-3">
        <Panel title="Yields" note={asOfNote}>
          {data.yields ? (
            <>
              <div className="grid grid-cols-4 gap-x-4 gap-y-2 mb-3">
                <div>
                  <div className="text-[10px] tracking-wider uppercase text-muted">US 10Y</div>
                  <div className="num text-lg">{pct(data.yields.us10y)}</div>
                </div>
                <div>
                  <div className="text-[10px] tracking-wider uppercase text-muted">US 2Y</div>
                  <div className="num text-lg">{pct(data.yields.us2y)}</div>
                </div>
                <div>
                  <div className="text-[10px] tracking-wider uppercase text-muted">US 3M</div>
                  <div className="num text-lg">{pct(data.yields.us3m)}</div>
                </div>
                <div>
                  <div className="text-[10px] tracking-wider uppercase text-muted">2s10s spread</div>
                  <div className="num text-lg">{signedPct(data.yields.spread_2s10s)}</div>
                </div>
              </div>
              <SeriesChart points={data.yields.series.us10y} label="US10Y" colorToken="market" />
              <BookSensitivityStrip sensitivity={sensitivity} />
            </>
          ) : (
            <p className="text-muted text-[12px]">No yields cached yet — sync US10Y/US2Y/US3M.</p>
          )}
        </Panel>

        <Panel
          title="Net liquidity"
          note={data.net_liquidity ? `${asOfNote} · weekly cadence` : asOfNote}
        >
          {data.net_liquidity ? (
            <>
              <div className="mb-3">
                <div className="text-[10px] tracking-wider uppercase text-muted">Latest ($bn)</div>
                <div className="num text-lg">
                  {data.net_liquidity.latest_bn === null ? "—" : data.net_liquidity.latest_bn.toFixed(1)}
                </div>
                <div className="text-muted text-[11px] mt-1">
                  Fed net liquidity — {data.net_liquidity.cadence_note} cadence, explains regimes not today's move.
                </div>
              </div>
              <SeriesChart points={data.net_liquidity.series} label="Net liquidity" colorToken="market" />
            </>
          ) : (
            <p className="text-muted text-[12px]">No net liquidity cached yet — sync NET_LIQUIDITY.</p>
          )}
        </Panel>
      </div>

      {(data.curve || data.regime_rotation) && (
        <div className="grid grid-cols-[1fr_1.4fr] gap-3">
          {data.curve && <CurvePanel curve={data.curve} asOfNote={asOfNote} />}
          {data.regime_rotation && (
            <RegimePanel
              regime={data.regime_rotation}
              asOfNote={data.regime_rotation.as_of ? `as of ${data.regime_rotation.as_of.slice(0, 10)}` : asOfNote}
            />
          )}
        </div>
      )}

      <Panel
        title="Sector rotation"
        note={hasBook && sensitivity?.as_of ? `${asOfNote} · book sens ${sensitivity.window_note}` : asOfNote}
      >
        <RotationTable
          rows={data.sectors}
          emptyNote="No sector data cached yet."
          sensRows={sensFor("sectors")}
          hasBook={hasBook}
        />
      </Panel>

      <Panel
        title="Factors"
        note={hasBook && sensitivity?.as_of ? `${asOfNote} · book sens ${sensitivity.window_note}` : asOfNote}
      >
        <RotationTable
          rows={data.factors}
          emptyNote="No factor data cached yet."
          sensRows={sensFor("factors")}
          hasBook={hasBook}
        />
      </Panel>
    </div>
  );
}
