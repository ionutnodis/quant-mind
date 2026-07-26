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
  // Option-leg fields (wave-3A book pin): null/absent for plain equity legs.
  strike?: number | null;
  expiry?: string | null;
  right?: "C" | "P" | null;
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

/** The full leg shape POST /api/book/pin accepts (batch-2 final review item
 * 7k): option fields optional — a bare {symbol, qty} is a plain equity leg. */
export interface PinPositionIn {
  symbol: string;
  qty: number;
  strike?: number | null;
  expiry?: string | null;
  right?: "C" | "P" | null;
  multiplier?: number | null;
}

export function pinBook(positions: PinPositionIn[]): Promise<BookSnapshotOut> {
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

// --- Pinned what-if scenarios (wave-3B What-If flow): a named summary of a
// computed result, stored locally and compared side-by-side. The record
// lives in localStorage; the pinned NAMES are URL-persisted (like the
// active book_ref above) so a reload keeps the same compare set in view. ---

export interface PinnedScenario {
  name: string;
  pinned_at: string;
  as_of: string | null;
  // Every risk number stays horizon-labeled: MC stats are over horizon_days,
  // ES is daily, vol is annualized, beta is the 60d rolling estimate.
  horizon_days: number;
  n_paths: number;
  seed: number | null;
  beta: number | null;
  es_975: number | null;
  ann_vol: number | null;
  p5: number | null;
  p50: number | null;
  p95: number | null;
}

const PINNED_SCENARIOS_KEY = "quantmind.whatif.pins";
const PINS_PARAM = "pins";

/** Shape check for one stored pin (fix round 1, I2 minor): localStorage is
 * user-editable junk territory — a corrupt value must never TypeError the
 * compare table, it just doesn't load. */
function isPinnedScenario(v: unknown): v is PinnedScenario {
  if (typeof v !== "object" || v === null) return false;
  const p = v as Record<string, unknown>;
  return (
    typeof p.name === "string" &&
    typeof p.horizon_days === "number" &&
    typeof p.n_paths === "number"
  );
}

export function readPinnedScenarios(): Record<string, PinnedScenario> {
  try {
    const raw = localStorage.getItem(PINNED_SCENARIOS_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : {};
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return {};
    const out: Record<string, PinnedScenario> = {};
    for (const [k, v] of Object.entries(parsed)) {
      if (isPinnedScenario(v)) out[k] = v;
    }
    return out;
  } catch {
    return {};
  }
}

export function writePinnedScenarios(pins: Record<string, PinnedScenario>): void {
  try {
    localStorage.setItem(PINNED_SCENARIOS_KEY, JSON.stringify(pins));
  } catch {
    // localStorage unavailable (private mode, quota) — pins just don't persist.
  }
}

/** Reads the URL-persisted pinned-scenario names (`?pins=a,b`). Names are
 * per-name percent-encoded on write so a name containing "," survives the
 * join/split round-trip (fix round 1, I2 minor). */
export function readPinnedNames(): string[] {
  if (typeof window === "undefined") return [];
  const raw = new URLSearchParams(window.location.search).get(PINS_PARAM);
  if (!raw) return [];
  return raw
    .split(",")
    .filter((n) => n !== "")
    .map((n) => {
      try {
        return decodeURIComponent(n);
      } catch {
        return n; // malformed escape in a hand-edited URL — take it literally
      }
    });
}

/** Mirrors the pinned-scenario names into the URL (replaceState, like
 * writeActiveBookRef) — an empty list clears the param. */
export function writePinnedNames(names: string[]): void {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  if (names.length) {
    url.searchParams.set(PINS_PARAM, names.map((n) => encodeURIComponent(n)).join(","));
  } else {
    url.searchParams.delete(PINS_PARAM);
  }
  window.history.replaceState(null, "", url);
}
