// BookBuilder: the shared position-row builder (wave-3 Task A1's book-flow
// spine) — Hedge's string-qty row variant as base (kept as text while
// editing so a half-typed qty like "-" or "" doesn't get coerced to 0
// mid-keystroke). WhatIf and Hedge both swap their bespoke row builders for
// this component. A "Load current book" button pulls GET /api/book/current
// (lib/book.ts) and hands the resulting snapshot to the parent via
// `onUseCurrentBook` — the parent decides whether to populate rows for
// display, remember the book_ref, or both (WhatIf/Hedge do both: show the
// live positions AND submit by book_ref rather than re-deriving them).
//
// Wave-3B additions are STRICTLY additive (Hedge consumes this component
// concurrently with its pre-3B props — defaults preserve that behavior
// exactly):
// * `allowOptionLegs` (default false): per-row STK/OPT toggle with
//   strike / expiry / right / multiplier (default 100) inputs, matching the
//   optional option-leg fields _shared.PositionIn has carried since 3A.
// * `pinnedBookRef`/`pinnedAsOf`/`onUnpin` (default hidden): a small amber
//   chip showing the ACTIVE pinned book_ref + its as-of stamp with an unpin
//   affordance. Amber is lawful here: the pinned current book IS the user's
//   book (DESIGN.md core law).
import { useMutation } from "@tanstack/react-query";
import { getCurrentBook, type BookSnapshotOut } from "../lib/book";

export interface BookRow {
  key: number;
  symbol: string;
  qty: string; // kept as text while editing; parsed to number on submit
  // Option-leg fields (wave-3B) — optional so pre-3B row literals/tests
  // stay valid; undefined secType means a plain STK row.
  secType?: "STK" | "OPT";
  strike?: string;
  expiry?: string; // YYYY-MM-DD or YYYYMMDD (backend normalizes)
  right?: "C" | "P";
  multiplier?: string;
}

/** The position shape whatif/hedge accept: bare {symbol, qty} for an equity
 * leg; + strike/expiry/right/multiplier for an option leg. */
export interface BuilderPosition {
  symbol: string;
  qty: number;
  strike?: number;
  expiry?: string;
  right?: "C" | "P";
  multiplier?: number;
}

let rowKeySeq = 0;

export function newBookRow(overrides: Partial<Omit<BookRow, "key">> = {}): BookRow {
  rowKeySeq += 1;
  return {
    key: rowKeySeq,
    symbol: "",
    qty: "1",
    secType: "STK",
    strike: "",
    expiry: "",
    right: "C",
    multiplier: "100",
    ...overrides,
  };
}

/** True when an OPT row has a symbol but is missing a positive strike or an
 * expiry — such a leg cannot be priced and must not be silently dropped or
 * submitted; callers surface an honest error instead. */
export function isIncompleteOptionRow(row: BookRow): boolean {
  return (
    row.secType === "OPT" &&
    row.symbol.trim() !== "" &&
    (!(Number(row.strike) > 0) || (row.expiry ?? "").trim() === "")
  );
}

/** Rows -> the positions whatif/hedge accept: blank symbols and
 * zero/unparseable quantities are dropped rather than submitted. STK rows
 * emit exactly {symbol, qty} (pre-3B wire shape, unchanged); OPT rows carry
 * their full leg descriptor. Incomplete OPT rows are dropped here too —
 * gate on isIncompleteOptionRow() first to refuse them honestly. */
export function rowsToPositions(rows: BookRow[]): BuilderPosition[] {
  return rows
    .filter((r) => r.symbol.trim() !== "" && !isIncompleteOptionRow(r))
    .map((r): BuilderPosition => {
      const base = { symbol: r.symbol.trim().toUpperCase(), qty: Number(r.qty) || 0 };
      if (r.secType !== "OPT") return base;
      return {
        ...base,
        strike: Number(r.strike),
        expiry: (r.expiry ?? "").trim(),
        right: r.right ?? "C",
        multiplier: Number(r.multiplier) > 0 ? Number(r.multiplier) : 100,
      };
    })
    .filter((p) => p.qty !== 0);
}

/** A pinned snapshot's positions rendered back as editable rows (for the
 * "Load current book" flow) — an empty book still yields one blank row so
 * the builder never renders with zero rows. Option legs round-trip their
 * strike/expiry/right/multiplier. */
export function snapshotToRows(snapshot: BookSnapshotOut): BookRow[] {
  if (snapshot.positions.length === 0) return [newBookRow()];
  return snapshot.positions.map((p) =>
    p.sec_type === "OPT"
      ? newBookRow({
          symbol: p.symbol,
          qty: String(p.qty),
          secType: "OPT",
          strike: p.strike != null ? String(p.strike) : "",
          expiry: p.expiry ?? "",
          right: p.right ?? "C",
          multiplier: String(p.multiplier || 100),
        })
      : newBookRow({ symbol: p.symbol, qty: String(p.qty) })
  );
}

interface BookBuilderProps {
  rows: BookRow[];
  onRowsChange: (rows: BookRow[]) => void;
  onUseCurrentBook: (snapshot: BookSnapshotOut) => void;
  datalistId?: string;
  label?: string;
  /** Enables the per-row STK/OPT toggle + option-leg inputs (default off —
   * Hedge's builder is unchanged). */
  allowOptionLegs?: boolean;
  /** The ACTIVE pinned book_ref (amber chip with as-of + unpin) — hidden
   * when null/undefined. */
  pinnedBookRef?: string | null;
  pinnedAsOf?: string | null;
  onUnpin?: () => void;
}

export function BookBuilder({
  rows,
  onRowsChange,
  onUseCurrentBook,
  datalistId,
  label = "Book",
  allowOptionLegs = false,
  pinnedBookRef = null,
  pinnedAsOf = null,
  onUnpin,
}: BookBuilderProps) {
  const loadCurrent = useMutation({
    mutationFn: getCurrentBook,
    onSuccess: onUseCurrentBook,
  });

  function updateRow(key: number, patch: Partial<BookRow>) {
    onRowsChange(rows.map((r) => (r.key === key ? { ...r, ...patch } : r)));
  }

  function addRow() {
    onRowsChange([...rows, newBookRow()]);
  }

  function removeRow(key: number) {
    onRowsChange(rows.length > 1 ? rows.filter((r) => r.key !== key) : rows);
  }

  return (
    <div className="space-y-2" data-testid="book-builder">
      <div className="flex items-center justify-between">
        <span className="text-[10px] tracking-wider uppercase text-muted">{label}</span>
        <button
          type="button"
          className="border border-hairline bg-elevated hover:bg-hairline text-[11px] px-2 py-1 disabled:opacity-40"
          disabled={loadCurrent.isPending}
          onClick={() => loadCurrent.mutate()}
        >
          {loadCurrent.isPending ? "Loading…" : "Load current book"}
        </button>
      </div>
      {pinnedBookRef && (
        <div
          data-testid="book-pinned-chip"
          className="inline-flex items-center gap-2 border border-you/40 bg-you/10 text-you text-[11px] px-2 py-0.5 rounded-sm"
        >
          <span className="num">book {pinnedBookRef}</span>
          {pinnedAsOf && <span className="text-you/70 num">as of {pinnedAsOf.slice(0, 10)}</span>}
          {onUnpin && (
            <button
              type="button"
              aria-label="unpin current book"
              className="text-you/70 hover:text-you"
              onClick={onUnpin}
            >
              ×
            </button>
          )}
        </div>
      )}
      {loadCurrent.isError && (
        <p className="text-down text-[11px]">
          {String((loadCurrent.error as Error)?.message ?? loadCurrent.error)}
        </p>
      )}
      <div className="space-y-2">
        {rows.map((row, i) => (
          <div key={row.key} className="flex items-end gap-2 flex-wrap">
            <div className="flex-1 min-w-28">
              <label
                htmlFor={`book-symbol-${row.key}`}
                className="text-[10px] tracking-wider uppercase text-muted block mb-1"
              >
                {`Symbol ${i + 1}`}
              </label>
              <input
                id={`book-symbol-${row.key}`}
                aria-label={`symbol row ${i + 1}`}
                list={datalistId}
                className="num w-full bg-elevated border border-hairline px-2 py-1.5 text-ink text-[12px]"
                value={row.symbol}
                onChange={(e) => updateRow(row.key, { symbol: e.target.value.toUpperCase() })}
              />
            </div>
            <div className="w-24">
              <label
                htmlFor={`book-qty-${row.key}`}
                className="text-[10px] tracking-wider uppercase text-muted block mb-1"
              >
                {`Qty ${i + 1}`}
              </label>
              <input
                id={`book-qty-${row.key}`}
                aria-label={`qty row ${i + 1}`}
                type="number"
                className="num w-full bg-elevated border border-hairline px-2 py-1.5 text-ink text-[12px]"
                value={row.qty}
                onChange={(e) => updateRow(row.key, { qty: e.target.value })}
              />
            </div>
            {allowOptionLegs && (
              <div className="w-20">
                <label
                  htmlFor={`book-type-${row.key}`}
                  className="text-[10px] tracking-wider uppercase text-muted block mb-1"
                >
                  {`Type ${i + 1}`}
                </label>
                <select
                  id={`book-type-${row.key}`}
                  aria-label={`type row ${i + 1}`}
                  className="w-full bg-elevated border border-hairline px-2 py-1.5 text-ink text-[12px]"
                  value={row.secType ?? "STK"}
                  onChange={(e) => updateRow(row.key, { secType: e.target.value as "STK" | "OPT" })}
                >
                  <option value="STK">STK</option>
                  <option value="OPT">OPT</option>
                </select>
              </div>
            )}
            {allowOptionLegs && row.secType === "OPT" && (
              <>
                <div className="w-24">
                  <label
                    htmlFor={`book-strike-${row.key}`}
                    className="text-[10px] tracking-wider uppercase text-muted block mb-1"
                  >
                    {`Strike ${i + 1}`}
                  </label>
                  <input
                    id={`book-strike-${row.key}`}
                    aria-label={`strike row ${i + 1}`}
                    type="number"
                    min={0}
                    className="num w-full bg-elevated border border-hairline px-2 py-1.5 text-ink text-[12px]"
                    value={row.strike ?? ""}
                    onChange={(e) => updateRow(row.key, { strike: e.target.value })}
                  />
                </div>
                <div className="w-32">
                  <label
                    htmlFor={`book-expiry-${row.key}`}
                    className="text-[10px] tracking-wider uppercase text-muted block mb-1"
                  >
                    {`Expiry ${i + 1}`}
                  </label>
                  <input
                    id={`book-expiry-${row.key}`}
                    aria-label={`expiry row ${i + 1}`}
                    placeholder="YYYY-MM-DD"
                    className="num w-full bg-elevated border border-hairline px-2 py-1.5 text-ink text-[12px]"
                    value={row.expiry ?? ""}
                    onChange={(e) => updateRow(row.key, { expiry: e.target.value })}
                  />
                </div>
                <div className="w-16">
                  <label
                    htmlFor={`book-right-${row.key}`}
                    className="text-[10px] tracking-wider uppercase text-muted block mb-1"
                  >
                    {`Right ${i + 1}`}
                  </label>
                  <select
                    id={`book-right-${row.key}`}
                    aria-label={`right row ${i + 1}`}
                    className="w-full bg-elevated border border-hairline px-2 py-1.5 text-ink text-[12px]"
                    value={row.right ?? "C"}
                    onChange={(e) => updateRow(row.key, { right: e.target.value as "C" | "P" })}
                  >
                    <option value="C">C</option>
                    <option value="P">P</option>
                  </select>
                </div>
                <div className="w-20">
                  <label
                    htmlFor={`book-multiplier-${row.key}`}
                    className="text-[10px] tracking-wider uppercase text-muted block mb-1"
                  >
                    {`Mult ${i + 1}`}
                  </label>
                  <input
                    id={`book-multiplier-${row.key}`}
                    aria-label={`multiplier row ${i + 1}`}
                    type="number"
                    min={1}
                    className="num w-full bg-elevated border border-hairline px-2 py-1.5 text-ink text-[12px]"
                    value={row.multiplier ?? "100"}
                    onChange={(e) => updateRow(row.key, { multiplier: e.target.value })}
                  />
                </div>
              </>
            )}
            <button
              type="button"
              aria-label={`remove row ${i + 1}`}
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
    </div>
  );
}
