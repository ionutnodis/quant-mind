// Plotly heatmap through the single theme module — no color literals here.
import { useEffect, useRef } from "react";
// @ts-expect-error - dist bundle has no types; typed via @types/plotly.js consumers
import Plotly from "plotly.js-cartesian-dist-min";
import { baseLayout, tokens } from "../lib/plotly-theme";

export function CorrelationHeatmap({
  data,
}: {
  data: { symbols: string[]; matrix: (number | null)[][] };
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current) return;
    Plotly.newPlot(
      ref.current,
      [
        {
          type: "heatmap",
          x: data.symbols,
          y: data.symbols,
          z: data.matrix,
          zmin: -1,
          zmax: 1,
          // Amber law: amber marks the book, never market data. Inverse
          // correlation = muted gray, positive = market steel.
          colorscale: [
            [0, tokens.muted],
            [0.5, tokens.ground],
            [1, tokens.market],
          ],
          showscale: false,
        },
      ],
      { ...baseLayout, height: 260 },
      { displayModeBar: false, responsive: true }
    );
    const node = ref.current;
    return () => Plotly.purge(node);
  }, [data]);
  return <div ref={ref} data-testid="corr-heatmap" />;
}
