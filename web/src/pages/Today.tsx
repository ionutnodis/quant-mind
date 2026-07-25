// Today: the morning bench (FINDING-001 rebuild). Three zones per the approved
// workbench composition: regime + book vitals band, overnight strip ranked by
// impact, analytics row with the saved-models console. Renders from cache;
// staleness visible, never hidden. The book zone is honest about the empty
// paper book — structure present, amber reserved for when positions exist.
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { CorrelationHeatmap } from "../components/CorrelationHeatmap";
import { Panel, Skeleton } from "../components/Panel";

function staleDays(asOf: string | null): number | null {
  if (!asOf) return null;
  return Math.floor((Date.now() - new Date(asOf).getTime()) / 86_400_000);
}

function regimeLine(tiles: { symbol: string; change_1d: number }[]): string {
  const up = tiles.filter((t) => t.change_1d >= 0).length;
  const lead = [...tiles].sort((a, b) => Math.abs(b.change_1d) - Math.abs(a.change_1d))[0];
  const dir = lead.change_1d >= 0 ? "up" : "down";
  const tape = up > tiles.length * 0.6 ? "Risk-on tape" : up < tiles.length * 0.4 ? "Defensive tape" : "Mixed tape";
  return `${tape} — ${lead.symbol} leads, ${dir} ${(Math.abs(lead.change_1d) * 100).toFixed(2)}%.`;
}

const VITALS = [
  { label: "Est. open P&L", unit: "" },
  { label: "Beta (60d)", unit: "" },
  { label: "Expected shortfall", unit: "97.5%" },
  { label: "Vega", unit: "/vol pt" },
];

export function Today() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["brief"],
    queryFn: api.brief,
    staleTime: 60 * 60 * 1000,
  });
  const models = useQuery({ queryKey: ["models"], queryFn: api.models, staleTime: Infinity });

  if (isLoading)
    return (
      <div className="grid grid-cols-[1.4fr_1fr] gap-3">
        <Skeleton className="h-36" />
        <Skeleton className="h-36" />
        <Skeleton className="h-28 col-span-2" />
      </div>
    );
  if (error) return <p className="text-down">Brief unavailable: {String(error)}</p>;
  if (!data || data.tiles.length === 0)
    return (
      <Panel title="Cache empty">
        <p className="text-muted">
          No market data cached yet. With IB Gateway running, sync the universe:
        </p>
        <code className="num text-ink block mt-2">uv run python -m quantmind.sync_cli</code>
      </Panel>
    );

  const days = staleDays(data.as_of);
  const ranked = [...data.tiles].sort((a, b) => Math.abs(b.change_1d) - Math.abs(a.change_1d));
  const asOfNote = `as of ${data.as_of?.slice(0, 10)}`;

  return (
    <div className="space-y-3 max-w-[1400px]">
      {days !== null && days > 3 && (
        <p data-testid="staleness" className="text-warning text-[12px] num border border-warning/40 px-3 py-1.5">
          Data is {days} days old ({asOfNote}) — run the sync.
        </p>
      )}

      {/* Zone A — regime + book vitals */}
      <div className="grid grid-cols-[1.4fr_1fr] gap-3">
        <Panel title="Regime" note={asOfNote}>
          <p className="text-[20px] leading-snug font-medium max-w-[34ch]">
            {regimeLine(data.tiles)}
          </p>
          <p className="text-muted mt-2 text-[12px]">
            Derived from the cached universe. Portfolio-weighted regime lands with your positions.
          </p>
        </Panel>
        <Panel title="Your book" note="paper account">
          <div className="grid grid-cols-2 gap-x-4 gap-y-2">
            {VITALS.map((v) => (
              <div key={v.label}>
                <div className="text-[10px] tracking-wider uppercase text-muted">{v.label}</div>
                <div className="num text-lg text-muted">
                  —<span className="text-[10px] ml-1">{v.unit}</span>
                </div>
              </div>
            ))}
          </div>
          <p className="text-[11px] text-muted mt-2 border-t border-hairline pt-2">
            No positions yet — vitals light up in <span className="text-you">amber</span> when the
            book connects.
          </p>
        </Panel>
      </div>

      {/* Zone B — overnight strip, ranked */}
      <Panel
        title="Overnight — ranked by move"
        note="book-impact ranking arrives with positions"
      >
        <div className="grid grid-cols-11 gap-px bg-hairline -m-3">
          {ranked.map((t) => (
            <div key={t.symbol} className="bg-surface px-2.5 py-2">
              <div className="text-[10px] tracking-wider text-muted">{t.symbol}</div>
              <div className="num text-[14px]">{t.last_close.toFixed(2)}</div>
              <div className={`num text-[12px] ${t.change_1d >= 0 ? "text-up" : "text-down"}`}>
                {t.change_1d >= 0 ? "▲" : "▼"} {(Math.abs(t.change_1d) * 100).toFixed(2)}%
              </div>
            </div>
          ))}
        </div>
      </Panel>

      {/* Zone C — analytics + console */}
      <div className="grid grid-cols-[1fr_1.2fr] gap-3">
        <Panel title="Benchmark tail risk" note="daily ES 97.5% · 5y">
          {data.benchmark_es !== null && (
            <div className="num text-[26px]">{(data.benchmark_es * 100).toFixed(2)}%</div>
          )}
          <p className="text-muted text-[12px] mt-1 max-w-[40ch]">
            Average SPY loss in the worst 2.5% of days. Your book's ES replaces this anchor once
            positions exist.
          </p>
        </Panel>
        <Panel title="Correlation" note="daily · 5y · union calendar">
          {data.correlation && <CorrelationHeatmap data={data.correlation} />}
        </Panel>
      </div>

      {/* Saved models console — the garage shelf */}
      <Panel title="Models on the bench" note="Lab ⌘K → lab">
        <div className="flex gap-2 flex-wrap">
          {(models.data ?? []).map((m) => (
            <span
              key={m.name}
              className="num text-[12px] border border-hairline px-2.5 py-1 text-ink"
            >
              {m.label ?? m.name}
              <span className="text-muted ml-2">registered</span>
            </span>
          ))}
          {models.data && models.data.length === 0 && (
            <span className="text-muted text-[12px]">No models registered.</span>
          )}
        </div>
      </Panel>
    </div>
  );
}
