// Regression scatter: asset returns vs the primary factor's returns, with
// the single-factor OLS fit line overlaid — through the single Plotly theme
// (RollingBetaChart/FanChart pattern). Symbol lens, not book data (DESIGN.md
// amber law): points and line are market steel/ink, never amber.
import { useEffect, useRef } from "react";
// @ts-expect-error - dist bundle has no types; typed via @types/plotly.js consumers
import Plotly from "plotly.js-cartesian-dist-min";
import { baseLayout, tokens } from "../lib/plotly-theme";

export interface ScatterPoint {
  date: string;
  asset: number | null;
  factor: number | null;
}

export function RegressionScatter({
  points,
  slope,
  intercept,
  factorLabel,
  assetLabel,
}: {
  points: ScatterPoint[];
  slope: number | null;
  intercept: number | null;
  factorLabel: string;
  assetLabel: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current) return;
    const xs = points.map((p) => p.factor).filter((v): v is number => v !== null);
    const traces: Record<string, unknown>[] = [
      {
        type: "scatter",
        mode: "markers",
        x: points.map((p) => p.factor),
        y: points.map((p) => p.asset),
        marker: { color: tokens.market, size: 4, opacity: 0.55 },
        name: "obs",
      },
    ];
    if (slope !== null && intercept !== null && xs.length > 0) {
      const xMin = Math.min(...xs);
      const xMax = Math.max(...xs);
      traces.push({
        type: "scatter",
        mode: "lines",
        x: [xMin, xMax],
        y: [slope * xMin + intercept, slope * xMax + intercept],
        line: { color: tokens.ink, width: 1.5, dash: "dash" },
        name: "OLS fit",
      });
    }
    const layout = baseLayout();
    Plotly.newPlot(
      ref.current,
      traces,
      {
        ...layout,
        height: 280,
        xaxis: { ...layout.xaxis, title: { text: factorLabel } },
        yaxis: { ...layout.yaxis, title: { text: assetLabel } },
      },
      { displayModeBar: false, responsive: true }
    );
    const node = ref.current;
    return () => Plotly.purge(node);
  }, [points, slope, intercept, factorLabel, assetLabel]);
  return (
    <div
      ref={ref}
      data-testid="regression-scatter"
      role="img"
      aria-label={`Scatter of ${assetLabel} daily returns vs ${factorLabel}, with OLS fit line`}
    />
  );
}
