// Monte Carlo terminal-return distribution: histogram of block-bootstrap
// terminal returns with p5/p50/p95 markers, through the single Plotly theme.
// Symbol lens, not book data (DESIGN.md amber law) — bars are market steel.
import { useEffect, useRef } from "react";
// @ts-expect-error - dist bundle has no types; typed via @types/plotly.js consumers
import Plotly from "plotly.js-cartesian-dist-min";
import { baseLayout, tokens } from "../lib/plotly-theme";

export interface Histogram {
  bin_edges: number[];
  counts: number[];
}

export function FanChart({
  histogram,
  p5,
  p50,
  p95,
}: {
  histogram: Histogram;
  p5: number | null;
  p50: number | null;
  p95: number | null;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current) return;
    const centers = histogram.bin_edges
      .slice(0, -1)
      .map((edge, i) => (edge + histogram.bin_edges[i + 1]) / 2);
    const markers = [p5, p50, p95].filter((v): v is number => v !== null);
    Plotly.newPlot(
      ref.current,
      [
        {
          type: "bar",
          x: centers,
          y: histogram.counts,
          marker: { color: tokens.market },
        },
      ],
      {
        ...baseLayout(),
        height: 240,
        shapes: markers.map((v) => ({
          type: "line" as const,
          xref: "x" as const,
          x0: v,
          x1: v,
          yref: "paper" as const,
          y0: 0,
          y1: 1,
          line: { color: tokens.ink, width: 1, dash: "dash" as const },
        })),
      },
      { displayModeBar: false, responsive: true }
    );
    const node = ref.current;
    return () => Plotly.purge(node);
  }, [histogram, p5, p50, p95]);
  return (
    <div
      ref={ref}
      data-testid="fan-chart"
      role="img"
      aria-label="Monte Carlo terminal return distribution with p5, p50, p95 markers"
    />
  );
}
