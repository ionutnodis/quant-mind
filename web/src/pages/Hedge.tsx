// Hedge Lab (DESIGN.md IA #4): "decisions, not analytics" — an objective
// picker (beta_target only for now) and a book builder feed POST /api/hedge,
// which returns candidates ranked by protection (ES reduction), sized to
// move the book's beta to target.
//
// Cointegration column removed (pre-wave-3 consolidation pass, TODOS.md):
// its home is Lab's pair pipeline now, never the Hedge Lab response/page.
//
// Hypothetical books ARE the user's book for color purposes (wave-2 Global
// Constraints addendum, Lab's Apply-to-Book precedent): protection and the
// book-level stats render in amber, exactly like Lab's Apply-to-Book zone.
//
// Book builder (wave-3 Task A1's book-flow spine): the shared BookBuilder
// component (this page's own row builder was its base) adds "Load current
// book" (explicit POST /api/book/pin) and book_ref submission — see WhatIf.tsx's
// identical pattern.
import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { BookBuilder, newBookRow, rowsToPositions, snapshotToRows, type BookRow } from "../components/BookBuilder";
import { Panel } from "../components/Panel";
import { request } from "../lib/api";
import type { components } from "../lib/api-types";
import { getBook, readActiveBookRef, type BookSnapshotOut } from "../lib/book";

type HedgeRequest = components["schemas"]["HedgeRequest"];
type HedgeResponse = components["schemas"]["HedgeResponse"];
type LeverageRequest = components["schemas"]["LeverageRequest"];
type LeverageResponse = components["schemas"]["LeverageResponse"];
type FxEvidence = components["schemas"]["FxEvidenceOut"];

function runHedge(body: HedgeRequest) {
  return request<HedgeResponse>("/api/hedge", { method: "POST", body: JSON.stringify(body) });
}

function runLeverage(body: LeverageRequest) {
  return request<LeverageResponse>("/api/leverage", { method: "POST", body: JSON.stringify(body) });
}

function FxEvidenceLine({ fx, testId }: { fx: FxEvidence; testId: string }) {
  return (
    <p data-testid={testId} className="num text-muted text-[10px]">
      FX base {fx.base_currency} · source {fx.source ?? "identity"} · as of {fx.as_of ?? "not required"}
    </p>
  );
}

function num(x: number | null | undefined, digits = 2): string {
  if (x === null || x === undefined || !Number.isFinite(x)) return "—";
  return x.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

// es_before/es_after/protection are fractions of gross (historical_es on
// daily returns, ~0.001-0.05) — never dollar-scaled — so they render as
// percentages, matching WhatIf's pct() pattern (WhatIf.tsx).
function pct(x: number | null | undefined): string {
  if (x === null || x === undefined || !Number.isFinite(x)) return "—";
  return `${(x * 100).toFixed(2)}%`;
}

export function Hedge() {
  const [requestedBookRef] = useState(() => readActiveBookRef()?.trim() || null);
  const [rows, setRows] = useState<BookRow[]>(() => requestedBookRef ? [] : [newBookRow()]);
  // book_ref (wave-3 Task A1's book-flow spine): set when "Load current
  // book" resolves, cleared on any row edit (see WhatIf.tsx's identical
  // pattern) — an edited book submits its (now-inline) positions instead.
  const [bookRef, setBookRef] = useState<string | null>(null);
  const [linkedBookLoaded, setLinkedBookLoaded] = useState(requestedBookRef === null);
  const linkedBook = useQuery({
    queryKey: ["book", requestedBookRef],
    queryFn: () => getBook(requestedBookRef!),
    enabled: requestedBookRef !== null,
    staleTime: Infinity,
  });
  useEffect(() => {
    if (!linkedBook.data) return;
    setRows(snapshotToRows(linkedBook.data));
    setBookRef(linkedBook.data.snapshot_id);
    setLinkedBookLoaded(true);
  }, [linkedBook.data]);
  const [targetBeta, setTargetBeta] = useState(0);
  const [years, setYears] = useState(5);
  // Drawdown budget for the resilience / leverage-headroom lens (0.25 = 25%).
  const [drawdownBudget, setDrawdownBudget] = useState(0.25);

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

  const leverage = useMutation({
    mutationFn: () => {
      if (!(drawdownBudget > 0)) throw new Error("drawdown budget must be a positive fraction (e.g. 0.25)");
      if (bookRef) return runLeverage({ book_ref: bookRef, drawdown_budget: drawdownBudget, years });
      const book = rowsToPositions(rows);
      if (book.length === 0) throw new Error("add at least one book position (symbol + nonzero qty)");
      return runLeverage({ book, drawdown_budget: drawdownBudget, years });
    },
  });

  function handleRowsChange(next: BookRow[]) {
    setRows(next);
    setBookRef(null);
  }

  function handleUseCurrentBook(snapshot: BookSnapshotOut) {
    setRows(snapshotToRows(snapshot));
    setBookRef(snapshot.snapshot_id);
  }

  if (requestedBookRef && !linkedBookLoaded) {
    return (
      <Panel title="Book" note={`book ${requestedBookRef}`}>
        {linkedBook.isError ? (
          <p className="text-down text-[11px]">
            Pinned book {requestedBookRef} unavailable: {String(linkedBook.error)}
          </p>
        ) : (
          <p className="text-muted text-[11px]">Loading pinned book {requestedBookRef}…</p>
        )}
      </Panel>
    );
  }

  return (
    <div className="grid w-full grid-cols-1 gap-3 xl:grid-cols-[360px_1fr]">
      {/* LEFT — Objective + Book builder */}
      <div className="space-y-3">
        <div className="authoring-only-block space-y-3">
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
            <div>
              <label htmlFor="hedge-drawdown-budget" className="text-[10px] tracking-wider uppercase text-muted block mb-1">
                Drawdown budget (resilience)
              </label>
              <input
                id="hedge-drawdown-budget"
                type="number"
                step={0.05}
                min={0.05}
                max={1}
                className="num w-full bg-elevated border border-hairline px-2 py-1.5 text-[12px]"
                value={drawdownBudget}
                onChange={(e) => setDrawdownBudget(Number(e.target.value))}
              />
            </div>
            </div>
          </Panel>

          <Panel title="Book" note={`${rows.length} row${rows.length === 1 ? "" : "s"}`}>
            <BookBuilder
              rows={rows}
              onRowsChange={handleRowsChange}
              onUseCurrentBook={handleUseCurrentBook}
              label="Positions"
            />
          </Panel>

          <button
            type="button"
            className="w-full border border-hairline bg-elevated py-1.5 text-[12px] text-ink hover:border-market hover:bg-hairline disabled:border-hairline disabled:bg-transparent disabled:text-muted disabled:opacity-40"
            disabled={run.isPending}
            onClick={() => {
              run.mutate();
              leverage.mutate();
            }}
          >
            {run.isPending ? "Running…" : "Run"}
          </button>
          {run.isError && (
            <p className="text-down text-[11px]">{String(run.error?.message ?? run.error)}</p>
          )}
          {leverage.isError && (
            <p className="text-down text-[11px]">Resilience: {String(leverage.error?.message ?? leverage.error)}</p>
          )}
        </div>

        {leverage.data && (
          <Panel title="Resilience" note={`drawdown budget ${pct(leverage.data.drawdown_budget)}`}>
            <FxEvidenceLine fx={leverage.data.fx} testId="leverage-fx-evidence" />
            <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-3">
              <div>
                <div className="text-[10px] tracking-wider uppercase text-muted">Max drawdown (hist.)</div>
                <div className="num text-lg">{pct(leverage.data.max_drawdown)}</div>
              </div>
              <div>
                <div className="text-[10px] tracking-wider uppercase text-muted">Leverage headroom</div>
                <div className="num text-lg">{num(leverage.data.leverage_headroom, 2)}×</div>
              </div>
              <div className="col-span-2">
                <div className="text-[10px] tracking-wider uppercase text-muted">Diversification ratio</div>
                <div className="num text-lg">{num(leverage.data.diversification_ratio, 2)}</div>
                <div className="num text-muted text-[10px]">
                  1.00 = collinear book; higher = more orthogonal legs
                </div>
              </div>
              <div className="col-span-2 text-muted text-[10px]">{leverage.data.note}</div>
            </div>
          </Panel>
        )}
      </div>

      {/* RIGHT — Ranked candidates */}
      <Panel
        title="Ranked candidates"
        note={
          run.data
            ? `${run.data.n_candidates_evaluated} evaluated · book β ${num(run.data.book_beta, 2)} → target ${num(
                run.data.objective.value,
                2
              )} · as of ${run.data.as_of ?? "—"}`
            : undefined
        }
      >
        {!run.data && !run.isPending && (
          <p className="text-muted text-[12px]">
            Awaiting run — build a book, set a target beta, and press Run to rank hedge candidates by
            protection.
          </p>
        )}
        {run.isPending && <p className="text-muted text-[12px]">Ranking candidates…</p>}
        {run.data && (
          <div className="mb-3 space-y-1">
            <FxEvidenceLine fx={run.data.fx} testId="hedge-fx-evidence" />
            <p data-testid="hedge-comparison-evidence" className="num text-muted text-[10px]">
              Common comparison · as of {run.data.comparison_as_of?.slice(0, 10) ?? "—"} ·{" "}
              {run.data.comparison_n_obs.toLocaleString()} obs
            </p>
          </div>
        )}
        {run.data && run.data.skipped_candidates.length > 0 && (
          <div
            role="status"
            aria-label="Skipped hedge candidates"
            className="mb-3 border border-warning/40 bg-warning/5 px-3 py-2 text-warning"
          >
            <p className="text-[10px] tracking-wider uppercase">Candidate coverage warning</p>
            <ul className="mt-1 space-y-1 text-[11px]">
              {run.data.skipped_candidates.map((candidate) => (
                <li key={candidate.symbol}>
                  <span className="num text-ink">{candidate.symbol}</span>: {candidate.reason}
                </li>
              ))}
            </ul>
          </div>
        )}
        {run.data && run.data.candidates.length === 0 && (
          <p className="text-muted text-[12px]">No candidates evaluated.</p>
        )}
        {run.data && run.data.candidates.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-[12px]" role="table">
              <thead>
                <tr className="text-left text-[10px] tracking-wider uppercase text-muted border-b border-hairline">
                  <th className="py-1.5 pr-2">#</th>
                  <th className="py-1.5 pr-2">Instrument</th>
                  <th className="py-1.5 pr-2 text-right">Size (qty / notional)</th>
                  <th className="py-1.5 pr-2 text-right">Protection (ES before → after)</th>
                  <th className="py-1.5 pr-2 text-right">Residual β</th>
                  <th className="py-1.5 pr-2 text-right">Corr stability (diagnostic)</th>
                </tr>
              </thead>
              <tbody>
                {run.data.candidates.map((c, i) => (
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
                    <td className="num py-1.5 pr-2 text-right">{num(c.residual_beta, 2)}</td>
                    <td className="num py-1.5 pr-2 text-right text-muted">{num(c.corr_stability, 3)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}
