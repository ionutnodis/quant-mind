// Rolling beta vs the benchmark, through the single Plotly theme (CorrelationHeatmap
// pattern) — no color literals here. Symbol lens, not book data: line is market
// steel, never amber (DESIGN.md amber law). Tolerance band hairlines mark the
// beta=1 reference (a symbol identical to the benchmark sits on this line).
import { useEffect, useRef } from "react";
// @ts-expect-error - dist bundle has no types; typed via @types/plotly.js consumers
import Plotly from "plotly.js-cartesian-dist-min";
import { baseLayout, tokens } from "../lib/plotly-theme";

export interface BetaPoint {
  date: string;
  beta: number | null;
}

export function RollingBetaChart({
  points,
  benchmark,
}: {
  points: BetaPoint[];
  benchmark: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current) return;
    const band = (y: number) => ({
      type: "line" as const,
      xref: "paper" as const,
      x0: 0,
      x1: 1,
      y0: y,
      y1: y,
      line: { color: tokens.hairline, width: 1, dash: "dot" as const },
    });
    Plotly.newPlot(
      ref.current,
      [
        {
          type: "scatter",
          mode: "lines",
          x: points.map((p) => p.date),
          y: points.map((p) => p.beta),
          line: { color: tokens.market, width: 1.5 },
          connectgaps: false,
        },
      ],
      { ...baseLayout(), height: 240, shapes: [band(1), band(1.2), band(0.8)] },
      { displayModeBar: false, responsive: true }
    );
    const node = ref.current;
    return () => Plotly.purge(node);
  }, [points, benchmark]);
  return (
    <div
      ref={ref}
      data-testid="rolling-beta-chart"
      role="img"
      aria-label={`Rolling beta vs ${benchmark}, dotted hairlines at 1.0 +/- 0.2`}
    />
  );
}
