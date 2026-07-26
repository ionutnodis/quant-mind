// BookBuilder: the shared position-row builder (wave-3 Task A1's book-flow
// spine) — Hedge's string-qty row variant as base (kept as text while
// editing so a half-typed qty like "-" or "" doesn't get coerced to 0
// mid-keystroke). WhatIf and Hedge both swap their bespoke row builders for
// this component. A "Load current book" button pulls GET /api/book/current
// (lib/book.ts) and hands the resulting snapshot to the parent via
// `onUseCurrentBook` — the parent decides whether to populate rows for
// display, remember the book_ref, or both (WhatIf/Hedge do both: show the
// live positions AND submit by book_ref rather than re-deriving them).
import { useMutation } from "@tanstack/react-query";
import { getCurrentBook, type BookSnapshotOut } from "../lib/book";

export interface BookRow {
  key: number;
  symbol: string;
  qty: string; // kept as text while editing; parsed to number on submit
}

let rowKeySeq = 0;

export function newBookRow(overrides: Partial<Omit<BookRow, "key">> = {}): BookRow {
  rowKeySeq += 1;
  return { key: rowKeySeq, symbol: "", qty: "1", ...overrides };
}

/** Rows -> the {symbol, qty} pairs whatif/hedge accept: blank symbols and
 * zero/unparseable quantities are dropped rather than submitted. */
export function rowsToPositions(rows: BookRow[]): { symbol: string; qty: number }[] {
  return rows
    .filter((r) => r.symbol.trim() !== "")
    .map((r) => ({ symbol: r.symbol.trim().toUpperCase(), qty: Number(r.qty) || 0 }))
    .filter((p) => p.qty !== 0);
}

/** A pinned snapshot's positions rendered back as editable rows (for the
 * "Load current book" flow) — an empty book still yields one blank row so
 * the builder never renders with zero rows. */
export function snapshotToRows(snapshot: BookSnapshotOut): BookRow[] {
  if (snapshot.positions.length === 0) return [newBookRow()];
  return snapshot.positions.map((p) => newBookRow({ symbol: p.symbol, qty: String(p.qty) }));
}

interface BookBuilderProps {
  rows: BookRow[];
  onRowsChange: (rows: BookRow[]) => void;
  onUseCurrentBook: (snapshot: BookSnapshotOut) => void;
  datalistId?: string;
  label?: string;
}

export function BookBuilder({ rows, onRowsChange, onUseCurrentBook, datalistId, label = "Book" }: BookBuilderProps) {
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
      {loadCurrent.isError && (
        <p className="text-down text-[11px]">
          {String((loadCurrent.error as Error)?.message ?? loadCurrent.error)}
        </p>
      )}
      <div className="space-y-2">
        {rows.map((row, i) => (
          <div key={row.key} className="flex items-end gap-2">
            <div className="flex-1">
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
