// GlanceCharts: "charts at a glance" row (wave-3B Today task — "full
// mini-charts for major indices, VIX, oil, gold, and the yield spread").
// Reuses CandleChart/SeriesChart through the one Plotly theme (DESIGN.md)
// rather than inventing a new mini-chart component; every symbol goes
// through InstrumentHover (instrument identity law). Store-backed only
// (GET /api/instruments/{symbol}/candles, GET /api/macro's already-served
// yields block) — an instrument with no cached candles renders an honest
// "no data" cell instead of a broken chart, never a crash.
import { useQuery } from "@tanstack/react-query";
import { request } from "../lib/api";
import { CandleChart, type Candle } from "./CandleChart";
import { SeriesChart, type SeriesPoint } from "./SeriesChart";
import { InstrumentHover } from "./InstrumentHover";

interface CandlesResponse {
  symbol: string;
  days: number;
  candles: Candle[];
}

function getCandles(symbol: string, days: number): Promise<CandlesResponse> {
  return request<CandlesResponse>(
    `/api/instruments/${encodeURIComponent(symbol)}/candles?days=${days}`
  );
}

interface MacroSeriesPoint {
  date: string;
  value: number | null;
}

interface MacroResponse {
  yields: {
    spread_2s10s: number | null;
    series: Record<string, MacroSeriesPoint[]>;
  } | null;
}

function getMacro(): Promise<MacroResponse> {
  return request<MacroResponse>("/api/macro");
}

const GLANCE_DAYS = 90;
const CHART_HEIGHT = 96;

const GLANCE_INSTRUMENTS: { symbol: string; label: string }[] = [
  { symbol: "SPX", label: "S&P 500" },
  { symbol: "VIX", label: "VIX" },
  { symbol: "USO", label: "Oil (USO)" },
  { symbol: "GLD", label: "Gold (GLD)" },
];

// us10y/us2y are FRED series sharing the same calendar (both cached by
// sources/fred.py's sync), so a plain date-keyed join is enough — this
// deliberately does NOT try to align mismatched calendars, that's macro.py's
// job for the authoritative spread number this chart is just visualizing.
function buildSpread(series: Record<string, MacroSeriesPoint[]> | undefined): SeriesPoint[] {
  if (!series?.us10y || !series?.us2y) return [];
  const shortEnd = new Map(series.us2y.map((p) => [p.date, p.value]));
  return series.us10y
    .filter((p) => shortEnd.has(p.date))
    .map((p) => {
      const short = shortEnd.get(p.date) ?? null;
      const value = p.value !== null && short !== null ? p.value - short : null;
      return { date: p.date, value };
    });
}

function GlanceCell({ symbol, label }: { symbol: string; label: string }) {
  const { data, isLoading } = useQuery({
    queryKey: ["glance-candles", symbol],
    queryFn: () => getCandles(symbol, GLANCE_DAYS),
    staleTime: 5 * 60 * 1000,
    retry: false,
  });

  return (
    <div
      data-testid={`glance-${symbol}`}
      className="border border-hairline bg-ground/40 p-2 min-w-0"
    >
      <div className="text-[10px] tracking-wider uppercase text-muted mb-1">
        <InstrumentHover symbol={symbol}>{label}</InstrumentHover>
      </div>
      {isLoading ? (
        <div className="animate-pulse bg-elevated" style={{ height: CHART_HEIGHT }} />
      ) : data && data.candles.length > 0 ? (
        <CandleChart candles={data.candles} height={CHART_HEIGHT} />
      ) : (
        <p
          className="text-muted text-[10px] flex items-center justify-center"
          style={{ height: CHART_HEIGHT }}
        >
          no cached candles
        </p>
      )}
    </div>
  );
}

export function GlanceCharts() {
  const macro = useQuery({
    queryKey: ["macro"],
    queryFn: getMacro,
    staleTime: 5 * 60 * 1000,
    retry: false,
  });
  const spread = buildSpread(macro.data?.yields?.series);

  return (
    <div className="grid grid-cols-5 gap-2" data-testid="glance-charts">
      {GLANCE_INSTRUMENTS.map((g) => (
        <GlanceCell key={g.symbol} symbol={g.symbol} label={g.label} />
      ))}
      <div data-testid="glance-2s10s" className="border border-hairline bg-ground/40 p-2 min-w-0">
        <div className="text-[10px] tracking-wider uppercase text-muted mb-1">2s10s spread</div>
        {macro.isLoading ? (
          <div className="animate-pulse bg-elevated" style={{ height: CHART_HEIGHT }} />
        ) : spread.length > 0 ? (
          <SeriesChart points={spread} label="2s10s" colorToken="market" height={CHART_HEIGHT} />
        ) : (
          <p
            className="text-muted text-[10px] flex items-center justify-center"
            style={{ height: CHART_HEIGHT }}
          >
            no cached yields
          </p>
        )}
      </div>
    </div>
  );
}
