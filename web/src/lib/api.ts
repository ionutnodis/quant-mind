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

async function get<T>(path: string): Promise<T> {
  const res = await fetch(path, {
    headers: TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {},
  });
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json() as Promise<T>;
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
