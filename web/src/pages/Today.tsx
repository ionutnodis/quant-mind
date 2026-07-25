// Today: the morning entry point. Renders exclusively from the cached brief;
// staleness is shown, never hidden (DESIGN.md / staleness policy).
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { CorrelationHeatmap } from "../components/CorrelationHeatmap";

function staleDays(asOf: string | null): number | null {
  if (!asOf) return null;
  return Math.floor((Date.now() - new Date(asOf).getTime()) / 86_400_000);
}

export function Today() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["brief"],
    queryFn: api.brief,
    staleTime: 60 * 60 * 1000, // data changes once a day (decision 11A)
  });

  if (isLoading) return <p className="text-muted">Loading brief…</p>;
  if (error) return <p className="text-down">Brief unavailable: {String(error)}</p>;
  if (!data || data.tiles.length === 0)
    return (
      <div className="text-muted">
        <p>Cache is empty.</p>
        <p className="mt-1">
          Run <code className="num text-ink">uv run python -m quantmind.sync_cli</code> with IB
          Gateway up, then reload.
        </p>
      </div>
    );

  const days = staleDays(data.as_of);
  return (
    <div>
      <div className="flex items-baseline justify-between">
        <h1 className="text-xl font-semibold">Today</h1>
        <span className="text-[11px] num text-muted" data-testid="asof">
          as of {data.as_of?.slice(0, 10)}
        </span>
      </div>
      {days !== null && days > 3 && (
        <p data-testid="staleness" className="mt-2 text-down text-[12px]">
          Data is {days} days old — run the sync.
        </p>
      )}

      <div className="mt-4 grid grid-cols-6 gap-px bg-hairline border border-hairline">
        {[...data.tiles]
          .sort((a, b) => a.symbol.localeCompare(b.symbol))
          .map((t) => (
            <div key={t.symbol} className="bg-surface px-3 py-2.5">
              <div className="text-[10px] tracking-wider text-muted">{t.symbol}</div>
              <div className="num text-lg">{t.last_close.toFixed(2)}</div>
              <div className={`num text-[12px] ${t.change_1d >= 0 ? "text-up" : "text-down"}`}>
                {t.change_1d >= 0 ? "▲" : "▼"} {(Math.abs(t.change_1d) * 100).toFixed(2)}%
              </div>
            </div>
          ))}
      </div>

      <div className="mt-6 grid grid-cols-2 gap-8">
        <div>
          <h2 className="text-[11px] tracking-widest text-muted uppercase mb-2">
            Benchmark tail risk
          </h2>
          {data.benchmark_es !== null && (
            <div className="num text-2xl">
              {(data.benchmark_es * 100).toFixed(2)}%
              <span className="text-muted text-[11px] ml-2">daily ES 97.5% · 5y</span>
            </div>
          )}
        </div>
        <div>
          <h2 className="text-[11px] tracking-widest text-muted uppercase mb-2">
            Correlation — daily, 5y
          </h2>
          {data.correlation && <CorrelationHeatmap data={data.correlation} />}
        </div>
      </div>
    </div>
  );
}
