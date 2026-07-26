// Lab: the centerpiece bench (DESIGN.md IA #7). Three zones — Model | Simulate
// | Apply to Book — the ONLY page where amber marks anything besides the
// user's book, because Apply-to-Book results ARE book P&L (the amber law
// still holds). Every model on the bench is driven generically from its
// registry schema (GET /api/models): zero UI changes per new model.
//
// Pipeline: pick model + data source -> Fit (parameter estimates + CIs +
// diagnostics, full mathematical transparency) -> Simulate (percentile fan)
// -> Apply to Book (same fit piped through the exposure bridge -> P&L
// distribution). Panels before their inputs exist show structured "awaiting"
// states, never fake data (DESIGN.md empty-state honesty).
//
// Wave-3B practitioner row: Book Exposure (regress the book's daily $P&L on
// Δ US10Y bp with HAC SEs — a BOOK quantity, so its headline is amber — with
// one-click hand-off of the estimated β into Apply-to-Book's exposure) and
// the Pair Bench (EG→OU: hedge-pair discovery lives here now, moved out of
// the Hedge Lab in wave-3A — market data, steel only). Every OU fit also
// carries the half-life/displacement readout and the random-walk gate.
import { useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Panel, Skeleton } from "../components/Panel";
import { LabFanChart, PairBandsChart } from "../components/LabFanChart";
import { request } from "../lib/api";
import { getCurrentBook, readActiveBookRef } from "../lib/book";

interface ParamMeta {
  label: string;
  type: string;
}

interface LabModelSchema {
  name: string;
  label?: string;
  factor: { kind: string; units: string; dt: number };
  params: Record<string, ParamMeta>;
}

interface FitResponse {
  model_name: string;
  params: Record<string, number>;
  cis: Record<string, [number, number]>;
  diagnostics: Record<string, number>;
  n_obs: number;
}

interface SimulateResponse {
  bands: Record<string, number[]>;
  sample_paths: number[][];
  horizon: number;
  n_paths: number;
}

interface LabApplyResponse {
  histogram: { bin_edges: number[]; counts: number[] };
  mean: number | null;
  p5: number | null;
  p50: number | null;
  p95: number | null;
  es: number | null;
  horizon: number;
  n_paths: number;
  n_nonfinite: number;
}

function fetchModels() {
  return request<LabModelSchema[]>("/api/models");
}

function fitModel(name: string, symbol: string, years: number) {
  return request<FitResponse>(`/api/models/${name}/fit`, {
    method: "POST",
    body: JSON.stringify({ symbol, years }),
  });
}

function simulateModel(
  name: string,
  body: { fit: FitResponse; horizon: number; n_paths: number }
) {
  return request<SimulateResponse>(`/api/models/${name}/simulate`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

function applyToBook(body: {
  model_name: string;
  fit: FitResponse;
  horizon: number;
  n_paths: number;
  exposure: { factor_kind: string; units: string; value: number };
}) {
  return request<LabApplyResponse>("/api/lab/apply", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

// Page-scoped response types for the wave-3B practitioner endpoints
// (openapi-typescript regeneration happens after the batch; Global
// Constraints: page-local types, never edits to lib/api-types.ts).
interface BookRegressionResponse {
  factor_series: string;
  horizon: string; // "daily" — every risk number is horizon-labeled
  exposure_units: string; // "usd_per_bp"
  beta_usd_per_bp: number | null;
  beta_se: number | null;
  beta_ci: [number, number] | null;
  alpha_usd: number | null;
  alpha_se: number | null;
  r_squared: number | null;
  n_obs: number;
  hac_lags: number;
  book_gross: number | null;
  as_of: string | null;
}

interface PairResponse {
  y_symbol: string;
  x_symbol: string;
  horizon: string;
  coint_pvalue: number | null;
  hedge_ratio: number | null;
  hedge_ratio_se: number | null;
  is_cointegrated: boolean;
  dates: string[];
  spread: (number | null)[];
  mu: number | null;
  stationary_sigma: number | null;
  current_z: number | null;
  half_life_days: number | null;
  half_life_ci: [number, number] | null;
  mean_reversion_established: boolean;
  fit: FitResponse;
  n_obs: number;
  as_of: string | null;
}

async function runBookRegression(years: number): Promise<BookRegressionResponse> {
  // The active pinned snapshot (?book_ref=) wins; otherwise pull the live
  // book via /api/book/current (which auto-pins and returns a snapshot id).
  let ref = readActiveBookRef();
  if (!ref) {
    const snapshot = await getCurrentBook();
    ref = snapshot.snapshot_id;
  }
  return request<BookRegressionResponse>("/api/lab/book-regression", {
    method: "POST",
    body: JSON.stringify({ book_ref: ref, factor_series: "US10Y", years }),
  });
}

function runPair(body: { y_symbol: string; x_symbol: string; years: number }) {
  return request<PairResponse>("/api/lab/pair", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

// Mirrors quantmind.exposure.bridge._CONVERSIONS — the only exposure units
// each factor kind's units can be dimensionally converted into.
const UNIT_OPTIONS: Record<string, string[]> = {
  decimal: ["usd_per_bp"],
  return: ["usd_per_return"],
  vol_points: ["usd_per_volpt"],
};

const DIAG_LABELS: Record<string, string> = {
  adf_pvalue: "ADF p",
  aic: "AIC",
  aic_rw: "AIC (RW)",
  delta_aic: "ΔAIC (RW−OU)",
  lr_stat: "LR",
  log_likelihood: "Log-lik",
  r_squared: "R²",
};

// Diagnostics rendered by the dedicated half-life/displacement readout and
// the RW-gate banner — kept out of the generic diagnostics grid.
const READOUT_DIAG_KEYS = new Set([
  "x_last",
  "half_life_days",
  "half_life_ci_lo",
  "half_life_ci_hi",
  "displacement_sigma",
  "stationary_sigma",
  "mean_reversion",
]);

function num(x: number | null | undefined, digits = 4): string {
  if (x === null || x === undefined || !Number.isFinite(x)) return "—";
  return x.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

// "2.1σ above mean · half-life 34d (95% CI 21–61d)" — the practitioner
// readout shared by the Model zone (any OU fit) and the Pair Bench (spread).
function DisplacementReadout({
  z,
  halfLife,
  ci,
}: {
  z: number | null | undefined;
  halfLife: number | null | undefined;
  ci: [number, number] | null | undefined;
}) {
  const zVal = z !== null && z !== undefined && Number.isFinite(z) ? z : null;
  const hlVal =
    halfLife !== null && halfLife !== undefined && Number.isFinite(halfLife) ? halfLife : null;
  if (zVal === null && hlVal === null) return null;
  return (
    <div className="num text-[12px] text-ink">
      {zVal !== null && (
        <span>
          {Math.abs(zVal).toFixed(1)}σ {zVal >= 0 ? "above" : "below"} mean
        </span>
      )}
      {zVal !== null && hlVal !== null && <span className="text-muted"> · </span>}
      {hlVal !== null && (
        <span>
          half-life {Math.round(hlVal)}d
          {ci && (
            <span className="text-muted">
              {" "}
              (95% CI {Math.round(ci[0])}–{Math.round(ci[1])}d)
            </span>
          )}
        </span>
      )}
    </div>
  );
}

// Random-walk gate banner (warning tone — a caution badge, never book-amber).
function RwGateBanner({ diagnostics }: { diagnostics: Record<string, number> }) {
  if (diagnostics.mean_reversion !== 0) return null;
  return (
    <p className="text-warning text-[11px]">
      Mean reversion not established — random-walk null not rejected (ΔAIC{" "}
      {num(diagnostics.delta_aic, 1)}, LR {num(diagnostics.lr_stat, 1)}, ADF p{" "}
      {num(diagnostics.adf_pvalue, 3)}).
    </p>
  );
}

export function Lab() {
  const models = useQuery({ queryKey: ["lab-models"], queryFn: fetchModels, staleTime: Infinity });

  const [selectedName, setSelectedName] = useState("");
  const [symbol, setSymbol] = useState("SPY");
  const [years, setYears] = useState(5);
  const [horizon, setHorizon] = useState(126);
  const [nPaths, setNPaths] = useState(10_000);
  const [exposureUnits, setExposureUnits] = useState("");
  const [exposureValue, setExposureValue] = useState(-610);
  const [pairY, setPairY] = useState("QQQ");
  const [pairX, setPairX] = useState("SPY");

  const schema = useMemo(() => {
    if (!models.data || models.data.length === 0) return undefined;
    return models.data.find((m) => m.name === selectedName) ?? models.data[0];
  }, [models.data, selectedName]);
  const activeName = schema?.name ?? "";
  const allowedUnits = schema ? (UNIT_OPTIONS[schema.factor.units] ?? []) : [];
  const activeUnits = allowedUnits.includes(exposureUnits) ? exposureUnits : allowedUnits[0];

  const fit = useMutation({ mutationFn: () => fitModel(activeName, symbol, years) });
  const simulate = useMutation({
    mutationFn: () => {
      if (!fit.data) throw new Error("fit a model first");
      return simulateModel(activeName, { fit: fit.data, horizon, n_paths: nPaths });
    },
  });
  // `override` is the one-click Book Exposure hand-off: the regression's β
  // posts immediately (state setters alone would race the mutation read).
  const apply = useMutation({
    mutationFn: (override?: { units: string; value: number }) => {
      const units = override?.units ?? activeUnits;
      if (!fit.data || !schema || !units) throw new Error("fit a model first");
      return applyToBook({
        model_name: activeName,
        fit: fit.data,
        horizon,
        n_paths: nPaths,
        exposure: {
          factor_kind: schema.factor.kind,
          units,
          value: override?.value ?? exposureValue,
        },
      });
    },
  });
  const bookReg = useMutation({ mutationFn: () => runBookRegression(years) });
  const pair = useMutation({
    mutationFn: () => runPair({ y_symbol: pairY, x_symbol: pairX, years }),
  });

  const bookBeta = bookReg.data?.beta_usd_per_bp ?? null;
  const canFeedApply =
    fit.data !== undefined &&
    bookBeta !== null &&
    (schema ? (UNIT_OPTIONS[schema.factor.units] ?? []).includes("usd_per_bp") : false);

  const feedRegressionIntoApply = () => {
    if (bookBeta === null) return;
    setExposureUnits("usd_per_bp");
    setExposureValue(bookBeta);
    apply.mutate({ units: "usd_per_bp", value: bookBeta });
  };

  if (models.isLoading)
    return (
      <div className="grid grid-cols-3 gap-3">
        <Skeleton className="h-96" />
        <Skeleton className="h-96" />
        <Skeleton className="h-96" />
      </div>
    );
  if (models.error)
    return <p className="text-down">Model registry unavailable: {String(models.error)}</p>;
  if (!schema)
    return (
      <Panel title="Lab">
        <p className="text-muted">No models registered yet.</p>
      </Panel>
    );

  return (
    <div className="grid grid-cols-3 gap-3 max-w-[1600px]">
      {/* LEFT — Model */}
      <Panel title="Model" note="data source → fit">
        <div className="space-y-3">
          <div>
            <label htmlFor="lab-model-select" className="text-[10px] tracking-wider uppercase text-muted block mb-1">
              Model
            </label>
            <select
              id="lab-model-select"
              className="num w-full bg-elevated border border-hairline px-2 py-1.5 text-[12px]"
              value={activeName}
              onChange={(e) => setSelectedName(e.target.value)}
            >
              {models.data?.map((m) => (
                <option key={m.name} value={m.name}>
                  {m.label ?? m.name}
                </option>
              ))}
            </select>
          </div>

          <div className="grid grid-cols-2 gap-2">
            <div>
              <label htmlFor="lab-symbol" className="text-[10px] tracking-wider uppercase text-muted block mb-1">
                Symbol
              </label>
              <input
                id="lab-symbol"
                className="num w-full bg-elevated border border-hairline px-2 py-1.5 text-[12px]"
                value={symbol}
                onChange={(e) => setSymbol(e.target.value.toUpperCase())}
              />
            </div>
            <div>
              <label htmlFor="lab-years" className="text-[10px] tracking-wider uppercase text-muted block mb-1">
                Years
              </label>
              <input
                id="lab-years"
                type="number"
                min={0}
                max={25}
                className="num w-full bg-elevated border border-hairline px-2 py-1.5 text-[12px]"
                value={years}
                onChange={(e) => setYears(Number(e.target.value))}
              />
            </div>
          </div>

          <button
            type="button"
            className="w-full border border-hairline bg-elevated hover:bg-hairline text-[12px] py-1.5 disabled:opacity-40"
            disabled={fit.isPending}
            onClick={() => fit.mutate()}
          >
            {fit.isPending ? "Fitting…" : "Fit"}
          </button>

          {fit.isError && <p className="text-down text-[11px]">{String(fit.error.message ?? fit.error)}</p>}

          {!fit.data && !fit.isPending && (
            <p className="text-muted text-[11px] border-t border-hairline pt-2">
              Awaiting fit — pick a data source and run Fit to see parameter estimates.
            </p>
          )}

          {fit.data && (
            <div className="border-t border-hairline pt-2 space-y-2">
              <div className="text-[10px] tracking-wider uppercase text-muted">
                Estimates ({fit.data.n_obs} obs)
              </div>
              {Object.entries(fit.data.params).map(([key, value]) => {
                const meta = schema.params[key];
                const ci = fit.data!.cis[key];
                return (
                  <div key={key} className="num text-[12px]">
                    <span className="text-ink">{meta?.label ?? key}</span>
                    <span className="ml-2">{num(value)}</span>
                    {ci && (
                      <span className="text-muted ml-1">
                        ± ({num(ci[0])}, {num(ci[1])})
                      </span>
                    )}
                  </div>
                );
              })}
              {/* Practitioner readout: displacement + half-life on every fit
                  (market/model quantities — ink/steel, never amber). */}
              <DisplacementReadout
                z={fit.data.diagnostics.displacement_sigma}
                halfLife={fit.data.diagnostics.half_life_days}
                ci={
                  fit.data.diagnostics.half_life_ci_lo !== undefined &&
                  fit.data.diagnostics.half_life_ci_hi !== undefined
                    ? [fit.data.diagnostics.half_life_ci_lo, fit.data.diagnostics.half_life_ci_hi]
                    : null
                }
              />
              <RwGateBanner diagnostics={fit.data.diagnostics} />
              <div className="text-[10px] tracking-wider uppercase text-muted pt-1">Diagnostics</div>
              <div className="grid grid-cols-2 gap-x-2 gap-y-1">
                {Object.entries(fit.data.diagnostics)
                  .filter(([key]) => !READOUT_DIAG_KEYS.has(key))
                  .map(([key, value]) => (
                    <div key={key} className="num text-[11px]">
                      <span className="text-muted">{DIAG_LABELS[key] ?? key}</span>
                      <span className="ml-1 text-ink">{num(value)}</span>
                    </div>
                  ))}
              </div>
            </div>
          )}
        </div>
      </Panel>

      {/* CENTER — Simulate */}
      <Panel title="Simulate" note="steel · percentile bands">
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-2">
            <div>
              <label htmlFor="lab-horizon" className="text-[10px] tracking-wider uppercase text-muted block mb-1">
                Horizon (days)
              </label>
              <input
                id="lab-horizon"
                type="number"
                min={1}
                max={2520}
                className="num w-full bg-elevated border border-hairline px-2 py-1.5 text-[12px]"
                value={horizon}
                onChange={(e) => setHorizon(Number(e.target.value))}
              />
            </div>
            <div>
              <label htmlFor="lab-paths" className="text-[10px] tracking-wider uppercase text-muted block mb-1">
                Paths
              </label>
              <input
                id="lab-paths"
                type="number"
                min={1}
                max={200_000}
                className="num w-full bg-elevated border border-hairline px-2 py-1.5 text-[12px]"
                value={nPaths}
                onChange={(e) => setNPaths(Number(e.target.value))}
              />
            </div>
          </div>

          <button
            type="button"
            className="w-full border border-hairline bg-elevated hover:bg-hairline text-[12px] py-1.5 disabled:opacity-40"
            disabled={!fit.data || simulate.isPending}
            onClick={() => simulate.mutate()}
          >
            {simulate.isPending ? "Simulating…" : "Simulate"}
          </button>

          {simulate.isError && (
            <p className="text-down text-[11px]">{String(simulate.error.message ?? simulate.error)}</p>
          )}

          {!fit.data && (
            <p className="text-muted text-[11px] border-t border-hairline pt-2">
              Awaiting fit — the fan chart unlocks once a model is fit.
            </p>
          )}
          {fit.data && !simulate.data && (
            <p className="text-muted text-[11px] border-t border-hairline pt-2">
              Fit complete — run Simulate to see the percentile fan.
            </p>
          )}
          {simulate.data && (
            <LabFanChart bands={simulate.data.bands} samplePaths={simulate.data.sample_paths} />
          )}
        </div>
      </Panel>

      {/* RIGHT — Apply to Book (the ONLY amber zone) */}
      <Panel title="Apply to Book" note="amber · your book">
        <div className="space-y-3">
          <div>
            <span className="text-[10px] tracking-wider uppercase text-muted block mb-1">Factor</span>
            <span className="num text-[12px] text-ink">{schema.factor.kind}</span>
          </div>

          <div className="grid grid-cols-2 gap-2">
            <div>
              <label htmlFor="lab-exposure-units" className="text-[10px] tracking-wider uppercase text-muted block mb-1">
                Units
              </label>
              <select
                id="lab-exposure-units"
                className="num w-full bg-elevated border border-hairline px-2 py-1.5 text-[12px]"
                value={activeUnits ?? ""}
                onChange={(e) => setExposureUnits(e.target.value)}
                disabled={allowedUnits.length === 0}
              >
                {allowedUnits.map((u) => (
                  <option key={u} value={u}>
                    {u}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label htmlFor="lab-exposure-value" className="text-[10px] tracking-wider uppercase text-muted block mb-1">
                Exposure value
              </label>
              <input
                id="lab-exposure-value"
                type="number"
                className="num w-full bg-elevated border border-hairline px-2 py-1.5 text-[12px]"
                value={exposureValue}
                onChange={(e) => setExposureValue(Number(e.target.value))}
              />
            </div>
          </div>

          <button
            type="button"
            className="w-full border border-you/60 bg-you/10 hover:bg-you/20 text-you text-[12px] py-1.5 disabled:opacity-40 disabled:text-muted disabled:border-hairline disabled:bg-transparent"
            disabled={!fit.data || !activeUnits || apply.isPending}
            onClick={() => apply.mutate(undefined)}
          >
            {apply.isPending ? "Applying…" : "Apply"}
          </button>

          {apply.isError && (
            <p className="text-down text-[11px]">{String(apply.error.message ?? apply.error)}</p>
          )}

          {!fit.data && (
            <p className="text-muted text-[11px] border-t border-hairline pt-2">
              Awaiting fit — Apply to Book unlocks once a model is fit.
            </p>
          )}
          {fit.data && !apply.data && (
            <p className="text-muted text-[11px] border-t border-hairline pt-2">
              Set exposure and Apply to see book P&amp;L.
            </p>
          )}

          {apply.data && (
            <div data-testid="apply-results" className="text-you border-t border-hairline pt-2 space-y-2">
              <div className="grid grid-cols-2 gap-x-2 gap-y-1">
                <div className="num text-[12px]">
                  <span className="text-you/70">Mean</span>
                  <span className="ml-1">{num(apply.data.mean, 2)}</span>
                </div>
                <div className="num text-[12px]">
                  <span className="text-you/70">ES 97.5% (P&amp;L)</span>
                  <span className="ml-1">{num(apply.data.es, 2)}</span>
                </div>
                <div className="num text-[12px]">
                  <span className="text-you/70">p5</span>
                  <span className="ml-1">{num(apply.data.p5, 2)}</span>
                </div>
                <div className="num text-[12px]">
                  <span className="text-you/70">p50</span>
                  <span className="ml-1">{num(apply.data.p50, 2)}</span>
                </div>
                <div className="num text-[12px]">
                  <span className="text-you/70">p95</span>
                  <span className="ml-1">{num(apply.data.p95, 2)}</span>
                </div>
              </div>
              <div className="flex items-end gap-px h-16 bg-you/5">
                {apply.data.histogram.counts.map((c, i) => {
                  const max = Math.max(...apply.data!.histogram.counts, 1);
                  return (
                    <div
                      key={i}
                      className="flex-1 bg-you/60"
                      style={{ height: `${Math.max(4, (c / max) * 100)}%` }}
                    />
                  );
                })}
              </div>
              <p className="text-you/60 text-[10px]">
                Linear P&amp;L approximation over {apply.data.n_paths.toLocaleString()} paths, horizon{" "}
                {apply.data.horizon}d.
              </p>
              {apply.data.n_nonfinite > 0 && (
                <p className="text-warning text-[10px]">
                  {apply.data.n_nonfinite.toLocaleString()} path
                  {apply.data.n_nonfinite === 1 ? "" : "s"} produced non-finite P&amp;L and{" "}
                  {apply.data.n_nonfinite === 1 ? "was" : "were"} excluded — check fit stability in
                  the diagnostics.
                </p>
              )}
            </div>
          )}
        </div>
      </Panel>

      {/* ROW 2 — practitioner bench (wave-3B): Book Exposure | Pair Bench */}
      <Panel
        title="Book Exposure"
        note={
          bookReg.data
            ? `daily · as of ${bookReg.data.as_of?.slice(0, 10) ?? "—"}`
            : "your book · Δ US10Y"
        }
      >
        <div className="space-y-3">
          <p className="text-muted text-[11px]">
            Regresses your book&apos;s daily $P&amp;L on the daily bp change of US10Y
            (Newey-West HAC SEs). Uses ?book_ref= if pinned, otherwise the live book.
          </p>
          <button
            type="button"
            className="w-full border border-hairline bg-elevated hover:bg-hairline text-[12px] py-1.5 disabled:opacity-40"
            disabled={bookReg.isPending}
            onClick={() => bookReg.mutate()}
          >
            {bookReg.isPending ? "Regressing…" : "Regress book vs Δ US10Y"}
          </button>

          {bookReg.isError && (
            <p className="text-down text-[11px]">
              {String(bookReg.error.message ?? bookReg.error)}
            </p>
          )}
          {!bookReg.data && !bookReg.isPending && (
            <p className="text-muted text-[11px] border-t border-hairline pt-2">
              Awaiting regression — the estimated β lands directly in Apply&apos;s
              usd_per_bp exposure.
            </p>
          )}

          {bookReg.data && (
            <div
              data-testid="book-regression-results"
              className="text-you border-t border-hairline pt-2 space-y-2"
            >
              {/* Book sensitivity — THE amber quantity on this panel. */}
              <div className="num text-[14px]">
                β {num(bookReg.data.beta_usd_per_bp, 0)} $/bp
                <span className="text-you/70 text-[12px]"> ± {num(bookReg.data.beta_se, 0)}</span>
              </div>
              {bookReg.data.beta_ci && (
                <div className="num text-[11px] text-you/70">
                  95% CI ({num(bookReg.data.beta_ci[0], 0)}, {num(bookReg.data.beta_ci[1], 0)})
                  · {bookReg.data.horizon} horizon
                </div>
              )}
              <div className="grid grid-cols-2 gap-x-2 gap-y-1 num text-[11px]">
                <div>
                  <span className="text-you/70">α $/day</span>
                  <span className="ml-1">{num(bookReg.data.alpha_usd, 1)}</span>
                </div>
                <div>
                  <span className="text-you/70">R²</span>
                  <span className="ml-1">{num(bookReg.data.r_squared, 3)}</span>
                </div>
                <div>
                  <span className="text-you/70">obs (daily)</span>
                  <span className="ml-1">{bookReg.data.n_obs}</span>
                </div>
                <div>
                  <span className="text-you/70">HAC lags</span>
                  <span className="ml-1">{bookReg.data.hac_lags}</span>
                </div>
              </div>
              <button
                type="button"
                className="w-full border border-you/60 bg-you/10 hover:bg-you/20 text-you text-[12px] py-1.5 disabled:opacity-40 disabled:text-muted disabled:border-hairline disabled:bg-transparent"
                disabled={!canFeedApply || apply.isPending}
                onClick={feedRegressionIntoApply}
              >
                Use in Apply → β as usd_per_bp exposure
              </button>
              {!fit.data && (
                <p className="text-muted text-[10px]">
                  Fit a model first — the β feeds that fit&apos;s Apply-to-Book run.
                </p>
              )}
            </div>
          )}
        </div>
      </Panel>

      <Panel
        className="col-span-2"
        title="Pair Bench"
        note={
          pair.data
            ? `daily · as of ${pair.data.as_of?.slice(0, 10) ?? "—"}`
            : "EG → OU · steel"
        }
      >
        <div className="space-y-3">
          <div className="grid grid-cols-4 gap-2">
            <div>
              <label
                htmlFor="lab-pair-y"
                className="text-[10px] tracking-wider uppercase text-muted block mb-1"
              >
                Pair Y
              </label>
              <input
                id="lab-pair-y"
                className="num w-full bg-elevated border border-hairline px-2 py-1.5 text-[12px]"
                value={pairY}
                onChange={(e) => setPairY(e.target.value.toUpperCase())}
              />
            </div>
            <div>
              <label
                htmlFor="lab-pair-x"
                className="text-[10px] tracking-wider uppercase text-muted block mb-1"
              >
                Pair X
              </label>
              <input
                id="lab-pair-x"
                className="num w-full bg-elevated border border-hairline px-2 py-1.5 text-[12px]"
                value={pairX}
                onChange={(e) => setPairX(e.target.value.toUpperCase())}
              />
            </div>
            <div className="col-span-2 flex items-end">
              <button
                type="button"
                className="w-full border border-hairline bg-elevated hover:bg-hairline text-[12px] py-1.5 disabled:opacity-40"
                disabled={pair.isPending}
                onClick={() => pair.mutate()}
              >
                {pair.isPending ? "Running…" : "Run pair"}
              </button>
            </div>
          </div>

          {pair.isError && (
            <p className="text-down text-[11px]">{String(pair.error.message ?? pair.error)}</p>
          )}
          {!pair.data && !pair.isPending && (
            <p className="text-muted text-[11px] border-t border-hairline pt-2">
              Awaiting pair — Engle-Granger on the pair, OU on the spread, z-score bands.
              Hedge-pair discovery lives here now.
            </p>
          )}

          {pair.data && (
            <div className="border-t border-hairline pt-2 space-y-2">
              <div className="num text-[12px] text-ink">
                EG p = {num(pair.data.coint_pvalue, 4)}{" "}
                {pair.data.is_cointegrated ? (
                  <span className="text-muted">· cointegrated (5%)</span>
                ) : (
                  <span className="text-warning">· not cointegrated (5%)</span>
                )}
              </div>
              <div className="num text-[12px] text-ink">
                hedge ratio β = {num(pair.data.hedge_ratio)}
                <span className="text-muted">
                  {" "}
                  ± {num(pair.data.hedge_ratio_se)} · spread = {pair.data.y_symbol} − β·
                  {pair.data.x_symbol}
                </span>
              </div>
              <DisplacementReadout
                z={pair.data.current_z}
                halfLife={pair.data.half_life_days}
                ci={pair.data.half_life_ci}
              />
              {!pair.data.mean_reversion_established && (
                <p className="text-warning text-[11px]">
                  Mean reversion not established on this spread — random-walk null not
                  rejected (ΔAIC {num(pair.data.fit.diagnostics.delta_aic, 1)}, ADF p{" "}
                  {num(pair.data.fit.diagnostics.adf_pvalue, 3)}).
                </p>
              )}
              <PairBandsChart
                dates={pair.data.dates}
                values={pair.data.spread}
                mu={pair.data.mu}
                sigma={pair.data.stationary_sigma}
              />
              <p className="text-muted text-[10px]">
                {pair.data.n_obs} daily obs · OU fit on the spread: θ{" "}
                {num(pair.data.fit.params.theta, 3)}, μ {num(pair.data.fit.params.mu, 3)}, σ{" "}
                {num(pair.data.fit.params.sigma, 3)}
              </p>
            </div>
          )}
        </div>
      </Panel>
    </div>
  );
}
