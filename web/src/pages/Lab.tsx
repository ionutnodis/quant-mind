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
import { useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Panel, Skeleton } from "../components/Panel";
import { LabFanChart } from "../components/LabFanChart";

// ---- typed fetch helpers (api.ts is owned by another task; these are local
// to the Lab page, following its Authorization-header pattern) -------------

const TOKEN = import.meta.env.VITE_QM_TOKEN as string | undefined;

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {}),
      ...(options.headers ?? {}),
    },
  });
  if (!res.ok) {
    let detail = `${path} → ${res.status}`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body?.detail) detail = body.detail;
    } catch {
      // non-JSON error body — fall back to the status line above
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

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
  histogram: { edges: number[]; counts: number[] };
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
  log_likelihood: "Log-lik",
  r_squared: "R²",
};

function num(x: number | null | undefined, digits = 4): string {
  if (x === null || x === undefined || !Number.isFinite(x)) return "—";
  return x.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
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
  const apply = useMutation({
    mutationFn: () => {
      if (!fit.data || !schema || !activeUnits) throw new Error("fit a model first");
      return applyToBook({
        model_name: activeName,
        fit: fit.data,
        horizon,
        n_paths: nPaths,
        exposure: { factor_kind: schema.factor.kind, units: activeUnits, value: exposureValue },
      });
    },
  });

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
              <div className="text-[10px] tracking-wider uppercase text-muted pt-1">Diagnostics</div>
              <div className="grid grid-cols-2 gap-x-2 gap-y-1">
                {Object.entries(fit.data.diagnostics).map(([key, value]) => (
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
            onClick={() => apply.mutate()}
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
                  <span className="text-you/70">ES 97.5%</span>
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
    </div>
  );
}
