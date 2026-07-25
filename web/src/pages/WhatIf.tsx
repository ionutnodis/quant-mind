// WhatIf: clone the book, modify, watch risk recompute side-by-side vs live
// (DESIGN.md IA #5). Hypothetical books ARE the user's book for color
// purposes (wave-2 Global Constraints addendum, Lab's Apply-to-Book
// precedent): the risk/MC results panels render in amber, benchmark
// comparisons render in steel/market. Scenarios save/load/delete in
// localStorage only — server persistence is deferred (noted in the panel).
import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Panel, Skeleton } from "../components/Panel";
import { api, request } from "../lib/api";

interface PositionRow {
  symbol: string;
  qty: number;
}

interface WeightOut {
  symbol: string;
  qty: number;
  // Nullable per the backend serialization policy (NaN/Inf -> null): render
  // "—" via pct()/num(), never crash on a null field.
  price: number | null;
  market_value: number | null;
  weight: number | null;
}

interface MonteCarloOut {
  histogram: { bin_edges: number[]; counts: number[] };
  p5: number | null;
  p50: number | null;
  p95: number | null;
  n_nonfinite: number;
}

interface BenchmarkOut {
  symbol: string;
  es_975: number | null;
  ann_vol: number | null;
}

interface WhatIfResponse {
  weights: WeightOut[];
  beta: number | null;
  es_975: number | null;
  ann_vol: number | null;
  mc: MonteCarloOut;
  benchmark: BenchmarkOut;
  n_obs: number;
  as_of: string | null;
}

interface Scenario {
  positions: PositionRow[];
  years: number;
  horizon: number;
  n_paths: number;
  seed?: number;
}

const SCENARIOS_KEY = "quantmind.whatif.scenarios";
const YEARS_BOUNDS = { min: 1, max: 25 };
const HORIZON_BOUNDS = { min: 1, max: 2520 };
const PATHS_BOUNDS = { min: 1, max: 200_000 };

function loadScenarios(): Record<string, Scenario> {
  try {
    const raw = localStorage.getItem(SCENARIOS_KEY);
    return raw ? (JSON.parse(raw) as Record<string, Scenario>) : {};
  } catch {
    return {};
  }
}

function persistScenarios(scenarios: Record<string, Scenario>) {
  try {
    localStorage.setItem(SCENARIOS_KEY, JSON.stringify(scenarios));
  } catch {
    // localStorage unavailable (private mode, quota) — scenarios just don't persist.
  }
}

function postWhatIf(body: {
  positions: PositionRow[];
  years: number;
  mc: { horizon: number; n_paths: number; seed?: number };
}): Promise<WhatIfResponse> {
  return request<WhatIfResponse>("/api/whatif", { method: "POST", body: JSON.stringify(body) });
}

function pct(x: number | null): string {
  if (x === null || x === undefined || !Number.isFinite(x)) return "—";
  return `${(x * 100).toFixed(2)}%`;
}

function num(x: number | null, digits = 4): string {
  if (x === null || x === undefined || !Number.isFinite(x)) return "—";
  return x.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function WhatIf() {
  const brief = useQuery({ queryKey: ["brief"], queryFn: api.brief, staleTime: 60 * 60 * 1000 });

  const [positions, setPositions] = useState<PositionRow[]>([{ symbol: "SPY", qty: 100 }]);
  const [years, setYears] = useState(5);
  const [horizon, setHorizon] = useState(126);
  const [nPaths, setNPaths] = useState(10_000);
  const [seed, setSeed] = useState<number | undefined>(undefined);
  const [scenarioName, setScenarioName] = useState("");
  const [scenarios, setScenarios] = useState<Record<string, Scenario>>(() => loadScenarios());

  const compute = useMutation({
    mutationFn: () =>
      postWhatIf({
        positions: positions.map((p) => ({ symbol: p.symbol.trim().toUpperCase(), qty: Number(p.qty) })),
        years,
        mc: { horizon, n_paths: nPaths, seed },
      }),
  });

  function updateRow(i: number, patch: Partial<PositionRow>) {
    setPositions((rows) => rows.map((r, idx) => (idx === i ? { ...r, ...patch } : r)));
  }

  function addRow() {
    setPositions((rows) => [...rows, { symbol: "", qty: 1 }]);
  }

  function removeRow(i: number) {
    setPositions((rows) => rows.filter((_, idx) => idx !== i));
  }

  function saveScenario() {
    const name = scenarioName.trim();
    if (!name) return;
    const next = { ...scenarios, [name]: { positions, years, horizon, n_paths: nPaths, seed } };
    setScenarios(next);
    persistScenarios(next);
    setScenarioName("");
  }

  function loadScenario(name: string) {
    const s = scenarios[name];
    if (!s) return;
    setPositions(s.positions);
    setYears(s.years);
    setHorizon(s.horizon);
    setNPaths(s.n_paths);
    setSeed(s.seed);
  }

  function deleteScenario(name: string) {
    setScenarios((prev) => {
      const next = { ...prev };
      delete next[name];
      persistScenarios(next);
      return next;
    });
  }

  const hasValidBook = positions.length > 0 && positions.every((p) => p.symbol.trim() !== "" && Number(p.qty) !== 0);
  const data = compute.data;
  const errorMessage = compute.isError ? String((compute.error as Error)?.message ?? compute.error) : null;

  return (
    <div className="space-y-3 max-w-[1400px]">
      <datalist id="whatif-symbols">
        {(brief.data?.tiles ?? []).map((t) => (
          <option key={t.symbol} value={t.symbol} />
        ))}
      </datalist>

      <Panel title="Book builder" note="clone the book, modify, watch risk recompute">
        <div className="space-y-2">
          {positions.map((p, i) => (
            <div key={i} className="flex items-end gap-2">
              <label className="flex flex-col gap-1 text-[10px] tracking-wider uppercase text-muted">
                {`Symbol ${i + 1}`}
                <input
                  aria-label={`Symbol ${i + 1}`}
                  list="whatif-symbols"
                  className="num bg-elevated border border-hairline px-2 py-1 text-ink text-[12px] w-28"
                  value={p.symbol}
                  onChange={(e) => updateRow(i, { symbol: e.target.value.toUpperCase() })}
                />
              </label>
              <label className="flex flex-col gap-1 text-[10px] tracking-wider uppercase text-muted">
                {`Qty ${i + 1}`}
                <input
                  aria-label={`Qty ${i + 1}`}
                  type="number"
                  className="num bg-elevated border border-hairline px-2 py-1 text-ink text-[12px] w-24"
                  value={p.qty}
                  onChange={(e) => updateRow(i, { qty: Number(e.target.value) })}
                />
              </label>
              <button
                type="button"
                aria-label={`Remove position ${i + 1}`}
                className="border border-hairline bg-elevated hover:bg-hairline text-[12px] px-2 py-1 disabled:opacity-40"
                disabled={positions.length <= 1}
                onClick={() => removeRow(i)}
              >
                Remove
              </button>
            </div>
          ))}
          <button
            type="button"
            className="border border-hairline bg-elevated hover:bg-hairline text-[12px] px-3 py-1.5"
            onClick={addRow}
          >
            Add position
          </button>

          <div className="flex items-end gap-4 flex-wrap pt-2 border-t border-hairline">
            <label className="flex flex-col gap-1 text-[10px] tracking-wider uppercase text-muted">
              Years
              <input
                aria-label="Years"
                type="number"
                min={YEARS_BOUNDS.min}
                max={YEARS_BOUNDS.max}
                className="num bg-elevated border border-hairline px-2 py-1 text-ink text-[12px] w-20"
                value={years}
                onChange={(e) => setYears(Number(e.target.value))}
              />
            </label>
            <label className="flex flex-col gap-1 text-[10px] tracking-wider uppercase text-muted">
              Horizon (days)
              <input
                aria-label="Horizon (days)"
                type="number"
                min={HORIZON_BOUNDS.min}
                max={HORIZON_BOUNDS.max}
                className="num bg-elevated border border-hairline px-2 py-1 text-ink text-[12px] w-24"
                value={horizon}
                onChange={(e) => setHorizon(Number(e.target.value))}
              />
            </label>
            <label className="flex flex-col gap-1 text-[10px] tracking-wider uppercase text-muted">
              Paths
              <input
                aria-label="Paths"
                type="number"
                min={PATHS_BOUNDS.min}
                max={PATHS_BOUNDS.max}
                className="num bg-elevated border border-hairline px-2 py-1 text-ink text-[12px] w-28"
                value={nPaths}
                onChange={(e) => setNPaths(Number(e.target.value))}
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
              className="border border-you/60 bg-you/10 hover:bg-you/20 text-you text-[12px] px-3 py-1.5 disabled:opacity-40 disabled:text-muted disabled:border-hairline disabled:bg-transparent"
              disabled={!hasValidBook || compute.isPending}
              onClick={() => compute.mutate()}
            >
              {compute.isPending ? "Computing…" : "Compute"}
            </button>
          </div>

          {errorMessage && <p className="text-down text-[11px]">{errorMessage}</p>}
        </div>
      </Panel>

      <div className="grid grid-cols-[1fr_1.4fr] gap-3">
        <Panel title="Weights" note={data ? "gross-normalized" : undefined}>
          {!data && (
            <p className="text-muted text-[11px]">Awaiting compute — build a book and run Compute to see weights.</p>
          )}
          {data && (
            <div data-testid="whatif-weights" className="space-y-2">
              {data.weights.map((w) => (
                <div key={w.symbol} className="space-y-1">
                  <div className="flex items-baseline justify-between text-[12px] num">
                    <span className="text-ink">{w.symbol}</span>
                    <span className="text-muted">{pct(w.weight)}</span>
                  </div>
                  <div className="h-1.5 bg-elevated">
                    <div
                      className={`h-1.5 ${(w.market_value ?? 0) >= 0 ? "bg-up" : "bg-down"}`}
                      style={{ width: `${Math.min(100, Math.abs(w.weight ?? 0) * 100)}%` }}
                    />
                  </div>
                </div>
              ))}
            </div>
          )}
        </Panel>

        <Panel title="Risk — book vs benchmark" note={data?.as_of ? `as of ${data.as_of.slice(0, 10)} · ${data.n_obs} obs` : undefined}>
          {!data && (
            <p className="text-muted text-[11px]">Awaiting compute — book/benchmark risk lands here once you run Compute.</p>
          )}
          {data && (
            <div className="grid grid-cols-2 gap-4">
              <div data-testid="whatif-book-risk" className="text-you space-y-2">
                <div className="text-[10px] tracking-wider uppercase text-you/70">This book (amber)</div>
                <div className="num text-[12px]">
                  <span className="text-you/70">Beta (60d)</span>
                  <span className="ml-2">{num(data.beta)}</span>
                </div>
                <div className="num text-[12px]">
                  <span className="text-you/70">ES 97.5% (loss)</span>
                  <span className="ml-2">{pct(data.es_975)}</span>
                </div>
                <div className="num text-[12px]">
                  <span className="text-you/70">Ann. vol</span>
                  <span className="ml-2">{pct(data.ann_vol)}</span>
                </div>
              </div>
              <div data-testid="whatif-benchmark-risk" className="text-market space-y-2">
                <div className="text-[10px] tracking-wider uppercase text-market/70">
                  {data.benchmark.symbol} (steel)
                </div>
                <div className="num text-[12px]">
                  <span className="text-market/70">ES 97.5% (loss)</span>
                  <span className="ml-2">{pct(data.benchmark.es_975)}</span>
                </div>
                <div className="num text-[12px]">
                  <span className="text-market/70">Ann. vol</span>
                  <span className="ml-2">{pct(data.benchmark.ann_vol)}</span>
                </div>
              </div>
            </div>
          )}
        </Panel>
      </div>

      <Panel title="Monte Carlo — terminal return" note={data ? `${nPaths.toLocaleString()} paths · ${horizon}d` : "block-bootstrap"}>
        {!data && !compute.isPending && (
          <p className="text-muted text-[11px]">Awaiting compute — the terminal distribution unlocks once you run Compute.</p>
        )}
        {compute.isPending && <Skeleton className="h-24" />}
        {data && (
          <div data-testid="whatif-mc-results" className="text-you space-y-2">
            <div className="flex items-end gap-px h-16 bg-you/5">
              {data.mc.histogram.counts.map((c, i) => {
                const max = Math.max(...data.mc.histogram.counts, 1);
                return (
                  <div key={i} className="flex-1 bg-you/60" style={{ height: `${Math.max(4, (c / max) * 100)}%` }} />
                );
              })}
            </div>
            <div className="grid grid-cols-3 gap-x-4">
              <div>
                <div className="text-[10px] tracking-wider uppercase text-you/70">p5</div>
                <div className="num text-[12px]">{pct(data.mc.p5)}</div>
              </div>
              <div>
                <div className="text-[10px] tracking-wider uppercase text-you/70">p50</div>
                <div className="num text-[12px]">{pct(data.mc.p50)}</div>
              </div>
              <div>
                <div className="text-[10px] tracking-wider uppercase text-you/70">p95</div>
                <div className="num text-[12px]">{pct(data.mc.p95)}</div>
              </div>
            </div>
            {data.mc.n_nonfinite > 0 && (
              <p className="text-warning text-[10px]">
                {data.mc.n_nonfinite.toLocaleString()} path{data.mc.n_nonfinite === 1 ? "" : "s"} produced
                non-finite terminal returns and {data.mc.n_nonfinite === 1 ? "was" : "were"} excluded — check
                cached bars for zero/degenerate prices.
              </p>
            )}
          </div>
        )}
      </Panel>

      <Panel title="Scenarios" note="localStorage · server persistence deferred">
        <div className="flex items-end gap-2 mb-3">
          <label className="flex flex-col gap-1 text-[10px] tracking-wider uppercase text-muted flex-1">
            Scenario name
            <input
              aria-label="Scenario name"
              className="num bg-elevated border border-hairline px-2 py-1 text-ink text-[12px] w-full"
              value={scenarioName}
              onChange={(e) => setScenarioName(e.target.value)}
            />
          </label>
          <button
            type="button"
            className="border border-hairline bg-elevated hover:bg-hairline text-[12px] px-3 py-1.5 disabled:opacity-40"
            disabled={!scenarioName.trim()}
            onClick={saveScenario}
          >
            Save scenario
          </button>
        </div>
        {Object.keys(scenarios).length === 0 && (
          <p className="text-muted text-[11px]">No saved scenarios yet.</p>
        )}
        <div className="flex flex-wrap gap-2">
          {Object.keys(scenarios).map((name) => (
            <div key={name} className="flex items-center gap-1 border border-hairline px-2 py-1">
              <button
                type="button"
                className="text-[12px] text-ink hover:text-you"
                onClick={() => loadScenario(name)}
              >
                {name}
              </button>
              <button
                type="button"
                aria-label={`Delete scenario ${name}`}
                className="text-[11px] text-muted hover:text-down"
                onClick={() => deleteScenario(name)}
              >
                ×
              </button>
            </div>
          ))}
        </div>
      </Panel>
    </div>
  );
}
