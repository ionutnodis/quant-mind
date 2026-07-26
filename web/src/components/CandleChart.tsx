// CandleChart: OHLC candlesticks for InstrumentSheet, drawn with Plotly
// `shapes` (wick + body rects) rather than the "candlestick" trace type —
// that trace lives only in Plotly's finance/full bundles, and this repo
// ships the lighter plotly.js-cartesian-dist-min (web/package.json isn't
// owned by this task). Market data (never book/amber, DESIGN.md law):
// up/down candles use the conventional green/red semantic tokens.
import { useEffect, useRef } from "react";
// @ts-expect-error - dist bundle has no types; typed via @types/plotly.js consumers
import Plotly from "plotly.js-cartesian-dist-min";
import { baseLayout, tokens } from "../lib/plotly-theme";

export interface Candle {
  date: string;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  volume: number | null;
}

export function CandleChart({ candles, height = 220 }: { candles: Candle[]; height?: number }) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current) return;
    const finite = candles.filter(
      (c) => c.open !== null && c.close !== null && c.high !== null && c.low !== null
    ) as Array<Candle & { open: number; high: number; low: number; close: number }>;

    const shapes = finite.flatMap((c, i) => {
      const up = c.close >= c.open;
      const color = up ? tokens.up : tokens.down;
      return [
        {
          type: "line" as const,
          x0: i,
          x1: i,
          y0: c.low,
          y1: c.high,
          line: { color, width: 1 },
        },
        {
          type: "rect" as const,
          x0: i - 0.3,
          x1: i + 0.3,
          y0: Math.min(c.open, c.close),
          y1: Math.max(c.open, c.close),
          fillcolor: color,
          line: { color, width: 1 },
        },
      ];
    });

    const step = Math.max(1, Math.ceil(finite.length / 6));
    const tickvals = finite.map((_, i) => i).filter((i) => i % step === 0);
    const ticktext = tickvals.map((i) => finite[i].date.slice(5, 10));

    const base = baseLayout();
    Plotly.newPlot(
      ref.current,
      [
        {
          type: "scatter",
          mode: "markers",
          x: finite.map((_, i) => i),
          y: finite.map((c) => c.close),
          marker: { size: 1, color: tokens.muted },
          hoverinfo: "skip",
        },
      ],
      {
        ...base,
        height,
        shapes,
        xaxis: { ...base.xaxis, tickmode: "array", tickvals, ticktext },
      },
      { displayModeBar: false, responsive: true }
    );
    const node = ref.current;
    return () => Plotly.purge(node);
  }, [candles, height]);

  return (
    <div
      ref={ref}
      data-testid="candle-chart"
      role="img"
      aria-label="candlestick chart"
      style={{ height }}
    />
  );
}
