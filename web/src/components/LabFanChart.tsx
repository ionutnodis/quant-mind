// Lab bench Simulate-zone fan chart: percentile bands in steel with an
// opacity ramp, p50 solid, a handful of faint sample paths underneath.
// DESIGN.md chart rule: market/simulation data is steel, never amber — amber
// is reserved for the Apply-to-Book zone. No color literals: everything comes
// from the shared Plotly theme token reader.
import { useEffect, useRef } from "react";
import type { Data } from "plotly.js";
// @ts-expect-error - dist bundle has no types; typed via @types/plotly.js consumers
import Plotly from "plotly.js-cartesian-dist-min";
import { baseLayout, tokens } from "../lib/plotly-theme";

export interface LabFanChartProps {
  bands: Record<string, number[]>;
  samplePaths?: number[][];
}

// [lower key, upper key, band opacity] — inner band denser than outer.
const BANDS: [string, string, number][] = [
  ["p5", "p95", 0.1],
  ["p25", "p75", 0.22],
];

export function LabFanChart({ bands, samplePaths = [] }: LabFanChartProps) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current) return;
    const horizon = bands.p50?.length ?? 0;
    const x = Array.from({ length: horizon }, (_, i) => i + 1);
    const traces: Partial<Data>[] = [];

    for (const path of samplePaths.slice(0, 30)) {
      traces.push({
        type: "scatter",
        mode: "lines",
        x,
        y: path,
        line: { color: tokens.market, width: 1 },
        opacity: 0.08,
        hoverinfo: "skip",
      });
    }

    for (const [lo, hi, opacity] of BANDS) {
      if (!bands[lo] || !bands[hi]) continue;
      traces.push({
        type: "scatter",
        mode: "lines",
        x: [...x, ...x.slice().reverse()],
        y: [...bands[hi], ...bands[lo].slice().reverse()],
        fill: "toself",
        fillcolor: tokens.market,
        opacity,
        line: { width: 0 },
        hoverinfo: "skip",
      });
    }

    if (bands.p50) {
      traces.push({
        type: "scatter",
        mode: "lines",
        x,
        y: bands.p50,
        line: { color: tokens.market, width: 2 },
        hoverinfo: "skip",
      });
    }

    Plotly.newPlot(
      ref.current,
      traces,
      { ...baseLayout(), height: 280 },
      { displayModeBar: false, responsive: true }
    );
    const node = ref.current;
    return () => Plotly.purge(node);
  }, [bands, samplePaths]);

  return (
    <div
      ref={ref}
      data-testid="lab-fan-chart"
      role="img"
      aria-label="Simulated percentile fan chart of the fitted model, median solid, p5-p95 and p25-p75 bands"
    />
  );
}
