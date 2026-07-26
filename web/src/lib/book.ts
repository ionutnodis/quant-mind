// book.ts: page-local typed helpers over the book-flow spine
// (POST /api/book/pin, GET /api/book/current, GET /api/book/{id}) plus a
// tiny URL-param-persisted store of the "active" pinned snapshot id — the
// "open in…" spine wave-3B's pages build on. Uses `request<T>()` from
// ./api (api.ts is not owned by this task — Global Constraints: define
// page-local typed helpers rather than editing it).
import { request } from "./api";

export interface BookPositionOut {
  symbol: string;
  qty: number;
  con_id: number | null;
  sec_type: string;
  multiplier: number;
}

export interface BookSnapshotOut {
  snapshot_id: string;
  valuation_ts: string;
  base_currency: string;
  positions: BookPositionOut[];
}

export function getCurrentBook(): Promise<BookSnapshotOut> {
  return request<BookSnapshotOut>("/api/book/current");
}

export function getBook(snapshotId: string): Promise<BookSnapshotOut> {
  return request<BookSnapshotOut>(`/api/book/${encodeURIComponent(snapshotId)}`);
}

export function pinBook(positions: { symbol: string; qty: number }[]): Promise<BookSnapshotOut> {
  return request<BookSnapshotOut>("/api/book/pin", {
    method: "POST",
    body: JSON.stringify({ positions }),
  });
}

const BOOK_REF_PARAM = "book_ref";

/** Reads the active book_ref from the current URL's query string (`?book_ref=...`). */
export function readActiveBookRef(): string | null {
  if (typeof window === "undefined") return null;
  return new URLSearchParams(window.location.search).get(BOOK_REF_PARAM);
}

/** Persists (or clears, when null) the active book_ref into the URL's query
 * string without pushing a new history entry — a page reload/share keeps
 * the same pinned book in view. */
export function writeActiveBookRef(snapshotId: string | null): void {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  if (snapshotId) {
    url.searchParams.set(BOOK_REF_PARAM, snapshotId);
  } else {
    url.searchParams.delete(BOOK_REF_PARAM);
  }
  window.history.replaceState(null, "", url);
}
