// Thin typed API client. Token comes from Vite env (VITE_QM_TOKEN); in dev the
// backend usually runs tokenless. Types are hand-minimal until the
// openapi-typescript generation step replaces them (drift test guards it).

export interface Tile {
  symbol: string;
  last_close: number;
  change_1d: number;
}

export interface Brief {
  tiles: Tile[];
  correlation: { symbols: string[]; matrix: (number | null)[][] } | null;
  benchmark_es: number | null;
  as_of: string | null;
}

const TOKEN = import.meta.env.VITE_QM_TOKEN as string | undefined;

// Shared fetch wrapper: attaches the bearer token + JSON content type, and —
// unlike a bare fetch() — parses a structured `{ detail }` error body (the
// shape every backend 422 uses) into the thrown Error's message so callers
// can surface the server's actual reason instead of a bare status code.
export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {}),
      ...(options.headers ?? {}),
    },
  });
  if (!res.ok) {
    let detail = `${path} → ${res.status}`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body?.detail) detail = body.detail;
    } catch {
      // non-JSON error body — fall back to the status line above
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

function get<T>(path: string): Promise<T> {
  return request<T>(path);
}

export interface ModelSchema {
  name: string;
  label?: string;
  factor?: { kind: string; units: string; dt: number };
}

export const api = {
  brief: () => get<Brief>("/api/brief"),
  health: () => get<{ status: string }>("/api/health"),
  models: () => get<ModelSchema[]>("/api/models"),
};
