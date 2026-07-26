// PortfolioAttributionChart: daily core-vs-overlay P&L split (Task B1's
// identity number — "is the overlay adding alpha?"). Core (beta*bench_return
// *book_value) is the market-driven piece, so it renders in the neutral
// market steel; overlay (the residual — the user's own active decisions on
// top of the beta core) is book data and renders in amber, the ONE sanctioned
// non-nav/non-wordmark amber use besides "your book" figures (DESIGN.md amber
// law) — this chart's whole point is making the overlay's contribution
// visually legible against the core.
import { useEffect, useRef } from "react";
import type { Data } from "plotly.js";
// @ts-expect-error - dist bundle has no types; typed via @types/plotly.js consumers
import Plotly from "plotly.js-cartesian-dist-min";
import { baseLayout, tokens } from "../lib/plotly-theme";

export interface AttributionPoint {
  date: string;
  total_pnl: number | null;
  core_pnl: number | null;
  overlay_pnl: number | null;
}

export function PortfolioAttributionChart({ series }: { series: AttributionPoint[] }) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current) return;
    const x = series.map((p) => p.date);
    const traces: Partial<Data>[] = [
      {
        type: "bar",
        name: "core (beta x bench)",
        x,
        y: series.map((p) => p.core_pnl),
        marker: { color: tokens.market },
      },
      {
        type: "bar",
        name: "overlay (residual)",
        x,
        y: series.map((p) => p.overlay_pnl),
        marker: { color: tokens.you },
      },
    ];
    Plotly.newPlot(
      ref.current,
      traces,
      { ...baseLayout(), height: 240, barmode: "relative", showlegend: true },
      { displayModeBar: false, responsive: true }
    );
    const node = ref.current;
    return () => Plotly.purge(node);
  }, [series]);

  return (
    <div
      ref={ref}
      data-testid="portfolio-attribution-chart"
      role="img"
      aria-label="Daily book P&L split into core (beta times benchmark return) and overlay (residual)"
    />
  );
}
