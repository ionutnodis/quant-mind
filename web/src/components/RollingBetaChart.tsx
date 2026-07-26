// Rolling beta vs the benchmark at several windows, with a full-sample beta
// drawn as long-run context — through the single Plotly theme
// (CorrelationHeatmap pattern) — no color literals here. Symbol lens, not
// book data: lines are market steel, never amber (DESIGN.md amber law).
// Tolerance band hairlines mark the beta=1 reference (a symbol identical to
// the benchmark sits on this line). The full-sample dashed line exists
// because a rolling window alone reads as unstable without a long-run
// anchor to compare it against (a short window can drift meaningfully off a
// stable full-sample beta and still be "normal" noise, not a regime shift).
import { useEffect, useRef } from "react";
// @ts-expect-error - dist bundle has no types; typed via @types/plotly.js consumers
import Plotly from "plotly.js-cartesian-dist-min";
import { baseLayout, tokens } from "../lib/plotly-theme";

export interface BetaPoint {
  date: string;
  beta: number | null;
}

export interface BetaWindowSeries {
  window: number;
  points: BetaPoint[];
}

// Shorter windows are noisier/more reactive, so they render lighter; the
// longest window ships closest to full opacity, visually "settling" toward
// the full-sample dashed reference line.
const OPACITY_BY_RANK = [0.4, 0.7, 1.0];

export function RollingBetaChart({
  series,
  fullSampleBeta,
  benchmark,
}: {
  series: BetaWindowSeries[];
  fullSampleBeta: number | null;
  benchmark: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current) return;
    const line = (y: number, color: string, width: number, dash: "dot" | "dash") => ({
      type: "line" as const,
      xref: "paper" as const,
      x0: 0,
      x1: 1,
      y0: y,
      y1: y,
      line: { color, width, dash },
    });
    const shapes = [
      line(1, tokens.hairline, 1, "dot"),
      line(1.2, tokens.hairline, 1, "dot"),
      line(0.8, tokens.hairline, 1, "dot"),
    ];
    if (fullSampleBeta !== null) {
      shapes.push(line(fullSampleBeta, tokens.ink, 1.5, "dash"));
    }
    const traces = series.map((s, i) => ({
      type: "scatter" as const,
      mode: "lines" as const,
      name: `${s.window}d`,
      x: s.points.map((p) => p.date),
      y: s.points.map((p) => p.beta),
      line: { color: tokens.market, width: 1.5 },
      opacity: OPACITY_BY_RANK[Math.min(i, OPACITY_BY_RANK.length - 1)],
      connectgaps: false,
    }));
    Plotly.newPlot(
      ref.current,
      traces,
      { ...baseLayout(), height: 260, shapes, showlegend: series.length > 1 },
      { displayModeBar: false, responsive: true }
    );
    const node = ref.current;
    return () => Plotly.purge(node);
  }, [series, fullSampleBeta, benchmark]);
  const windowsLabel = series.map((s) => `${s.window}d`).join("/");
  const fullSampleLabel =
    fullSampleBeta !== null ? `, dashed line at full-sample beta ${fullSampleBeta.toFixed(2)}` : "";
  return (
    <div
      ref={ref}
      data-testid="rolling-beta-chart"
      role="img"
      aria-label={`Rolling beta vs ${benchmark} at ${windowsLabel} day windows${fullSampleLabel}, dotted hairlines at 1.0 +/- 0.2`}
    />
  );
}
