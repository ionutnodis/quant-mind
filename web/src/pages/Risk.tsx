// Risk: the risk-decomposition workbench (DESIGN.md IA #3). CAPM is the base
// case — pick a symbol, its beta vs the benchmark is a scatter + OLS fit
// line with CIs — and the factor builder generalizes it: add factors, watch
// R^2 climb and the variance/return decomposition split systematic (per
// factor) from idiosyncratic risk. Rolling beta gets long-run context (three
// windows + the full-sample line) so a single number never reads as more
// certain than it is. The tail panel connects the daily historical ES to
// horizon risk explicitly (historical sqrt-t scaling vs Monte Carlo
// block-bootstrap, side by side, each labeled with its own assumption).
// Panels follow the Today chrome (Panel + as-of notes); charts route through
// the single Plotly theme. Amber law: this page is symbol-lens market data,
// so nothing here is amber (DESIGN.md).
import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api, request } from "../lib/api";
import { Panel, Skeleton } from "../components/Panel";
import { RollingBetaChart, type BetaWindowSeries } from "../components/RollingBetaChart";
import { FanChart } from "../components/FanChart";
import { RegressionScatter } from "../components/RegressionScatter";

interface BetaPoint {
  date: string;
  beta: number | null;
}

interface RiskResponse {
  symbol: string;
  benchmark: string;
  window: number;
  years: number;
  n_obs: number;
  beta_series: BetaPoint[];
  alpha_annualized: number | null;
  alpha_note: string;
  es_975: number | null;
  ann_vol: number | null;
  mean_arith_annual: number | null;
  cagr: number | null;
  drag_exact: number | null;
  drag_approx: number | null;
  drag_note: string;
  as_of: string | null;
}

interface Histogram {
  bin_edges: number[];
  counts: number[];
}

interface MonteCarloResponse {
  symbol: string;
  horizon: number;
  n_paths: number;
  histogram: Histogram;
  p5: number | null;
  p50: number | null;
  p95: number | null;
  es_975: number | null;
}

interface ScatterPointT {
  date: string;
  asset: number | null;
  factor: number | null;
}

interface FitLine {
  factor: string;
  slope: number | null;
  slope_se: number | null;
  slope_ci: [number | null, number | null];
  intercept: number | null;
  r_squared: number | null;
}

interface BetaEstimate {
  factor: string;
  beta: number | null;
  se: number | null;
  ci_low: number | null;
  ci_high: number | null;
}

interface ShareRow {
  name: string;
  share: number | null;
}

interface AttributionRow {
  name: string;
  daily: number | null;
  annualized: number | null;
}

interface R2Step {
  factor_added: string;
  r_squared: number | null;
}

interface RegressionResponse {
  symbol: string;
  factors: string[];
  window: number | null;
  years: number;
  n_obs: number;
  hac_lags: number;
  scatter: ScatterPointT[];
  fit_line: FitLine;
  alpha_daily: number | null;
  alpha_annualized: number | null;
  alpha_se: number | null;
  alpha_ci: [number | null, number | null];
  alpha_tstat: number | null;
  information_ratio: number | null;
  alpha_note: string;
  betas: BetaEstimate[];
  r_squared: number | null;
  r_squared_progression: R2Step[];
  variance_decomposition: ShareRow[];
  attribution: AttributionRow[];
  as_of: string | null;
  horizon_note: string;
}

// Named rate-level series the store may have cached (quantmind.sources.fred)
// — offered as factor candidates alongside whatever symbols are in the
// universe. Kept in sync by eye with routers/risk.py's _RATE_LEVEL_SERIES;
// an unknown factor name is still a clean 422 from the backend either way.
const RATE_FACTORS = ["US10Y", "US2Y", "US3M"];

// Long-run context trio for the beta panel (fixed, not user-adjustable — the
// point is to always show the same three windows next to the full-sample
// line, not to let a single cherry-picked window stand alone). Wired
// directly into the three risk60/risk20/risk120 queries below.

function getRisk(symbol: string, window_: number, years: number): Promise<RiskResponse> {
  const qs = new URLSearchParams({ window: String(window_), years: String(years) });
  return request<RiskResponse>(`/api/risk/${encodeURIComponent(symbol)}?${qs}`);
}

function getRegression(
  symbol: string,
  factors: string[],
  window: number | undefined,
  years: number
): Promise<RegressionResponse> {
  const qs = new URLSearchParams({ factors: factors.join(","), years: String(years) });
  if (window !== undefined) qs.set("window", String(window));
  return request<RegressionResponse>(`/api/risk/${encodeURIComponent(symbol)}/regression?${qs}`);
}

function postMontecarlo(body: {
  symbol: string;
  horizon: number;
  n_paths: number;
  seed?: number;
}): Promise<MonteCarloResponse> {
  return request<MonteCarloResponse>("/api/risk/montecarlo", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

function pct(x: number | null | undefined): string {
  return x == null ? "—" : `${(x * 100).toFixed(2)}%`;
}

function num(x: number | null | undefined, digits = 2): string {
  return x == null ? "—" : x.toFixed(digits);
}

function ciText(lo: number | null, hi: number | null, fmt: (x: number | null) => string): string {
  if (lo === null || hi === null) return "";
  return `[${fmt(lo)}, ${fmt(hi)}]`;
}

const YEARS_BOUNDS = { min: 1, max: 25 };
const REG_WINDOW_BOUNDS = { min: 20, max: 2520 };
const HORIZON_BOUNDS = { min: 1, max: 2520 };
const PATHS_BOUNDS = { min: 1, max: 200_000 };
const TAIL_HORIZON_PRESETS = [1, 10, 21, 252];

export function Risk() {
  const brief = useQuery({ queryKey: ["brief"], queryFn: api.brief, staleTime: 60 * 60 * 1000 });

  const [symbol, setSymbol] = useState<string>("");
  const [years, setYears] = useState(5);
  const [primaryFactor, setPrimaryFactor] = useState<string>("");
  const [extraFactors, setExtraFactors] = useState<string[]>([]);
  const [regWindowEnabled, setRegWindowEnabled] = useState(false);
  const [regWindow, setRegWindow] = useState(252);
  const [horizon, setHorizon] = useState(21);
  const [nPaths, setNPaths] = useState(10_000);
  const [seed, setSeed] = useState<number | undefined>(undefined);

  useEffect(() => {
    if (!symbol && brief.data && brief.data.tiles.length > 0) {
      setSymbol(brief.data.tiles[0].symbol);
    }
  }, [symbol, brief.data]);

  // Primary rolling-beta call (window=60) doubles as the source of the
  // benchmark name + daily ES/vol; the 20/120d calls below only feed the
  // beta-context chart.
  const risk60 = useQuery({
    queryKey: ["risk", symbol, 60, years],
    queryFn: () => getRisk(symbol, 60, years),
    enabled: !!symbol,
  });
  const risk20 = useQuery({
    queryKey: ["risk", symbol, 20, years],
    queryFn: () => getRisk(symbol, 20, years),
    enabled: !!symbol,
  });
  const risk120 = useQuery({
    queryKey: ["risk", symbol, 120, years],
    queryFn: () => getRisk(symbol, 120, years),
    enabled: !!symbol,
  });

  const benchmark = risk60.data?.benchmark;

  useEffect(() => {
    if (!primaryFactor && benchmark) setPrimaryFactor(benchmark);
  }, [primaryFactor, benchmark]);

  // A factor can't appear twice (the backend rejects duplicates 422) — drop
  // it from the extras the moment it becomes the primary.
  useEffect(() => {
    setExtraFactors((prev) => prev.filter((f) => f !== primaryFactor));
  }, [primaryFactor]);

  const factors = useMemo(() => [primaryFactor, ...extraFactors].filter(Boolean), [primaryFactor, extraFactors]);

  const factorCandidates = useMemo(() => {
    const fromTiles = (brief.data?.tiles ?? []).map((t) => t.symbol);
    return Array.from(new Set([...fromTiles, ...RATE_FACTORS])).sort();
  }, [brief.data]);

  const regression = useQuery({
    queryKey: ["regression", symbol, factors, regWindowEnabled ? regWindow : undefined, years],
    queryFn: () => getRegression(symbol, factors, regWindowEnabled ? regWindow : undefined, years),
    enabled: !!symbol && factors.length > 0,
  });

  // Full-sample (unwindowed) single-factor beta vs the benchmark — the
  // long-run anchor for the rolling-beta chart. Independent of the
  // regression builder's own window/factor choices above.
  const fullSampleReg = useQuery({
    queryKey: ["regression-fullsample", symbol, benchmark, years],
    queryFn: () => getRegression(symbol, [benchmark as string], undefined, years),
    enabled: !!symbol && !!benchmark,
  });

  const mc = useMutation({
    mutationFn: () => postMontecarlo({ symbol, horizon, n_paths: nPaths, seed }),
  });

  if (brief.isLoading) return <Skeleton className="h-64" />;
  if (brief.error)
    return <p className="text-down">Risk unavailable: {String(brief.error.message ?? brief.error)}</p>;
  if (!brief.data || brief.data.tiles.length === 0)
    return (
      <Panel title="Cache empty">
        <p className="text-muted">
          No market data cached yet. With IB Gateway running, sync the universe:
        </p>
        <code className="num text-ink block mt-2">uv run python -m quantmind.sync_cli</code>
      </Panel>
    );

  const betaSeries: BetaWindowSeries[] = [
    { window: 20, points: risk20.data?.beta_series ?? [] },
    { window: 60, points: risk60.data?.beta_series ?? [] },
    { window: 120, points: risk120.data?.beta_series ?? [] },
  ];
  const fullSampleBeta = fullSampleReg.data?.fit_line.slope ?? null;

  const tailHistorical =
    risk60.data?.es_975 != null ? risk60.data.es_975 * Math.sqrt(horizon) : null;

  return (
    <div className="w-full space-y-3">
      <div className="flex items-end justify-between gap-4">
        <label className="authoring-only flex-col gap-1 text-[11px] tracking-widest uppercase text-muted">
          Symbol
          <select
            aria-label="Symbol"
            className="num bg-elevated border border-hairline px-2 py-1 text-ink text-[13px] normal-case tracking-normal"
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
          >
            {brief.data.tiles.map((t) => (
              <option key={t.symbol} value={t.symbol}>
                {t.symbol}
              </option>
            ))}
          </select>
        </label>
        <label className="authoring-only flex-col gap-1 text-[10px] tracking-wider uppercase text-muted">
          Years
          <input
            aria-label="Years"
            type="number"
            className="num bg-elevated border border-hairline px-2 py-1 text-ink text-[12px] w-20"
            min={YEARS_BOUNDS.min}
            max={YEARS_BOUNDS.max}
            value={years}
            onChange={(e) =>
              setYears(Math.min(YEARS_BOUNDS.max, Math.max(YEARS_BOUNDS.min, Number(e.target.value) || YEARS_BOUNDS.min)))
            }
          />
        </label>
        <p className="text-muted text-[11px] max-w-[46ch] text-right">
          Symbol lens now; book lens when positions exist. Construct risk, then decompose it: systematic
          (factors) vs idiosyncratic (everything else).
        </p>
      </div>

      <div className="authoring-only-block">
        <Panel title="Factor builder" note={`${factors.length} factor${factors.length === 1 ? "" : "s"}`}>
          <div className="flex flex-wrap items-end gap-4">
          <label className="flex flex-col gap-1 text-[10px] tracking-wider uppercase text-muted">
            Primary factor (CAPM)
            <select
              aria-label="Primary factor"
              className="num bg-elevated border border-hairline px-2 py-1 text-ink text-[12px] normal-case tracking-normal"
              value={primaryFactor}
              onChange={(e) => setPrimaryFactor(e.target.value)}
            >
              {factorCandidates.map((f) => (
                <option key={f} value={f}>
                  {f}
                </option>
              ))}
            </select>
          </label>
          <div className="flex flex-col gap-1">
            <span className="text-[10px] tracking-wider uppercase text-muted">Additional factors</span>
            <div className="flex flex-wrap gap-1.5">
              {factorCandidates
                .filter((f) => f !== primaryFactor)
                .map((f) => {
                  const active = extraFactors.includes(f);
                  return (
                    <button
                      key={f}
                      type="button"
                      aria-pressed={active}
                      className={`num text-[11px] px-2 py-1 border rounded-sm ${
                        active ? "border-market text-ink bg-elevated" : "border-hairline text-muted hover:border-market"
                      }`}
                      onClick={() =>
                        setExtraFactors((prev) => (prev.includes(f) ? prev.filter((x) => x !== f) : [...prev, f]))
                      }
                    >
                      {f}
                    </button>
                  );
                })}
            </div>
          </div>
          <label className="flex items-center gap-2 text-[10px] tracking-wider uppercase text-muted">
            <input
              type="checkbox"
              checked={regWindowEnabled}
              onChange={(e) => setRegWindowEnabled(e.target.checked)}
            />
            Trim to window
          </label>
          {regWindowEnabled && (
            <label className="flex flex-col gap-1 text-[10px] tracking-wider uppercase text-muted">
              Regression window (obs)
              <input
                aria-label="Regression window (obs)"
                type="number"
                className="num bg-elevated border border-hairline px-2 py-1 text-ink text-[12px] w-24"
                min={REG_WINDOW_BOUNDS.min}
                max={REG_WINDOW_BOUNDS.max}
                value={regWindow}
                onChange={(e) =>
                  setRegWindow(
                    Math.min(REG_WINDOW_BOUNDS.max, Math.max(REG_WINDOW_BOUNDS.min, Number(e.target.value) || REG_WINDOW_BOUNDS.min))
                  )
                }
              />
            </label>
          )}
          </div>
        </Panel>
      </div>

      <div className="space-y-3 2xl:grid 2xl:grid-cols-12 2xl:gap-3 2xl:space-y-0">
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-[1.3fr_1fr] 2xl:contents">
        <Panel
          title="Regression"
          note={regression.data ? `${regression.data.n_obs} obs · HAC lags ${regression.data.hac_lags}` : undefined}
          className="2xl:col-span-6"
        >
          {regression.isLoading && <Skeleton className="h-72" />}
          {regression.error && (
            <p className="text-down text-[12px]">
              Regression unavailable: {String(regression.error.message ?? regression.error)}
            </p>
          )}
          {regression.data && (
            <>
              <RegressionScatter
                points={regression.data.scatter}
                slope={regression.data.fit_line.slope}
                intercept={regression.data.fit_line.intercept}
                factorLabel={regression.data.fit_line.factor}
                assetLabel={symbol}
              />
              <p className="text-muted text-[10px] mt-2">
                Scatter/fit line are the simple two-variable regression of {symbol} on{" "}
                {regression.data.fit_line.factor} alone; with more factors added, the partial beta in the
                table to the right (holding other factors fixed) differs from this line's slope — that gap
                is what the other factors are absorbing.
              </p>
            </>
          )}
        </Panel>

        <Panel
          title="Fit statistics"
          note={regression.data?.as_of ? `as of ${regression.data.as_of.slice(0, 10)}` : undefined}
          className="2xl:col-span-3"
        >
          {regression.data ? (
            <div className="grid grid-cols-2 gap-x-4 gap-y-3">
              <div>
                <div className="text-[10px] tracking-wider uppercase text-muted">
                  Slope vs {regression.data.fit_line.factor}
                </div>
                <div className="num text-lg">{num(regression.data.fit_line.slope, 3)}</div>
                <div className="num text-muted text-[10px]">
                  {ciText(regression.data.fit_line.slope_ci[0], regression.data.fit_line.slope_ci[1], (v) => num(v, 3))}
                </div>
              </div>
              <div>
                <div className="text-[10px] tracking-wider uppercase text-muted">Intercept (daily)</div>
                <div className="num text-lg">{num(regression.data.fit_line.intercept, 5)}</div>
              </div>
              <div>
                <div className="text-[10px] tracking-wider uppercase text-muted">R² (single-factor)</div>
                <div className="num text-lg">{num(regression.data.fit_line.r_squared, 3)}</div>
              </div>
              <div>
                <div className="text-[10px] tracking-wider uppercase text-muted">R² (all {factors.length} factors)</div>
                <div className="num text-lg">{num(regression.data.r_squared, 3)}</div>
              </div>
              <div className="col-span-2">
                <div className="text-[10px] tracking-wider uppercase text-muted">
                  Jensen alpha, daily / annualized
                </div>
                <div className="num text-lg">
                  {num(regression.data.alpha_daily, 5)} / {pct(regression.data.alpha_annualized)}
                </div>
                <div className="num text-muted text-[10px]">
                  {ciText(regression.data.alpha_ci[0], regression.data.alpha_ci[1], (v) => num(v, 5))} · SE{" "}
                  {num(regression.data.alpha_se, 5)}
                </div>
              </div>
              <div className="col-span-2">
                <div className="text-[10px] tracking-wider uppercase text-muted">
                  Skill vs luck — t-stat / information ratio
                </div>
                <div className="num text-lg">
                  {num(regression.data.alpha_tstat, 2)} / {num(regression.data.information_ratio, 2)}
                </div>
                <div className="num text-muted text-[10px]">
                  {regression.data.alpha_note}
                  {" · "}
                  {regression.data.alpha_tstat == null
                    ? "skill-vs-luck unavailable"
                    : Math.abs(regression.data.alpha_tstat) >= 2
                      ? "alpha is statistically distinguishable from luck (|t|≥2)"
                      : "alpha not distinguishable from luck (|t|<2)"}
                </div>
              </div>
              <div className="col-span-2 text-muted text-[10px]">{regression.data.horizon_note}</div>
            </div>
          ) : (
            <p className="text-muted text-[12px]">Awaiting regression.</p>
          )}
        </Panel>
      </div>

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-3 2xl:contents">
        <Panel title="Per-factor betas (multi-factor, HAC 95% CI)" className="2xl:col-span-3">
          {regression.data ? (
            <table className="w-full text-[12px]">
              <thead>
                <tr className="text-[10px] tracking-wider uppercase text-muted text-left">
                  <th className="font-normal">Factor</th>
                  <th className="font-normal text-right">Beta</th>
                  <th className="font-normal text-right">95% CI</th>
                </tr>
              </thead>
              <tbody>
                {regression.data.betas.map((b) => (
                  <tr key={b.factor} className="border-t border-hairline">
                    <td className="py-1">{b.factor}</td>
                    <td className="num text-right">{num(b.beta, 3)}</td>
                    <td className="num text-right text-muted">{ciText(b.ci_low, b.ci_high, (v) => num(v, 3))}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="text-muted text-[12px]">Awaiting regression.</p>
          )}
        </Panel>

        <Panel title="Variance decomposition" note="systematic vs idiosyncratic" className="2xl:col-span-3">
          {regression.data ? (
            <div className="space-y-2">
              <div className="flex h-3 w-full overflow-hidden border border-hairline">
                {regression.data.variance_decomposition.map((row) => (
                  <div
                    key={row.name}
                    className={row.name === "idiosyncratic" ? "bg-hairline" : "bg-market"}
                    style={{ width: `${Math.max(0, (row.share ?? 0) * 100)}%` }}
                    title={`${row.name}: ${pct(row.share)}`}
                  />
                ))}
              </div>
              <table className="w-full text-[12px]">
                <tbody>
                  {regression.data.variance_decomposition.map((row) => (
                    <tr key={row.name} className="border-t border-hairline">
                      <td className="py-1">{row.name}</td>
                      <td className="num text-right">{pct(row.share)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="text-muted text-[10px]">
                Exact decomposition of R²: each factor's share is beta × Cov(factor, {symbol}) / Var(
                {symbol}); idiosyncratic is the residual share (1 − R²). Sums to 100%.
              </p>
            </div>
          ) : (
            <p className="text-muted text-[12px]">Awaiting regression.</p>
          )}
        </Panel>

        <Panel title="R² progression" note="as factors are added" className="2xl:col-span-3">
          {regression.data ? (
            <table className="w-full text-[12px]">
              <thead>
                <tr className="text-[10px] tracking-wider uppercase text-muted text-left">
                  <th className="font-normal">+ Factor</th>
                  <th className="font-normal text-right">Cumulative R²</th>
                </tr>
              </thead>
              <tbody>
                {regression.data.r_squared_progression.map((step) => (
                  <tr key={step.factor_added} className="border-t border-hairline">
                    <td className="py-1">{step.factor_added}</td>
                    <td className="num text-right">{num(step.r_squared, 3)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="text-muted text-[12px]">Awaiting regression.</p>
          )}
        </Panel>
      </div>

      <Panel
        title="Return attribution"
        note="daily mean return split by source"
        className="2xl:col-span-6"
      >
        {regression.data ? (
          <table className="w-full text-[12px]">
            <thead>
              <tr className="text-[10px] tracking-wider uppercase text-muted text-left">
                <th className="font-normal">Source</th>
                <th className="font-normal text-right">Daily</th>
                <th className="font-normal text-right">Annualized</th>
              </tr>
            </thead>
            <tbody>
              {regression.data.attribution.map((row) => (
                <tr key={row.name} className="border-t border-hairline">
                  <td className="py-1">{row.name}</td>
                  <td className="num text-right">{num(row.daily, 5)}</td>
                  <td className="num text-right">{pct(row.annualized)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="text-muted text-[12px]">Awaiting regression.</p>
        )}
      </Panel>
      </div>

      <div className="space-y-3 2xl:grid 2xl:grid-cols-12 2xl:gap-3 2xl:space-y-0">
      <Panel
        title="Rolling beta, with long-run context"
        note={risk60.data?.as_of ? `as of ${risk60.data.as_of.slice(0, 10)}` : undefined}
        className="2xl:col-span-6"
      >
        {(risk20.isLoading || risk60.isLoading || risk120.isLoading) && <Skeleton className="h-64" />}
        {risk60.error && (
          <p className="text-down text-[12px]">Risk series unavailable: {String(risk60.error.message ?? risk60.error)}</p>
        )}
        {risk60.data && (
          <>
            <RollingBetaChart series={betaSeries} fullSampleBeta={fullSampleBeta} benchmark={risk60.data.benchmark} />
            <p className="text-muted text-[10px] mt-2">
              Windows: 20d / 60d / 120d (lighter = shorter, noisier) against a full-sample ({years}y) beta of{" "}
              <span className="num">{num(fullSampleBeta, 2)}</span>
              {fullSampleReg.data && (
                <>
                  {" "}
                  {ciText(
                    fullSampleReg.data.fit_line.slope_ci[0],
                    fullSampleReg.data.fit_line.slope_ci[1],
                    (v) => num(v, 2)
                  )}
                </>
              )}
              . A rolling window drifting off a stable full-sample line is normal noise, not necessarily a
              regime change.
            </p>
          </>
        )}
      </Panel>

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2 2xl:contents">
        <Panel
          title="Tail risk & vol"
          note={risk60.data ? `${risk60.data.n_obs} obs, daily` : undefined}
          className="2xl:col-span-3"
        >
          <div className="grid grid-cols-2 gap-x-4 gap-y-3">
            <div>
              <div className="text-[10px] tracking-wider uppercase text-muted">ES 97.5% (1-day loss)</div>
              <div className="num text-lg">{risk60.data ? pct(risk60.data.es_975) : "—"}</div>
            </div>
            <div>
              <div className="text-[10px] tracking-wider uppercase text-muted">Ann. vol (252d)</div>
              <div className="num text-lg">{risk60.data ? pct(risk60.data.ann_vol) : "—"}</div>
            </div>
            <div>
              <div className="text-[10px] tracking-wider uppercase text-muted">Arith. mean / CAGR</div>
              <div className="num text-lg">
                {risk60.data ? pct(risk60.data.mean_arith_annual) : "—"} / {risk60.data ? pct(risk60.data.cagr) : "—"}
              </div>
            </div>
            <div>
              <div className="text-[10px] tracking-wider uppercase text-muted">Vol drag (exact / ½σ²)</div>
              <div className="num text-lg">
                {risk60.data ? pct(risk60.data.drag_exact) : "—"} / {risk60.data ? pct(risk60.data.drag_approx) : "—"}
              </div>
              <div className="num text-muted text-[10px]">
                {risk60.data?.drag_note ?? "drag = mean − CAGR, the tax volatility takes from compounding"}
              </div>
            </div>
          </div>
        </Panel>

        <Panel
          title="Horizon risk (Monte Carlo)"
          note={mc.data ? `${mc.data.n_paths.toLocaleString()} paths · ${mc.data.horizon}d` : "block-bootstrap"}
          className="2xl:col-span-3"
        >
          <p className="text-muted text-[10px] mb-3">
            Monte Carlo answers a horizon question, not a shape question: it block-bootstraps {symbol}'s own
            daily history into {horizon}-day paths, so autocorrelation/vol-clustering in the real history
            carries through. The historical figure instead scales the single-day 97.5% ES by √{horizon}{" "}
            (textbook iid scaling) — a persistent gap between the two flags fat tails or vol-clustering the
            √t shortcut can't see.
          </p>
          <div className="authoring-only mb-3 flex-wrap items-end gap-2">
            <div className="flex gap-1">
              {TAIL_HORIZON_PRESETS.map((h) => (
                <button
                  key={h}
                  type="button"
                  className={`num text-[11px] px-2 py-1 border rounded-sm ${
                    horizon === h ? "border-market text-ink bg-elevated" : "border-hairline text-muted hover:border-market"
                  }`}
                  onClick={() => setHorizon(h)}
                >
                  {h}d
                </button>
              ))}
            </div>
            <label className="flex flex-col gap-1 text-[10px] tracking-wider uppercase text-muted">
              Horizon (days)
              <input
                aria-label="Horizon (days)"
                type="number"
                className="num bg-elevated border border-hairline px-2 py-1 text-ink text-[12px] w-24"
                min={HORIZON_BOUNDS.min}
                max={HORIZON_BOUNDS.max}
                value={horizon}
                onChange={(e) =>
                  setHorizon(Math.min(HORIZON_BOUNDS.max, Math.max(HORIZON_BOUNDS.min, Number(e.target.value) || HORIZON_BOUNDS.min)))
                }
              />
            </label>
            <label className="flex flex-col gap-1 text-[10px] tracking-wider uppercase text-muted">
              Paths
              <input
                aria-label="Paths"
                type="number"
                className="num bg-elevated border border-hairline px-2 py-1 text-ink text-[12px] w-28"
                min={PATHS_BOUNDS.min}
                max={PATHS_BOUNDS.max}
                value={nPaths}
                onChange={(e) =>
                  setNPaths(Math.min(PATHS_BOUNDS.max, Math.max(PATHS_BOUNDS.min, Number(e.target.value) || PATHS_BOUNDS.min)))
                }
              />
            </label>
            <label className="flex flex-col gap-1 text-[10px] tracking-wider uppercase text-muted">
              Seed (optional)
              <input
                aria-label="Seed (optional)"
                type="number"
                className="num bg-elevated border border-hairline px-2 py-1 text-ink text-[12px] w-24"
                value={seed ?? ""}
                onChange={(e) => setSeed(e.target.value === "" ? undefined : Number(e.target.value))}
              />
            </label>
            <button
              type="button"
              className="bg-elevated border border-hairline px-3 py-1.5 text-[12px] text-ink hover:border-market disabled:opacity-50"
              disabled={!symbol || mc.isPending}
              onClick={() => mc.mutate()}
            >
              {mc.isPending ? "Running…" : "Run Monte Carlo"}
            </button>
          </div>

          <div className="grid grid-cols-2 gap-x-4 gap-y-3 mb-3">
            <div>
              <div className="text-[10px] tracking-wider uppercase text-muted">Historical ES ({horizon}d, √t-scaled)</div>
              <div className="num text-lg">{pct(tailHistorical)}</div>
            </div>
            <div>
              <div className="text-[10px] tracking-wider uppercase text-muted">MC bootstrap ES ({horizon}d)</div>
              <div className="num text-lg">{mc.data ? pct(mc.data.es_975) : "—"}</div>
            </div>
          </div>

          {!mc.data && !mc.isPending && !mc.error && (
            <p className="text-muted text-[12px]">Run to see the {horizon}-day terminal distribution.</p>
          )}
          {mc.isPending && <Skeleton className="h-60" />}
          {mc.error && (
            <p className="text-down text-[12px]">Monte Carlo failed: {String(mc.error.message ?? mc.error)}</p>
          )}
          {mc.data && (
            <>
              <FanChart histogram={mc.data.histogram} p5={mc.data.p5} p50={mc.data.p50} p95={mc.data.p95} horizon={mc.data.horizon} />
              <div className="grid grid-cols-3 gap-x-4 gap-y-3 mt-3">
                <div>
                  <div className="text-[10px] tracking-wider uppercase text-muted">p5</div>
                  <div className="num text-lg">{pct(mc.data.p5)}</div>
                </div>
                <div>
                  <div className="text-[10px] tracking-wider uppercase text-muted">p50</div>
                  <div className="num text-lg">{pct(mc.data.p50)}</div>
                </div>
                <div>
                  <div className="text-[10px] tracking-wider uppercase text-muted">p95</div>
                  <div className="num text-lg">{pct(mc.data.p95)}</div>
                </div>
              </div>
            </>
          )}
        </Panel>
      </div>
      </div>
    </div>
  );
}
