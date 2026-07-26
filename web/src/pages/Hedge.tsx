// Hedge Lab (DESIGN.md IA #4): "decisions, not analytics" — an objective
// picker (beta_target only for now) and a book builder feed POST /api/hedge,
// which returns candidates ranked by PROTECTION-PER-COST (wave-3B "Hedge
// honest": ΔES per unit of annual drag), each carrying an honest cost column
// (carry drag + a labeled borrow proxy), a ΔES bootstrap interval (wave-3
// Global Constraint: any bootstrap statistic shows its interval), and a
// tail-conditional protection panel (book P&L with vs without each hedge on
// the worst-decile benchmark days). Option hedge candidates (protective put /
// put spread / collar on the dominant underlier, premium as % annual drag
// from the cached chain) render in their own panel; a missing chain degrades
// to the backend's structured note, never a broken panel.
//
// Cointegration column removed (pre-wave-3 consolidation pass, TODOS.md):
// its home is Lab's pair pipeline now, never the Hedge Lab.
//
// Color law: hedge candidates are MARKET data (steel/neutral); the book's
// protection / tail P&L numbers are BOOK quantities and render amber
// (wave-2 Global Constraints addendum, Lab's Apply-to-Book precedent).
//
// Book builder (wave-3 Task A1's book-flow spine): the shared BookBuilder
// component adds "Load current book" and book_ref submission; wave-3B adds
// the pre-load leg — a ?book_ref= URL param (lib/book.ts's active-snapshot
// store) is fetched on mount and pre-fills the builder, so "open in Hedge"
// from another page lands with the pinned book already loaded.
import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { BookBuilder, newBookRow, rowsToPositions, snapshotToRows, type BookRow } from "../components/BookBuilder";
import { Panel } from "../components/Panel";
import { request } from "../lib/api";
import { getBook, readActiveBookRef, writeActiveBookRef, type BookSnapshotOut } from "../lib/book";

// Page-scoped response types (Global Constraints: api-types.ts is regenerated
// by the controller after the batch — never hand-edited here).
interface HedgeCandidate {
  symbol: string;
  beta: number | null;
  unusable: boolean;
  hedge_qty: number | null;
  hedge_notional: number | null;
  es_before: number | null;
  es_after: number | null;
  protection: number | null;
  carry_drag_annual: number | null;
  borrow_proxy_annual: number | null;
  cost_annual: number | null;
  protection_per_cost: number | null;
  delta_es_ci_low: number | null;
  delta_es_ci_high: number | null;
  tail_n_days: number | null;
  tail_mean_book: number | null;
  tail_mean_hedged: number | null;
  residual_beta: number | null;
  corr_stability: number | null;
}

interface OptionHedgeLeg {
  action: "long" | "short";
  // Nullable as the backend's NaN->null insurance (fix round 1) — a healthy
  // chain always yields finite values here.
  strike: number | null;
  right: "C" | "P";
  price: number | null;
}

interface OptionHedge {
  kind: "protective_put" | "put_spread" | "collar";
  expiry: string;
  expiry_years: number | null;
  legs: OptionHedgeLeg[];
  contracts: number | null;
  net_premium_per_contract: number | null;
  cost_annual: number | null;
  es_before: number | null;
  es_after: number | null;
  protection: number | null;
  protection_per_cost: number | null;
  delta_es_ci_low: number | null;
  delta_es_ci_high: number | null;
  tail_n_days: number | null;
  tail_mean_book: number | null;
  tail_mean_hedged: number | null;
}

interface HedgeResponse {
  benchmark: string;
  objective: { kind: string; value: number };
  book_value: number | null;
  book_beta: number | null;
  es_before: number | null;
  bench_expected_return_annual: number | null;
  n_candidates_evaluated: number;
  candidates: HedgeCandidate[];
  option_underlier: string | null;
  option_chain_as_of: string | null;
  option_hedges: OptionHedge[];
  option_note: string | null;
  es_note: string;
  cost_note: string;
  ci_note: string;
  tail_note: string;
  as_of: string | null;
  // Declared approximations (batch-2 final review item 2): e.g. the option
  // delta-one proxy when the book carries OPT legs — rendered, never silent.
  notes: string[];
}

const KIND_LABEL: Record<OptionHedge["kind"], string> = {
  protective_put: "protective put",
  put_spread: "put spread",
  collar: "collar",
};

function runHedge(body: {
  book?: { symbol: string; qty: number }[];
  book_ref?: string;
  objective: { kind: string; value: number };
  years: number;
}) {
  return request<HedgeResponse>("/api/hedge", { method: "POST", body: JSON.stringify(body) });
}

function num(x: number | null | undefined, digits = 2): string {
  if (x === null || x === undefined || !Number.isFinite(x)) return "—";
  return x.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

// es_before/es_after/protection/cost/tail means are fractions of the ORIGINAL
// book's gross (never dollar-scaled), so they render as percentages; cost is
// per YEAR, ES/tail are DAILY — the backend's note fields label both.
function pct(x: number | null | undefined): string {
  if (x === null || x === undefined || !Number.isFinite(x)) return "—";
  return `${(x * 100).toFixed(2)}%`;
}

function ci(lo: number | null, hi: number | null): string {
  if (lo === null || hi === null || !Number.isFinite(lo) || !Number.isFinite(hi)) return "—";
  return `[${pct(lo)}, ${pct(hi)}]`;
}

function fmtStrike(s: number | null): string {
  if (s === null || !Number.isFinite(s)) return "—";
  return s.toLocaleString("en-US", { maximumFractionDigits: 2 });
}

export function Hedge() {
  const [rows, setRows] = useState<BookRow[]>([newBookRow()]);
  // book_ref (wave-3 Task A1's book-flow spine): set when "Load current
  // book" resolves OR when the ?book_ref= URL param pre-loads a pinned
  // snapshot; cleared on any row edit (see WhatIf.tsx's identical pattern)
  // — an edited book submits its (now-inline) positions instead.
  const [bookRef, setBookRef] = useState<string | null>(null);
  const [pinnedAsOf, setPinnedAsOf] = useState<string | null>(null);
  const [targetBeta, setTargetBeta] = useState(0);
  const [years, setYears] = useState(5);

  // Pre-load (wave-3B): the active snapshot id from the URL, read once.
  // retry: false (batch-2 final review item 5) — a stale/unknown ref fails
  // fast into the visible notice below instead of spinning through retries.
  const [initialBookRef] = useState<string | null>(() => readActiveBookRef());
  const preload = useQuery({
    queryKey: ["hedge-book-preload", initialBookRef],
    queryFn: () => getBook(initialBookRef as string),
    enabled: initialBookRef !== null,
    retry: false,
  });
  useEffect(() => {
    if (preload.data) {
      setRows(snapshotToRows(preload.data));
      setBookRef(preload.data.snapshot_id);
      setPinnedAsOf(preload.data.valuation_ts);
    }
  }, [preload.data]);

  const run = useMutation({
    mutationFn: () => {
      if (bookRef) {
        return runHedge({ book_ref: bookRef, objective: { kind: "beta_target", value: targetBeta }, years });
      }
      const book = rowsToPositions(rows);
      if (book.length === 0) throw new Error("add at least one book position (symbol + nonzero qty)");
      return runHedge({ book, objective: { kind: "beta_target", value: targetBeta }, years });
    },
  });

  function handleRowsChange(next: BookRow[]) {
    setRows(next);
    setBookRef(null);
    setPinnedAsOf(null);
    // Batch-2 final review item 5 (edit-then-reload trap): an edited book is
    // no longer the pinned snapshot — leaving ?book_ref= in the URL meant a
    // reload silently re-loaded the stale pin over the edit.
    writeActiveBookRef(null);
  }

  function handleUseCurrentBook(snapshot: BookSnapshotOut) {
    setRows(snapshotToRows(snapshot));
    setBookRef(snapshot.snapshot_id);
    setPinnedAsOf(snapshot.valuation_ts);
    // Persist the spine: the loaded snapshot becomes the page's active
    // book_ref so a reload/share keeps the same pinned book in view.
    writeActiveBookRef(snapshot.snapshot_id);
  }

  function handleUnpin() {
    setBookRef(null);
    setPinnedAsOf(null);
    writeActiveBookRef(null);
  }

  const data = run.data;
  const tailRows: { key: string; label: string; n: number | null; without: number | null; with_: number | null }[] =
    data
      ? [
          ...data.candidates
            .filter((c) => c.tail_mean_book !== null && c.tail_mean_hedged !== null)
            .map((c) => ({
              key: `cand-${c.symbol}`,
              label: c.symbol,
              n: c.tail_n_days,
              without: c.tail_mean_book,
              with_: c.tail_mean_hedged,
            })),
          ...data.option_hedges
            .filter((o) => o.tail_mean_book !== null && o.tail_mean_hedged !== null)
            .map((o) => ({
              key: `opt-${o.kind}-${o.expiry}`,
              label: `${KIND_LABEL[o.kind]} ${o.expiry}`,
              n: o.tail_n_days,
              without: o.tail_mean_book,
              with_: o.tail_mean_hedged,
            })),
        ]
      : [];

  return (
    <div className="grid grid-cols-[360px_1fr] gap-3 max-w-[1600px]">
      {/* LEFT — Objective + Book builder */}
      <div className="space-y-3">
        <Panel title="Objective" note="beta target">
          <div className="space-y-3">
            <div>
              <label htmlFor="hedge-objective-kind" className="text-[10px] tracking-wider uppercase text-muted block mb-1">
                Kind
              </label>
              <select
                id="hedge-objective-kind"
                className="num w-full bg-elevated border border-hairline px-2 py-1.5 text-[12px]"
                value="beta_target"
                disabled
              >
                <option value="beta_target">beta_target</option>
              </select>
              <p className="text-muted text-[10px] mt-1">More objectives (floor loss, cap vega) land later.</p>
            </div>
            <div>
              <label htmlFor="hedge-target-value" className="text-[10px] tracking-wider uppercase text-muted block mb-1">
                Target beta
              </label>
              <input
                id="hedge-target-value"
                type="number"
                step={0.1}
                min={-2}
                max={2}
                className="num w-full bg-elevated border border-hairline px-2 py-1.5 text-[12px]"
                value={targetBeta}
                onChange={(e) => setTargetBeta(Number(e.target.value))}
              />
            </div>
            <div>
              <label htmlFor="hedge-years" className="text-[10px] tracking-wider uppercase text-muted block mb-1">
                Years of history
              </label>
              <input
                id="hedge-years"
                type="number"
                min={1}
                max={25}
                className="num w-full bg-elevated border border-hairline px-2 py-1.5 text-[12px]"
                value={years}
                onChange={(e) => setYears(Number(e.target.value))}
              />
            </div>
          </div>
        </Panel>

        <Panel title="Book" note={`${rows.length} row${rows.length === 1 ? "" : "s"}`}>
          {preload.isError && (
            <p className="text-down text-[11px] mb-2">
              could not pre-load pinned book {initialBookRef}: {String((preload.error as Error)?.message ?? preload.error)}
            </p>
          )}
          <BookBuilder
            rows={rows}
            onRowsChange={handleRowsChange}
            onUseCurrentBook={handleUseCurrentBook}
            label="Positions"
            pinnedBookRef={bookRef}
            pinnedAsOf={pinnedAsOf}
            onUnpin={handleUnpin}
          />
        </Panel>

        <button
          type="button"
          className="w-full border border-you/60 bg-you/10 hover:bg-you/20 text-you text-[12px] py-1.5 disabled:opacity-40 disabled:text-muted disabled:border-hairline disabled:bg-transparent"
          disabled={run.isPending}
          onClick={() => run.mutate()}
        >
          {run.isPending ? "Running…" : "Run"}
        </button>
        {run.isError && (
          <p className="text-down text-[11px]">{String(run.error?.message ?? run.error)}</p>
        )}
      </div>

      {/* RIGHT — Ranked candidates + option hedges + tail-conditional protection */}
      <div className="space-y-3 min-w-0">
        <Panel
          title="Ranked candidates"
          note={
            data
              ? `${data.n_candidates_evaluated} evaluated · book β ${num(data.book_beta, 2)} → target ${num(
                  data.objective.value,
                  2
                )} · ranked by ΔES per unit annual drag · as of ${data.as_of ?? "—"}`
              : undefined
          }
        >
          {!data && !run.isPending && (
            <p className="text-muted text-[12px]">
              Awaiting run — build a book, set a target beta, and press Run to rank hedge candidates by
              protection per unit of annual cost.
            </p>
          )}
          {run.isPending && <p className="text-muted text-[12px]">Ranking candidates…</p>}
          {data && data.candidates.length === 0 && (
            <p className="text-muted text-[12px]">No candidates evaluated.</p>
          )}
          {data && data.candidates.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-[12px]" role="table" data-testid="candidates-table">
                <thead>
                  <tr className="text-left text-[10px] tracking-wider uppercase text-muted border-b border-hairline">
                    <th className="py-1.5 pr-2">#</th>
                    <th className="py-1.5 pr-2">Instrument</th>
                    <th className="py-1.5 pr-2 text-right">Size (qty / notional)</th>
                    <th className="py-1.5 pr-2 text-right">Cost (%/yr)</th>
                    <th className="py-1.5 pr-2 text-right">Protection (ES before → after, daily)</th>
                    <th className="py-1.5 pr-2 text-right">ΔES 95% CI</th>
                    <th className="py-1.5 pr-2 text-right">Prot. / cost (rank)</th>
                    <th className="py-1.5 pr-2 text-right">Residual β</th>
                    <th className="py-1.5 pr-2 text-right">Corr stability (diagnostic)</th>
                  </tr>
                </thead>
                <tbody>
                  {data.candidates.map((c, i) => (
                    <tr key={c.symbol} className="border-b border-hairline hover:bg-elevated">
                      <td className="num py-1.5 pr-2 text-muted">{i + 1}</td>
                      <td className="py-1.5 pr-2">
                        {c.symbol}
                        {c.unusable && (
                          <span className="ml-1.5 text-warning text-[10px]">unusable (|β| &lt; 0.1)</span>
                        )}
                      </td>
                      <td className="num py-1.5 pr-2 text-right">
                        {c.unusable ? (
                          "—"
                        ) : (
                          <>
                            {num(c.hedge_qty, 1)}
                            <span className="text-muted"> / {num(c.hedge_notional, 0)}</span>
                          </>
                        )}
                      </td>
                      <td
                        data-testid="cost-cell"
                        className="num py-1.5 pr-2 text-right"
                        title={
                          c.cost_annual !== null
                            ? `carry ${pct(c.carry_drag_annual)} + borrow proxy ${pct(c.borrow_proxy_annual)}`
                            : undefined
                        }
                      >
                        {pct(c.cost_annual)}
                      </td>
                      <td
                        data-testid="protection-cell"
                        className={`num py-1.5 pr-2 text-right ${c.protection !== null ? "text-you" : "text-muted"}`}
                      >
                        {c.unusable ? (
                          "—"
                        ) : (
                          <>
                            {pct(c.es_before)} → {pct(c.es_after)}
                            <span className="ml-1">({pct(c.protection)})</span>
                          </>
                        )}
                      </td>
                      <td data-testid="ci-cell" className="num py-1.5 pr-2 text-right text-muted">
                        {ci(c.delta_es_ci_low, c.delta_es_ci_high)}
                      </td>
                      <td data-testid="ppc-cell" className="num py-1.5 pr-2 text-right">
                        {num(c.protection_per_cost, 2)}
                      </td>
                      <td className="num py-1.5 pr-2 text-right">{num(c.residual_beta, 2)}</td>
                      <td className="num py-1.5 pr-2 text-right text-muted">{num(c.corr_stability, 3)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="mt-2 space-y-0.5">
                <p className="text-muted text-[10px]">{data.es_note}</p>
                <p className="text-muted text-[10px]">{data.cost_note}</p>
                <p className="text-muted text-[10px]">{data.ci_note}</p>
                {data.notes.map((n, i) => (
                  <p key={i} className="text-warning text-[10px]">{n}</p>
                ))}
              </div>
            </div>
          )}
        </Panel>

        {data && (
          <Panel
            title="Option hedges"
            note={`${data.option_underlier ?? "—"}${
              data.option_chain_as_of ? ` · chain as of ${data.option_chain_as_of}` : ""
            }`}
          >
            {data.option_hedges.length === 0 ? (
              <p className="text-muted text-[12px]">{data.option_note ?? "No option structures available."}</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-[12px]" role="table" data-testid="option-hedges-table">
                  <thead>
                    <tr className="text-left text-[10px] tracking-wider uppercase text-muted border-b border-hairline">
                      <th className="py-1.5 pr-2">#</th>
                      <th className="py-1.5 pr-2">Structure</th>
                      <th className="py-1.5 pr-2">Legs</th>
                      <th className="py-1.5 pr-2 text-right">Expiry</th>
                      <th className="py-1.5 pr-2 text-right">Contracts</th>
                      <th className="py-1.5 pr-2 text-right">Premium ($/ct)</th>
                      <th className="py-1.5 pr-2 text-right">Cost (%/yr)</th>
                      <th className="py-1.5 pr-2 text-right">Protection (ES before → after, daily)</th>
                      <th className="py-1.5 pr-2 text-right">ΔES 95% CI</th>
                      <th className="py-1.5 pr-2 text-right">Prot. / cost (rank)</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.option_hedges.map((o, i) => (
                      <tr key={`${o.kind}-${o.expiry}`} className="border-b border-hairline hover:bg-elevated">
                        <td className="num py-1.5 pr-2 text-muted">{i + 1}</td>
                        <td className="py-1.5 pr-2">{KIND_LABEL[o.kind]}</td>
                        <td className="num py-1.5 pr-2">
                          {o.legs.map((leg, j) => (
                            <span key={`${leg.action}-${leg.strike}-${leg.right}`}>
                              {j > 0 && <span className="text-muted"> / </span>}
                              <span>{`${leg.action} ${fmtStrike(leg.strike)}${leg.right}`}</span>
                            </span>
                          ))}
                        </td>
                        <td className="num py-1.5 pr-2 text-right text-muted">{o.expiry}</td>
                        <td className="num py-1.5 pr-2 text-right">{num(o.contracts, 1)}</td>
                        <td className="num py-1.5 pr-2 text-right">{num(o.net_premium_per_contract, 0)}</td>
                        <td data-testid="option-cost-cell" className="num py-1.5 pr-2 text-right">
                          {pct(o.cost_annual)}
                        </td>
                        <td
                          className={`num py-1.5 pr-2 text-right ${o.protection !== null ? "text-you" : "text-muted"}`}
                        >
                          {pct(o.es_before)} → {pct(o.es_after)}
                          <span className="ml-1">({pct(o.protection)})</span>
                        </td>
                        <td className="num py-1.5 pr-2 text-right text-muted">
                          {ci(o.delta_es_ci_low, o.delta_es_ci_high)}
                        </td>
                        <td className="num py-1.5 pr-2 text-right">{num(o.protection_per_cost, 2)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {data.option_note && <p className="text-muted text-[10px] mt-2">{data.option_note}</p>}
              </div>
            )}
          </Panel>
        )}

        {data && (
          <Panel
            title="Tail-conditional protection"
            note={`worst-decile ${data.benchmark} days · daily means · as of ${data.as_of ?? "—"}`}
          >
            {tailRows.length === 0 ? (
              <p className="text-muted text-[12px]">
                No tail statistics available — the window is too short for a non-empty worst decile.
              </p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-[12px]" role="table" data-testid="tail-table">
                  <thead>
                    <tr className="text-left text-[10px] tracking-wider uppercase text-muted border-b border-hairline">
                      <th className="py-1.5 pr-2">Hedge</th>
                      <th className="py-1.5 pr-2 text-right">Days (n)</th>
                      <th className="py-1.5 pr-2 text-right">Book without hedge</th>
                      <th className="py-1.5 pr-2 text-right">Book with hedge</th>
                    </tr>
                  </thead>
                  <tbody>
                    {tailRows.map((row) => (
                      <tr key={row.key} className="border-b border-hairline hover:bg-elevated">
                        <td className="py-1.5 pr-2">{row.label}</td>
                        <td className="num py-1.5 pr-2 text-right text-muted">{row.n ?? "—"}</td>
                        <td data-testid="tail-without-cell" className="num py-1.5 pr-2 text-right">
                          {pct(row.without)}
                        </td>
                        <td data-testid="tail-with-cell" className="num py-1.5 pr-2 text-right text-you">
                          {pct(row.with_)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <p className="text-muted text-[10px] mt-2">{data.tail_note}</p>
              </div>
            )}
          </Panel>
        )}
      </div>
    </div>
  );
}
