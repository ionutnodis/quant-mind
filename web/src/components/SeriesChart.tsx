// SeriesChart: single-line time series chart through the one Plotly theme
// (RollingBetaChart/FanChart pattern) — no color literals here. Named-series
// macro data (yields, net liquidity) is market data, never book data
// (DESIGN.md amber law): defaults to steel; callers may pass another token
// key for deliberately distinct market lines, but never "you".
import { useEffect, useRef } from "react";
// @ts-expect-error - dist bundle has no types; typed via @types/plotly.js consumers
import Plotly from "plotly.js-cartesian-dist-min";
import { baseLayout, tokens } from "../lib/plotly-theme";

export interface SeriesPoint {
  date: string;
  value: number | null;
}

export function SeriesChart({
  points,
  label,
  colorToken = "market",
  height = 200,
}: {
  points: SeriesPoint[];
  label: string;
  colorToken?: "market" | "you" | "ink" | "muted";
  height?: number;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current) return;
    Plotly.newPlot(
      ref.current,
      [
        {
          type: "scatter",
          mode: "lines",
          name: label,
          x: points.map((p) => p.date),
          y: points.map((p) => p.value),
          line: { color: tokens[colorToken], width: 1.5 },
          connectgaps: false,
        },
      ],
      { ...baseLayout(), height },
      { displayModeBar: false, responsive: true }
    );
    const node = ref.current;
    return () => Plotly.purge(node);
  }, [points, label, colorToken, height]);
  return (
    <div
      ref={ref}
      data-testid="series-chart"
      role="img"
      aria-label={`${label} time series`}
    />
  );
}
