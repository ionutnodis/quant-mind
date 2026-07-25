// Risk: symbol-lens rolling beta/alpha, ES/vol, and Monte Carlo. Panels follow
// the Today chrome (Panel + as-of notes); charts route through the single
// Plotly theme. Amber law: this page has no book yet, so nothing here is
// amber — beta line and MC histogram are market steel (DESIGN.md).
import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api, request } from "../lib/api";
import { Panel, Skeleton } from "../components/Panel";
import { RollingBetaChart } from "../components/RollingBetaChart";
import { FanChart } from "../components/FanChart";

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

function getRisk(symbol: string, window_: number, years: number): Promise<RiskResponse> {
  const qs = new URLSearchParams({ window: String(window_), years: String(years) });
  return request<RiskResponse>(`/api/risk/${encodeURIComponent(symbol)}?${qs}`);
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

function pct(x: number | null): string {
  return x === null ? "—" : `${(x * 100).toFixed(2)}%`;
}

const WINDOW_BOUNDS = { min: 5, max: 756 };
const YEARS_BOUNDS = { min: 1, max: 25 };
const HORIZON_BOUNDS = { min: 1, max: 2520 };
const PATHS_BOUNDS = { min: 1, max: 200_000 };

export function Risk() {
  const brief = useQuery({ queryKey: ["brief"], queryFn: api.brief, staleTime: 60 * 60 * 1000 });

  const [symbol, setSymbol] = useState<string>("");
  const [windowSize, setWindowSize] = useState(60);
  const [years, setYears] = useState(5);
  const [horizon, setHorizon] = useState(252);
  const [nPaths, setNPaths] = useState(10_000);
  const [seed, setSeed] = useState<number | undefined>(undefined);

  useEffect(() => {
    if (!symbol && brief.data && brief.data.tiles.length > 0) {
      setSymbol(brief.data.tiles[0].symbol);
    }
  }, [symbol, brief.data]);

  const risk = useQuery({
    queryKey: ["risk", symbol, windowSize, years],
    queryFn: () => getRisk(symbol, windowSize, years),
    enabled: !!symbol,
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

  return (
    <div className="space-y-3 max-w-[1400px]">
      <div className="flex items-end justify-between gap-4">
        <label className="flex flex-col gap-1 text-[11px] tracking-widest uppercase text-muted">
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
        <p className="text-muted text-[11px] max-w-[46ch] text-right">
          Symbol lens now; book lens when positions exist.
        </p>
      </div>

      <div className="grid grid-cols-[1.4fr_1fr] gap-3">
        <Panel title="Rolling beta" note={risk.data?.as_of ? `as of ${risk.data.as_of.slice(0, 10)}` : undefined}>
          <div className="flex gap-4 mb-3">
            <label className="flex flex-col gap-1 text-[10px] tracking-wider uppercase text-muted">
              Window (days)
              <input
                aria-label="Window (days)"
                type="number"
                className="num bg-elevated border border-hairline px-2 py-1 text-ink text-[12px] w-20"
                min={WINDOW_BOUNDS.min}
                max={WINDOW_BOUNDS.max}
                value={windowSize}
                onChange={(e) =>
                  setWindowSize(
                    Math.min(WINDOW_BOUNDS.max, Math.max(WINDOW_BOUNDS.min, Number(e.target.value) || WINDOW_BOUNDS.min))
                  )
                }
              />
            </label>
            <label className="flex flex-col gap-1 text-[10px] tracking-wider uppercase text-muted">
              Years
              <input
                aria-label="Years"
                type="number"
                className="num bg-elevated border border-hairline px-2 py-1 text-ink text-[12px] w-20"
                min={YEARS_BOUNDS.min}
                max={YEARS_BOUNDS.max}
                value={years}
                onChange={(e) =>
                  setYears(
                    Math.min(YEARS_BOUNDS.max, Math.max(YEARS_BOUNDS.min, Number(e.target.value) || YEARS_BOUNDS.min))
                  )
                }
              />
            </label>
          </div>

          {risk.isLoading && <Skeleton className="h-60" />}
          {risk.error && (
            <p className="text-down text-[12px]">
              Risk series unavailable: {String(risk.error.message ?? risk.error)}
            </p>
          )}
          {risk.data && <RollingBetaChart points={risk.data.beta_series} benchmark={risk.data.benchmark} />}
        </Panel>

        <Panel title="Tail risk & vol" note={risk.data ? `${risk.data.n_obs} obs` : undefined}>
          <div className="grid grid-cols-2 gap-x-4 gap-y-3">
            <div>
              <div className="text-[10px] tracking-wider uppercase text-muted">ES 97.5% (loss)</div>
              <div className="num text-lg">{risk.data ? pct(risk.data.es_975) : "—"}</div>
            </div>
            <div>
              <div className="text-[10px] tracking-wider uppercase text-muted">Ann. vol</div>
              <div className="num text-lg">{risk.data ? pct(risk.data.ann_vol) : "—"}</div>
            </div>
            <div className="col-span-2">
              <div className="text-[10px] tracking-wider uppercase text-muted">Jensen alpha (ann.)</div>
              <div className="num text-lg">{risk.data ? pct(risk.data.alpha_annualized) : "—"}</div>
              {risk.data && <div className="text-muted text-[11px] mt-1">{risk.data.alpha_note}</div>}
            </div>
          </div>
        </Panel>
      </div>

      <Panel title="Monte Carlo" note={mc.data ? `${mc.data.n_paths.toLocaleString()} paths · ${mc.data.horizon}d` : "block-bootstrap"}>
        <div className="flex items-end gap-4 mb-3 flex-wrap">
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
                setHorizon(
                  Math.min(HORIZON_BOUNDS.max, Math.max(HORIZON_BOUNDS.min, Number(e.target.value) || HORIZON_BOUNDS.min))
                )
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
                setNPaths(
                  Math.min(PATHS_BOUNDS.max, Math.max(PATHS_BOUNDS.min, Number(e.target.value) || PATHS_BOUNDS.min))
                )
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

        {!mc.data && !mc.isPending && !mc.error && (
          <p className="text-muted text-[12px]">Run to see the terminal distribution.</p>
        )}
        {mc.isPending && <Skeleton className="h-60" />}
        {mc.error && (
          <p className="text-down text-[12px]">
            Monte Carlo failed: {String(mc.error.message ?? mc.error)}
          </p>
        )}
        {mc.data && (
          <div className="grid grid-cols-[1.6fr_1fr] gap-4">
            <FanChart histogram={mc.data.histogram} p5={mc.data.p5} p50={mc.data.p50} p95={mc.data.p95} />
            <div className="grid grid-cols-2 gap-x-4 gap-y-3">
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
              <div>
                <div className="text-[10px] tracking-wider uppercase text-muted">ES 97.5% (loss)</div>
                <div className="num text-lg">{pct(mc.data.es_975)}</div>
              </div>
            </div>
          </div>
        )}
      </Panel>
    </div>
  );
}
