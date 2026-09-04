// Macro: yields/curve, Fed net liquidity, sector & factor rotation — each
// with the user's exposure noted (DESIGN.md IA #6). Nothing here is book
// data (amber law): yields/net-liquidity lines are market steel, returns use
// the conventional green/red glyph. Every symbol row links OUT to
// TradingView — the augmentation posture made literal, not a chart QuantMind
// re-implements.
import { useQuery } from "@tanstack/react-query";
import { request } from "../lib/api";
import { InstrumentHover } from "../components/InstrumentHover";
import { Panel, Skeleton } from "../components/Panel";
import { SeriesChart, type SeriesPoint } from "../components/SeriesChart";

interface YieldsBlock {
  us10y: number | null;
  us2y: number | null;
  us3m: number | null;
  spread_2s10s: number | null;
  series: { us10y: SeriesPoint[]; us2y: SeriesPoint[]; us3m: SeriesPoint[] };
}

interface NetLiquidityBlock {
  latest_bn: number | null;
  series: SeriesPoint[];
  cadence_note: string;
}

interface RotationRow {
  symbol: string;
  ret_1d: number | null;
  ret_1m: number | null;
  ret_3m: number | null;
}

interface MacroResponse {
  yields: YieldsBlock | null;
  net_liquidity: NetLiquidityBlock | null;
  sectors: RotationRow[];
  factors: RotationRow[];
  as_of: string | null;
  missing: string[];
}

function getMacro(): Promise<MacroResponse> {
  return request<MacroResponse>("/api/macro");
}

function tradingViewUrl(symbol: string): string {
  return `https://www.tradingview.com/chart/?symbol=${encodeURIComponent(symbol)}`;
}

function pct(x: number | null): string {
  if (x === null) return "—";
  return `${(x * 100).toFixed(2)}%`;
}

function signedPct(x: number | null): string {
  if (x === null) return "—";
  const sign = x >= 0 ? "+" : "";
  return `${sign}${(x * 100).toFixed(2)}%`;
}

function ReturnCell({ value }: { value: number | null }) {
  if (value === null) return <span className="num text-muted">—</span>;
  const up = value >= 0;
  return (
    <span className={`num ${up ? "text-up" : "text-down"}`}>
      {up ? "▲" : "▼"} {(Math.abs(value) * 100).toFixed(2)}%
    </span>
  );
}

function RotationTable({ rows, emptyNote }: { rows: RotationRow[]; emptyNote: string }) {
  if (rows.length === 0) return <p className="text-muted text-[12px]">{emptyNote}</p>;
  return (
    <table className="w-full text-[12px]">
      <thead>
        <tr className="text-[10px] tracking-wider uppercase text-muted border-b border-hairline">
          <th className="text-left font-normal py-1">Symbol</th>
          <th className="text-right font-normal py-1">1D</th>
          <th className="text-right font-normal py-1">1M</th>
          <th className="text-right font-normal py-1">3M</th>
          <th className="text-right font-normal py-1">TradingView</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.symbol} className="border-b border-hairline last:border-0">
            <td className="num py-1.5 text-ink">
              <InstrumentHover symbol={row.symbol} change1d={row.ret_1d}>
                {row.symbol}
              </InstrumentHover>
            </td>
            <td className="text-right py-1.5">
              <ReturnCell value={row.ret_1d} />
            </td>
            <td className="text-right py-1.5">
              <ReturnCell value={row.ret_1m} />
            </td>
            <td className="text-right py-1.5">
              <ReturnCell value={row.ret_3m} />
            </td>
            <td className="text-right py-1.5">
              <a
                href={tradingViewUrl(row.symbol)}
                target="_blank"
                rel="noreferrer"
                className="text-muted hover:text-market text-[11px] underline underline-offset-2"
              >
                chart ↗
              </a>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function Macro() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["macro"],
    queryFn: getMacro,
    staleTime: 60 * 60 * 1000,
  });

  if (isLoading)
    return (
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-[1.4fr_1fr]">
        <Skeleton className="h-48" />
        <Skeleton className="h-48" />
        <Skeleton className="h-56 lg:col-span-2" />
      </div>
    );
  if (error) return <p className="text-down">Macro unavailable: {String((error as Error).message ?? error)}</p>;
  if (!data) return null;

  const asOfNote = data.as_of ? `as of ${data.as_of.slice(0, 10)}` : "no cached data";
  const missingNote = data.missing.length > 0 ? `missing: ${data.missing.join(", ")}` : undefined;

  return (
    <div className="w-full space-y-3">
      {missingNote && (
        <p className="text-warning text-[12px] num border border-warning/40 px-3 py-1.5">
          Some series aren't cached yet — {missingNote}. Sync to fill them in.
        </p>
      )}

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-[1.4fr_1fr]">
        <Panel title="Yields" note={asOfNote}>
          {data.yields ? (
            <>
              <div className="mb-3 grid grid-cols-2 gap-x-4 gap-y-2 lg:grid-cols-4">
                <div>
                  <div className="text-[10px] tracking-wider uppercase text-muted">US 10Y</div>
                  <div className="num text-lg">{pct(data.yields.us10y)}</div>
                </div>
                <div>
                  <div className="text-[10px] tracking-wider uppercase text-muted">US 2Y</div>
                  <div className="num text-lg">{pct(data.yields.us2y)}</div>
                </div>
                <div>
                  <div className="text-[10px] tracking-wider uppercase text-muted">US 3M</div>
                  <div className="num text-lg">{pct(data.yields.us3m)}</div>
                </div>
                <div>
                  <div className="text-[10px] tracking-wider uppercase text-muted">2s10s spread</div>
                  <div className="num text-lg">{signedPct(data.yields.spread_2s10s)}</div>
                </div>
              </div>
              <SeriesChart points={data.yields.series.us10y} label="US10Y" colorToken="market" />
            </>
          ) : (
            <p className="text-muted text-[12px]">No yields cached yet — sync US10Y/US2Y/US3M.</p>
          )}
        </Panel>

        <Panel
          title="Net liquidity"
          note={data.net_liquidity ? `${asOfNote} · weekly cadence` : asOfNote}
        >
          {data.net_liquidity ? (
            <>
              <div className="mb-3">
                <div className="text-[10px] tracking-wider uppercase text-muted">Latest ($bn)</div>
                <div className="num text-lg">
                  {data.net_liquidity.latest_bn === null ? "—" : data.net_liquidity.latest_bn.toFixed(1)}
                </div>
                <div className="text-muted text-[11px] mt-1">
                  Fed net liquidity — {data.net_liquidity.cadence_note} cadence, explains regimes not today's move.
                </div>
              </div>
              <SeriesChart points={data.net_liquidity.series} label="Net liquidity" colorToken="market" />
            </>
          ) : (
            <p className="text-muted text-[12px]">No net liquidity cached yet — sync NET_LIQUIDITY.</p>
          )}
        </Panel>
      </div>

      <Panel title="Sector rotation" note={asOfNote}>
        <RotationTable rows={data.sectors} emptyNote="No sector data cached yet." />
      </Panel>

      <Panel title="Factors" note={asOfNote}>
        <RotationTable rows={data.factors} emptyNote="No factor data cached yet." />
      </Panel>
    </div>
  );
}
