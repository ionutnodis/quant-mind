// Thin typed API client. Token comes from Vite env (VITE_QM_TOKEN); in dev the
// backend usually runs tokenless. Types are hand-minimal until the
// openapi-typescript generation step replaces them (drift test guards it).

import type { components } from "./api-types";

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

function errorDetail(value: unknown): string | undefined {
  if (typeof value === "string") return value.trim() || undefined;
  if (!Array.isArray(value)) return undefined;
  // FastAPI validation evidence also carries `input` and `ctx`, which can
  // contain credentials. Only use its explicit field location and message.
  const messages = value.slice(0, 8).flatMap((issue: unknown) => {
    if (!issue || typeof issue !== "object") return [];
    const { loc, msg } = issue as { loc?: unknown; msg?: unknown };
    if (typeof msg !== "string" || !msg.trim()) return [];
    const field = Array.isArray(loc) ? loc.slice(0, 8).reduce<string>((name, part) => {
      if (typeof part === "number" && Number.isSafeInteger(part) && part >= 0) return `${name}[${part}]`;
      if (typeof part !== "string" || !/^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(part) || ["body", "query", "path", "header"].includes(part)) return name;
      return name ? `${name}.${part}` : part;
    }, "") : "";
    return [`${field ? `${field}: ` : ""}${msg.trim().slice(0, 300)}`];
  });
  return messages.length ? [...new Set(messages)].join("; ") : undefined;
}

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
      const body = (await res.json()) as { detail?: unknown };
      detail = errorDetail(body?.detail) ?? detail;
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

export type SetupStatus = components["schemas"]["SetupStatus"];

export const api = {
  brief: () => get<Brief>("/api/brief"),
  health: () => get<{ status: string }>("/api/health"),
  models: () => get<ModelSchema[]>("/api/models"),
  setupStatus: () => get<SetupStatus>("/api/setup/status"),
};
