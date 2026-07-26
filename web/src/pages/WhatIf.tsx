// WhatIf: clone the book, modify, watch risk recompute side-by-side vs live
// (DESIGN.md IA #5).
//
// Color adjudication (batch-1 final review, stress-grid precedent ea3095a):
// the CURRENT book (the pinned base) IS the user's book and renders amber;
// hypothetical/scenario values are NOT the live book and render neutral,
// sign in the number. Benchmark comparisons render in steel/market.
//
// Wave-3B What-If flow:
// * On load, an active `?book_ref=` preloads the pinned book as the BASE
//   (chip in the builder, URL-persisted via lib/book.ts); otherwise "Load
//   current book" is the primary entry — it pins the live book and remembers
//   it as the base. Editing rows makes the book hypothetical (submits inline
//   positions) while the base ref stays pinned for the diff.
// * The result carries a current→hypothetical TRADE TICKET (per-leg qty
//   deltas) plus a CRN-paired delta block: base and hypothetical simulate on
//   the same bootstrap draws (shared seed, echoed in mc.seed), so Δ numbers
//   are noise-free.
// * Option legs (strike/expiry/right/multiplier) flow through the builder
//   into both the inline-positions and book_ref paths.
// * Pinned scenarios (lib/book.ts store, names URL-persisted like the
//   book_ref) compare side-by-side. Input scenarios save/load/delete in
//   localStorage only — server persistence is deferred (noted in the panel).
import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  BookBuilder,
  isIncompleteOptionRow,
  newBookRow,
  rowsToPositions,
  snapshotToRows,
  type BookRow,
  type BuilderPosition,
} from "../components/BookBuilder";
import { Panel, Skeleton } from "../components/Panel";
import { api, request } from "../lib/api";
import {
  getBook,
  readActiveBookRef,
  readPinnedNames,
  readPinnedScenarios,
  writeActiveBookRef,
  writePinnedNames,
  writePinnedScenarios,
  type BookSnapshotOut,
  type PinnedScenario,
} from "../lib/book";

// A saved scenario leg persists the FULL row descriptor (fix round 1, I3):
// saving only {symbol, qty} silently reloaded "SPY 2 contracts" as "SPY 2
// shares" — a materially different book. Option fields are optional so
// pre-fix saved scenarios (plain {symbol, qty}) still load — as STK legs,
// which is exactly what they were.
interface ScenarioLeg {
  symbol: string;
  qty: string;
  secType?: "STK" | "OPT";
  strike?: string;
  expiry?: string;
  right?: "C" | "P";
  multiplier?: string;
}

interface Scenario {
  positions: ScenarioLeg[];
  years: number;
  horizon: number;
  n_paths: number;
  seed?: number;
}

interface WeightOut {
  symbol: string;
  qty: number;
  // Nullable per the backend serialization policy (NaN/Inf -> null): render
  // "—" via pct()/num(), never crash on a null field.
  sec_type: "STK" | "OPT";
  strike: number | null;
  expiry: string | null;
  right: "C" | "P" | null;
  multiplier: number;
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
  // The shared CRN seed actually used + the simulated horizon (labels).
  seed: number;
  horizon_days: number;
}

interface BenchmarkOut {
  symbol: string;
  es_975: number | null;
  ann_vol: number | null;
}

interface BaseRiskOut {
  book_ref: string;
  valuation_ts: string | null;
  n_positions: number;
  beta: number | null;
  es_975: number | null;
  ann_vol: number | null;
  p5: number | null;
  p50: number | null;
  p95: number | null;
}

interface DeltaOut {
  beta: number | null;
  es_975: number | null;
  ann_vol: number | null;
  p5: number | null;
  p50: number | null;
  p95: number | null;
}

interface TicketLine {
  symbol: string;
  sec_type: "STK" | "OPT";
  strike: number | null;
  expiry: string | null;
  right: "C" | "P" | null;
  multiplier: number;
  qty_from: number;
  qty_to: number;
  qty_delta: number;
  action: "BUY" | "SELL";
  price: number | null;
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
  base: BaseRiskOut | null;
  delta: DeltaOut | null;
  trade_ticket: TicketLine[] | null;
  notes: string[];
}

const SCENARIOS_KEY = "quantmind.whatif.scenarios";
const YEARS_BOUNDS = { min: 1, max: 25 };
const HORIZON_BOUNDS = { min: 1, max: 2520 };
const PATHS_BOUNDS = { min: 1, max: 200_000 };

/** Shape check for one stored scenario (batch-2 final review item 7,
 * mirroring lib/book.ts's isPinnedScenario): localStorage is user-editable
 * junk territory — a corrupt entry must never TypeError loadScenario, it
 * just doesn't load. */
function isScenario(v: unknown): v is Scenario {
  if (typeof v !== "object" || v === null) return false;
  const s = v as Record<string, unknown>;
  return (
    Array.isArray(s.positions) &&
    s.positions.every(
      (p) =>
        typeof p === "object" &&
        p !== null &&
        typeof (p as Record<string, unknown>).symbol === "string" &&
        typeof (p as Record<string, unknown>).qty === "string"
    ) &&
    typeof s.years === "number" &&
    typeof s.horizon === "number" &&
    typeof s.n_paths === "number"
  );
}

function loadScenarios(): Record<string, Scenario> {
  try {
    const raw = localStorage.getItem(SCENARIOS_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : {};
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return {};
    const out: Record<string, Scenario> = {};
    for (const [k, v] of Object.entries(parsed)) {
      if (isScenario(v)) out[k] = v;
    }
    return out;
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
  positions?: BuilderPosition[];
  book_ref?: string;
  base_book_ref?: string;
  years: number;
  mc: { horizon: number; n_paths: number; seed?: number };
}): Promise<WhatIfResponse> {
  return request<WhatIfResponse>("/api/whatif", { method: "POST", body: JSON.stringify(body) });
}

function pct(x: number | null | undefined): string {
  if (x === null || x === undefined || !Number.isFinite(x)) return "—";
  return `${(x * 100).toFixed(2)}%`;
}

function pctSigned(x: number | null | undefined): string {
  if (x === null || x === undefined || !Number.isFinite(x)) return "—";
  return `${x >= 0 ? "+" : ""}${(x * 100).toFixed(2)}%`;
}

function num(x: number | null | undefined, digits = 4): string {
  if (x === null || x === undefined || !Number.isFinite(x)) return "—";
  return x.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function numSigned(x: number | null | undefined, digits = 4): string {
  if (x === null || x === undefined || !Number.isFinite(x)) return "—";
  return `${x >= 0 ? "+" : ""}${x.toFixed(digits)}`;
}

/** "OPT SPY 20260918 450C ×100" leg descriptor (weights + ticket rows). */
function legDescriptor(leg: { expiry: string | null; strike: number | null; right: "C" | "P" | null; multiplier: number }): string {
  return `${leg.expiry ?? "?"} ${leg.strike ?? "?"}${leg.right ?? ""} ×${leg.multiplier}`;
}

function ticketVerb(t: TicketLine): string {
  if (t.sec_type === "OPT") {
    if (t.qty_from === 0) return "OPEN";
    if (t.qty_to === 0) return "CLOSE";
  }
  return t.action;
}

function ticketLabel(t: TicketLine): string {
  const units = t.sec_type === "OPT" ? "contracts" : "shares";
  const base = `${ticketVerb(t)} ${Math.abs(t.qty_delta)} ${units} ${t.symbol}`;
  return t.sec_type === "OPT" ? `${base} ${legDescriptor(t)}` : base;
}

export function WhatIf() {
  const brief = useQuery({ queryKey: ["brief"], queryFn: api.brief, staleTime: 60 * 60 * 1000 });

  const [rows, setRows] = useState<BookRow[]>([newBookRow({ symbol: "SPY", qty: "100" })]);
  // Hypothetical-book ref (wave-3 Task A1): set when a pinned book is loaded
  // unedited, cleared as soon as the user edits any row — an edited book is
  // no longer the exact pinned snapshot, so it submits the (now-inline)
  // positions instead (see `handleRowsChange` below).
  const [bookRef, setBookRef] = useState<string | null>(null);
  // BASE (current-book) ref for the current→hypothetical diff: URL-persisted
  // (lib/book.ts), preloaded on mount, pinned by "Load current book", and
  // sticky across row edits until explicitly unpinned via the builder chip.
  const [initialBookRef] = useState<string | null>(() => readActiveBookRef());
  const [baseRef, setBaseRef] = useState<string | null>(initialBookRef);
  const [baseAsOf, setBaseAsOf] = useState<string | null>(null);
  const [years, setYears] = useState(5);
  const [horizon, setHorizon] = useState(126);
  const [nPaths, setNPaths] = useState(10_000);
  const [seed, setSeed] = useState<number | undefined>(undefined);
  const [scenarioName, setScenarioName] = useState("");
  const [scenarios, setScenarios] = useState<Record<string, Scenario>>(() => loadScenarios());
  const [pinName, setPinName] = useState("");
  // Pinned compare set (fix round 1, I2): when the URL carries ?pins=, it is
  // the source of truth for WHICH pins show and in WHAT order (a shared/
  // reloaded URL restores the same compare view); without the param, the
  // whole local store shows. The full store stays the persistence layer —
  // pin/unpin below merge into it rather than overwriting it with the
  // URL-filtered subset.
  const [pinned, setPinned] = useState<Record<string, PinnedScenario>>(() => {
    const all = readPinnedScenarios();
    const urlNames = readPinnedNames();
    if (urlNames.length === 0) return all;
    const selected: Record<string, PinnedScenario> = {};
    for (const n of urlNames) {
      if (all[n]) selected[n] = all[n];
    }
    return selected;
  });

  // Load-current-book default (wave-3B item 1): an active ?book_ref=
  // preloads the pinned book as the base + builder rows.
  const preload = useQuery({
    queryKey: ["whatif", "preload-book", initialBookRef],
    queryFn: () => getBook(initialBookRef as string),
    enabled: initialBookRef !== null,
    staleTime: Infinity,
    retry: false,
  });
  useEffect(() => {
    if (preload.data) {
      setRows(snapshotToRows(preload.data));
      setBookRef(preload.data.snapshot_id);
      setBaseAsOf(preload.data.valuation_ts);
    }
  }, [preload.data]);
  // Stale-ref policy "notice + drop" (batch-2 final review item 5): the
  // unpin below used to be invisible — state was cleared but the JSX never
  // said why the pinned book vanished. The notice renders in the builder.
  const [preloadNotice, setPreloadNotice] = useState<string | null>(null);
  useEffect(() => {
    // A stale/unknown URL ref must not wedge the page: unpin it honestly.
    if (preload.isError) {
      setBaseRef(null);
      writeActiveBookRef(null);
      setPreloadNotice(
        `pinned book ${initialBookRef} could not be loaded — unpinned; re-pin from Portfolio or Load current book`
      );
    }
  }, [preload.isError, initialBookRef]);

  const compute = useMutation({
    mutationFn: () => {
      if (rows.some(isIncompleteOptionRow)) {
        throw new Error("every option leg needs a positive strike and expiry before computing");
      }
      const common = {
        years,
        mc: { horizon, n_paths: nPaths, seed },
        ...(baseRef ? { base_book_ref: baseRef } : {}),
      };
      if (bookRef) {
        return postWhatIf({ book_ref: bookRef, ...common });
      }
      const positions = rowsToPositions(rows);
      if (positions.length === 0) {
        throw new Error("add at least one book position (symbol + nonzero qty)");
      }
      return postWhatIf({ positions, ...common });
    },
  });

  function handleRowsChange(next: BookRow[]) {
    setRows(next);
    setBookRef(null);
  }

  function handleUseCurrentBook(snapshot: BookSnapshotOut) {
    setRows(snapshotToRows(snapshot));
    setBookRef(snapshot.snapshot_id);
    setBaseRef(snapshot.snapshot_id);
    setBaseAsOf(snapshot.valuation_ts);
    writeActiveBookRef(snapshot.snapshot_id);
  }

  function handleUnpin() {
    setBaseRef(null);
    setBaseAsOf(null);
    writeActiveBookRef(null);
  }

  function saveScenario() {
    const name = scenarioName.trim();
    if (!name) return;
    const next = {
      ...scenarios,
      [name]: {
        // Persist the full leg descriptor (I3) — everything but the
        // ephemeral row key.
        positions: rows.map(({ key: _key, ...leg }) => leg),
        years,
        horizon,
        n_paths: nPaths,
        seed,
      },
    };
    setScenarios(next);
    persistScenarios(next);
    setScenarioName("");
  }

  function loadScenario(name: string) {
    const s = scenarios[name];
    if (!s) return;
    setRows(s.positions.length ? s.positions.map((p) => newBookRow(p)) : [newBookRow()]);
    setBookRef(null);
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

  function pinResult() {
    const data = compute.data;
    const name = pinName.trim();
    if (!data || !name) return;
    const entry: PinnedScenario = {
      name,
      pinned_at: new Date().toISOString(),
      as_of: data.as_of,
      horizon_days: data.mc.horizon_days,
      // From the RESPONSE, not input state (reviewer minor): the paths the
      // sim actually ran = finite histogram mass + dropped non-finite paths.
      n_paths: data.mc.histogram.counts.reduce((a, b) => a + b, 0) + data.mc.n_nonfinite,
      seed: data.mc.seed,
      beta: data.beta,
      es_975: data.es_975,
      ann_vol: data.ann_vol,
      p5: data.mc.p5,
      p50: data.mc.p50,
      p95: data.mc.p95,
    };
    const next = { ...pinned, [name]: entry };
    setPinned(next);
    // Merge into the FULL store (the displayed set may be a URL-filtered
    // subset — overwriting would delete the unselected pins).
    writePinnedScenarios({ ...readPinnedScenarios(), [name]: entry });
    writePinnedNames(Object.keys(next));
    setPinName("");
  }

  function unpinScenario(name: string) {
    setPinned((prev) => {
      const next = { ...prev };
      delete next[name];
      const all = readPinnedScenarios();
      delete all[name];
      writePinnedScenarios(all);
      writePinnedNames(Object.keys(next));
      return next;
    });
  }

  const data = compute.data;
  const errorMessage = compute.isError ? String((compute.error as Error)?.message ?? compute.error) : null;
  const hasBase = data?.base != null && data.base.n_positions > 0;
  const pinnedList = Object.values(pinned);

  return (
    <div className="space-y-3 max-w-[1400px]">
      <datalist id="whatif-symbols">
        {(brief.data?.tiles ?? []).map((t) => (
          <option key={t.symbol} value={t.symbol} />
        ))}
      </datalist>

      <Panel title="Book builder" note="clone the book, modify, watch risk recompute">
        <div className="space-y-2">
          {preloadNotice && <p className="text-warning text-[11px]">{preloadNotice}</p>}
          {!baseRef && (
            <p className="text-muted text-[11px]">
              Start from your live book: <span className="text-ink">Load current book</span> pins it as the
              base — the result then diffs current → hypothetical as a trade ticket.
            </p>
          )}
          <BookBuilder
            rows={rows}
            onRowsChange={handleRowsChange}
            onUseCurrentBook={handleUseCurrentBook}
            datalistId="whatif-symbols"
            allowOptionLegs
            pinnedBookRef={baseRef}
            pinnedAsOf={baseAsOf}
            onUnpin={handleUnpin}
          />

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
              disabled={compute.isPending}
              onClick={() => compute.mutate()}
            >
              {compute.isPending ? "Computing…" : "Compute"}
            </button>
          </div>

          {errorMessage && <p className="text-down text-[11px]">{errorMessage}</p>}
        </div>
      </Panel>

      <div className="grid grid-cols-[1fr_1.4fr] gap-3">
        <Panel title="Weights — hypothetical" note={data ? "gross-normalized · options at delta-one notional" : undefined}>
          {!data && (
            <p className="text-muted text-[11px]">Awaiting compute — build a book and run Compute to see weights.</p>
          )}
          {data && (
            <div data-testid="whatif-weights" className="space-y-2">
              {data.weights.map((w, i) => (
                <div key={i} className="space-y-1">
                  <div className="flex items-baseline justify-between text-[12px] num">
                    <span className="text-ink">
                      {w.symbol}
                      {w.sec_type === "OPT" && (
                        <span className="text-muted ml-2 text-[10px]">{legDescriptor(w)}</span>
                      )}
                    </span>
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
              {data.notes.map((n, i) => (
                <p key={i} className="text-warning text-[10px]">{n}</p>
              ))}
            </div>
          )}
        </Panel>

        <Panel
          title="Risk — current vs hypothetical vs benchmark"
          note={data?.as_of ? `as of ${data.as_of.slice(0, 10)} · ${data.n_obs} obs` : undefined}
        >
          {!data && (
            <p className="text-muted text-[11px]">Awaiting compute — book/benchmark risk lands here once you run Compute.</p>
          )}
          {data && (
            <div className="space-y-4">
              <div className={`grid ${hasBase ? "grid-cols-3" : "grid-cols-2"} gap-4`}>
                {hasBase && data.base && (
                  <div data-testid="whatif-base-risk" className="text-you space-y-2">
                    <div className="text-[10px] tracking-wider uppercase text-you/70">
                      Current book{data.base.valuation_ts ? ` · ${data.base.valuation_ts.slice(0, 10)}` : ""}
                    </div>
                    <div className="num text-[12px]">
                      <span className="text-you/70">Beta (60d)</span>
                      <span className="ml-2">{num(data.base.beta)}</span>
                    </div>
                    <div className="num text-[12px]">
                      <span className="text-you/70">ES 97.5% (daily loss)</span>
                      <span className="ml-2">{pct(data.base.es_975)}</span>
                    </div>
                    <div className="num text-[12px]">
                      <span className="text-you/70">Ann. vol (252d)</span>
                      <span className="ml-2">{pct(data.base.ann_vol)}</span>
                    </div>
                  </div>
                )}
                <div data-testid="whatif-book-risk" className="text-ink space-y-2">
                  <div className="text-[10px] tracking-wider uppercase text-muted">Hypothetical</div>
                  <div className="num text-[12px]">
                    <span className="text-muted">Beta (60d)</span>
                    <span className="ml-2">{num(data.beta)}</span>
                  </div>
                  <div className="num text-[12px]">
                    <span className="text-muted">ES 97.5% (daily loss)</span>
                    <span className="ml-2">{pct(data.es_975)}</span>
                  </div>
                  <div className="num text-[12px]">
                    <span className="text-muted">Ann. vol (252d)</span>
                    <span className="ml-2">{pct(data.ann_vol)}</span>
                  </div>
                </div>
                <div data-testid="whatif-benchmark-risk" className="text-market space-y-2">
                  <div className="text-[10px] tracking-wider uppercase text-market/70">
                    {data.benchmark.symbol} (steel)
                  </div>
                  <div className="num text-[12px]">
                    <span className="text-market/70">ES 97.5% (daily loss)</span>
                    <span className="ml-2">{pct(data.benchmark.es_975)}</span>
                  </div>
                  <div className="num text-[12px]">
                    <span className="text-market/70">Ann. vol (252d)</span>
                    <span className="ml-2">{pct(data.benchmark.ann_vol)}</span>
                  </div>
                </div>
              </div>
              {data.delta && (
                <div data-testid="whatif-delta" className="text-ink space-y-1 border-t border-hairline pt-2">
                  <div className="text-[10px] tracking-wider uppercase text-muted">
                    Δ hypothetical − current · CRN-paired · seed {data.mc.seed} · MC {data.mc.horizon_days}d
                  </div>
                  <div className="grid grid-cols-3 gap-x-4 gap-y-1 num text-[12px]">
                    <div>
                      <span className="text-muted">ΔBeta (60d)</span>
                      <span className="ml-2">{numSigned(data.delta.beta)}</span>
                    </div>
                    <div>
                      <span className="text-muted">ΔES 97.5% (daily)</span>
                      <span className="ml-2">{pctSigned(data.delta.es_975)}</span>
                    </div>
                    <div>
                      <span className="text-muted">ΔAnn. vol</span>
                      <span className="ml-2">{pctSigned(data.delta.ann_vol)}</span>
                    </div>
                    <div>
                      <span className="text-muted">Δp5 ({data.mc.horizon_days}d)</span>
                      <span className="ml-2">{pctSigned(data.delta.p5)}</span>
                    </div>
                    <div>
                      <span className="text-muted">Δp50 ({data.mc.horizon_days}d)</span>
                      <span className="ml-2">{pctSigned(data.delta.p50)}</span>
                    </div>
                    <div>
                      <span className="text-muted">Δp95 ({data.mc.horizon_days}d)</span>
                      <span className="ml-2">{pctSigned(data.delta.p95)}</span>
                    </div>
                  </div>
                </div>
              )}
            </div>
          )}
        </Panel>
      </div>

      {data?.trade_ticket != null && (
        <Panel
          title="Trade ticket — current → hypothetical"
          note={data.base?.valuation_ts ? `vs book pinned ${data.base.valuation_ts.slice(0, 10)}` : undefined}
        >
          <div data-testid="whatif-trade-ticket" className="space-y-1">
            {data.trade_ticket.length === 0 && (
              <p className="text-muted text-[11px]">No changes vs the current book.</p>
            )}
            {data.trade_ticket.map((t, i) => (
              <div key={i} className="flex items-baseline justify-between text-[12px] num border-b border-hairline/50 pb-1">
                <span className="text-ink">{ticketLabel(t)}</span>
                <span className="text-muted text-[11px]">
                  {t.qty_from} → {t.qty_to}
                  {t.price != null && ` · underlier close ${num(t.price, 2)}`}
                </span>
              </div>
            ))}
          </div>
        </Panel>
      )}

      <Panel
        title="Monte Carlo — hypothetical terminal return"
        note={data ? `${nPaths.toLocaleString()} paths · ${data.mc.horizon_days}d · seed ${data.mc.seed}` : "block-bootstrap"}
      >
        {!data && !compute.isPending && (
          <p className="text-muted text-[11px]">Awaiting compute — the terminal distribution unlocks once you run Compute.</p>
        )}
        {compute.isPending && <Skeleton className="h-24" />}
        {data && (
          <div data-testid="whatif-mc-results" className="text-ink space-y-2">
            <div className="flex items-end gap-px h-16 bg-elevated">
              {data.mc.histogram.counts.map((c, i) => {
                const max = Math.max(...data.mc.histogram.counts, 1);
                return (
                  <div key={i} className="flex-1 bg-market/60" style={{ height: `${Math.max(4, (c / max) * 100)}%` }} />
                );
              })}
            </div>
            <div className="grid grid-cols-3 gap-x-4">
              <div>
                <div className="text-[10px] tracking-wider uppercase text-muted">p5 ({data.mc.horizon_days}d)</div>
                <div className="num text-[12px]">{pct(data.mc.p5)}</div>
              </div>
              <div>
                <div className="text-[10px] tracking-wider uppercase text-muted">p50 ({data.mc.horizon_days}d)</div>
                <div className="num text-[12px]">{pct(data.mc.p50)}</div>
              </div>
              <div>
                <div className="text-[10px] tracking-wider uppercase text-muted">p95 ({data.mc.horizon_days}d)</div>
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

      <Panel title="Pinned scenarios — compare" note="pins persist locally · names in URL">
        <div className="flex items-end gap-2 mb-3">
          <label className="flex flex-col gap-1 text-[10px] tracking-wider uppercase text-muted flex-1">
            Pin name
            <input
              aria-label="Pin name"
              className="num bg-elevated border border-hairline px-2 py-1 text-ink text-[12px] w-full"
              value={pinName}
              onChange={(e) => setPinName(e.target.value)}
            />
          </label>
          <button
            type="button"
            className="border border-hairline bg-elevated hover:bg-hairline text-[12px] px-3 py-1.5 disabled:opacity-40"
            disabled={!data || !pinName.trim()}
            onClick={pinResult}
          >
            Pin result
          </button>
        </div>
        <div data-testid="whatif-pinned-compare">
          {pinnedList.length === 0 && (
            <p className="text-muted text-[11px]">No pinned scenarios yet — compute, name it, pin it.</p>
          )}
          {pinnedList.length > 0 && (
            <table className="w-full text-[12px]">
              <thead>
                <tr className="text-left text-[10px] tracking-wider uppercase text-muted">
                  <th className="py-1 pr-2 font-normal">Metric</th>
                  {pinnedList.map((p) => (
                    <th key={p.name} className="py-1 pr-2 font-normal">
                      <span className="text-ink normal-case tracking-normal">{p.name}</span>
                      <button
                        type="button"
                        aria-label={`unpin scenario ${p.name}`}
                        className="ml-1 text-muted hover:text-down"
                        onClick={() => unpinScenario(p.name)}
                      >
                        ×
                      </button>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="num">
                <tr>
                  <td className="py-0.5 pr-2 text-muted">ES 97.5% (daily)</td>
                  {pinnedList.map((p) => (
                    <td key={p.name} className="py-0.5 pr-2">{pct(p.es_975)}</td>
                  ))}
                </tr>
                <tr>
                  <td className="py-0.5 pr-2 text-muted">Ann. vol (252d)</td>
                  {pinnedList.map((p) => (
                    <td key={p.name} className="py-0.5 pr-2">{pct(p.ann_vol)}</td>
                  ))}
                </tr>
                <tr>
                  <td className="py-0.5 pr-2 text-muted">Beta (60d)</td>
                  {pinnedList.map((p) => (
                    <td key={p.name} className="py-0.5 pr-2">{num(p.beta)}</td>
                  ))}
                </tr>
                <tr>
                  <td className="py-0.5 pr-2 text-muted">MC p5</td>
                  {pinnedList.map((p) => (
                    <td key={p.name} className="py-0.5 pr-2">{pct(p.p5)}</td>
                  ))}
                </tr>
                <tr>
                  <td className="py-0.5 pr-2 text-muted">MC p50</td>
                  {pinnedList.map((p) => (
                    <td key={p.name} className="py-0.5 pr-2">{pct(p.p50)}</td>
                  ))}
                </tr>
                <tr>
                  <td className="py-0.5 pr-2 text-muted">MC p95</td>
                  {pinnedList.map((p) => (
                    <td key={p.name} className="py-0.5 pr-2">{pct(p.p95)}</td>
                  ))}
                </tr>
                <tr>
                  <td className="py-0.5 pr-2 text-muted">MC horizon</td>
                  {pinnedList.map((p) => (
                    <td key={p.name} className="py-0.5 pr-2">{p.horizon_days}d</td>
                  ))}
                </tr>
                <tr>
                  <td className="py-0.5 pr-2 text-muted">Seed</td>
                  {pinnedList.map((p) => (
                    <td key={p.name} className="py-0.5 pr-2">{p.seed ?? "—"}</td>
                  ))}
                </tr>
                <tr>
                  <td className="py-0.5 pr-2 text-muted">As of</td>
                  {pinnedList.map((p) => (
                    <td key={p.name} className="py-0.5 pr-2">{p.as_of ? p.as_of.slice(0, 10) : "—"}</td>
                  ))}
                </tr>
              </tbody>
            </table>
          )}
        </div>
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
