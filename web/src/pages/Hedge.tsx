// Hedge Lab (DESIGN.md IA #4): "decisions, not analytics" — an objective
// picker (beta_target only for now) and a book builder feed POST /api/hedge,
// which returns candidates ranked by protection (ES reduction), sized to
// move the book's beta to target. Cointegration p-value is a labeled
// DIAGNOSTIC column only (Engineering Constraint 12) — it never drives the
// rank, and the UI says so.
//
// Hypothetical books ARE the user's book for color purposes (wave-2 Global
// Constraints addendum, Lab's Apply-to-Book precedent): protection and the
// book-level stats render in amber, exactly like Lab's Apply-to-Book zone.
import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Panel } from "../components/Panel";
import { request } from "../lib/api";

interface BookRow {
  symbol: string;
  qty: string; // kept as text while editing; parsed to number on submit
}

interface HedgeCandidate {
  symbol: string;
  beta: number | null;
  unusable: boolean;
  hedge_qty: number | null;
  hedge_notional: number | null;
  es_before: number | null;
  es_after: number | null;
  protection: number | null;
  residual_beta: number | null;
  corr_stability: number | null;
  coint_pvalue: number | null;
}

interface HedgeResponse {
  benchmark: string;
  objective: { kind: string; value: number };
  book_value: number | null;
  book_beta: number | null;
  es_before: number | null;
  n_candidates_evaluated: number;
  candidates: HedgeCandidate[];
  as_of: string | null;
}

function runHedge(body: {
  book: { symbol: string; qty: number }[];
  objective: { kind: string; value: number };
  years: number;
}) {
  return request<HedgeResponse>("/api/hedge", { method: "POST", body: JSON.stringify(body) });
}

function num(x: number | null | undefined, digits = 2): string {
  if (x === null || x === undefined || !Number.isFinite(x)) return "—";
  return x.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

let rowKeySeq = 0;
function newRow(): BookRow & { key: number } {
  rowKeySeq += 1;
  return { key: rowKeySeq, symbol: "", qty: "1" };
}

export function Hedge() {
  const [rows, setRows] = useState<(BookRow & { key: number })[]>([newRow()]);
  const [targetBeta, setTargetBeta] = useState(0);
  const [years, setYears] = useState(5);

  const run = useMutation({
    mutationFn: () => {
      const book = rows
        .filter((r) => r.symbol.trim() !== "")
        .map((r) => ({ symbol: r.symbol.trim(), qty: Number(r.qty) || 0 }))
        .filter((p) => p.qty !== 0);
      if (book.length === 0) throw new Error("add at least one book position (symbol + nonzero qty)");
      return runHedge({ book, objective: { kind: "beta_target", value: targetBeta }, years });
    },
  });

  function updateRow(key: number, patch: Partial<BookRow>) {
    setRows((prev) => prev.map((r) => (r.key === key ? { ...r, ...patch } : r)));
  }

  function addRow() {
    setRows((prev) => [...prev, newRow()]);
  }

  function removeRow(key: number) {
    setRows((prev) => (prev.length > 1 ? prev.filter((r) => r.key !== key) : prev));
  }

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
          <div className="space-y-2">
            {rows.map((row, i) => (
              <div key={row.key} className="flex items-end gap-2">
                <div className="flex-1">
                  <label htmlFor={`hedge-symbol-${row.key}`} className="text-[10px] tracking-wider uppercase text-muted block mb-1">
                    Symbol
                  </label>
                  <input
                    id={`hedge-symbol-${row.key}`}
                    aria-label={`symbol row ${i + 1}`}
                    className="num w-full bg-elevated border border-hairline px-2 py-1.5 text-[12px]"
                    value={row.symbol}
                    onChange={(e) => updateRow(row.key, { symbol: e.target.value.toUpperCase() })}
                  />
                </div>
                <div className="w-24">
                  <label htmlFor={`hedge-qty-${row.key}`} className="text-[10px] tracking-wider uppercase text-muted block mb-1">
                    Qty
                  </label>
                  <input
                    id={`hedge-qty-${row.key}`}
                    aria-label={`qty row ${i + 1}`}
                    type="number"
                    className="num w-full bg-elevated border border-hairline px-2 py-1.5 text-[12px]"
                    value={row.qty}
                    onChange={(e) => updateRow(row.key, { qty: e.target.value })}
                  />
                </div>
                <button
                  type="button"
                  aria-label="remove row"
                  className="border border-hairline bg-elevated hover:bg-hairline text-[12px] px-2 py-1.5 disabled:opacity-40"
                  disabled={rows.length <= 1}
                  onClick={() => removeRow(row.key)}
                >
                  ×
                </button>
              </div>
            ))}
            <button
              type="button"
              className="w-full border border-hairline bg-elevated hover:bg-hairline text-[12px] py-1.5"
              onClick={addRow}
            >
              + Add row
            </button>
          </div>
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
                  <th className="py-1.5 pr-2 text-right">Corr stability</th>
                  <th className="py-1.5 pr-2 text-right">Coint p (diagnostic)</th>
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
                          {num(c.es_before, 0)} → {num(c.es_after, 0)}
                          <span className="ml-1">({num(c.protection, 0)})</span>
                        </>
                      )}
                    </td>
                    <td className="num py-1.5 pr-2 text-right">{num(c.residual_beta, 2)}</td>
                    <td className="num py-1.5 pr-2 text-right text-muted">{num(c.corr_stability, 3)}</td>
                    <td className="num py-1.5 pr-2 text-right text-muted">{num(c.coint_pvalue, 3)}</td>
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
