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

// Pair-pipeline z-score bands chart (wave-3B): the EG spread in steel with
// ±1σ/±2σ stationary bands around the OU long-run mean and the current
// (last) observation marked. Market data — steel/muted only, never amber.
export interface PairBandsChartProps {
  dates: string[];
  values: (number | null)[];
  mu: number | null;
  sigma: number | null;
}

export function PairBandsChart({ dates, values, mu, sigma }: PairBandsChartProps) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current) return;
    const traces: Partial<Data>[] = [];

    if (mu !== null && sigma !== null && dates.length > 1) {
      const edge = [dates[0], dates[dates.length - 1]];
      // ±2σ then ±1σ fills (outer fainter), mean dashed.
      for (const [k, opacity] of [
        [2, 0.08],
        [1, 0.16],
      ] as [number, number][]) {
        traces.push({
          type: "scatter",
          mode: "lines",
          x: [...edge, ...edge.slice().reverse()],
          y: [mu + k * sigma, mu + k * sigma, mu - k * sigma, mu - k * sigma],
          fill: "toself",
          fillcolor: tokens.market,
          opacity,
          line: { width: 0 },
          hoverinfo: "skip",
        });
      }
      traces.push({
        type: "scatter",
        mode: "lines",
        x: edge,
        y: [mu, mu],
        line: { color: tokens.muted, width: 1, dash: "dash" },
        hoverinfo: "skip",
      });
    }

    traces.push({
      type: "scatter",
      mode: "lines",
      x: dates,
      y: values,
      line: { color: tokens.market, width: 1.5 },
      hoverinfo: "skip",
    });

    const last = values[values.length - 1];
    if (last !== null && last !== undefined) {
      traces.push({
        type: "scatter",
        mode: "markers",
        x: [dates[dates.length - 1]],
        y: [last],
        marker: { color: tokens.ink, size: 7 },
        hoverinfo: "skip",
      });
    }

    Plotly.newPlot(
      ref.current,
      traces,
      { ...baseLayout(), height: 260 },
      { displayModeBar: false, responsive: true }
    );
    const node = ref.current;
    return () => Plotly.purge(node);
  }, [dates, values, mu, sigma]);

  return (
    <div
      ref={ref}
      data-testid="pair-bands-chart"
      role="img"
      aria-label="Pair spread with stationary ±1σ and ±2σ bands around the OU long-run mean, current observation marked"
    />
  );
}
