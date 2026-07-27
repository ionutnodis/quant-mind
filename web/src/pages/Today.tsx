// Today: the morning bench (FINDING-001 rebuild). Three zones per the approved
// workbench composition: regime + book vitals band, overnight strip ranked by
// impact, analytics row with the saved-models console. Renders from cache;
// staleness visible, never hidden. The book zone is honest about the empty
// paper book — structure present, amber reserved for when positions exist.
import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, request } from "../lib/api";
import { GlanceCharts } from "../components/GlanceCharts";
import { InstrumentHover } from "../components/InstrumentHover";
import { NewsTicker } from "../components/NewsTicker";
import { Panel, Skeleton } from "../components/Panel";
import { RotationHeatmap } from "../components/RotationHeatmap";

// Local, page-scoped types + calls for the sync job — api.ts is shared and not
// owned here, so these stay in Today.tsx per the wave-2 ownership rule.
interface SyncSubmitResponse {
  job_id: string;
}
interface SyncStatusResponse {
  state: "running" | "done" | "error" | "cancelled";
  result?: string | null;
  error?: string | null;
}
function postSync(): Promise<SyncSubmitResponse> {
  return request<SyncSubmitResponse>("/api/sync", { method: "POST" });
}
function getSyncStatus(jobId: string): Promise<SyncStatusResponse> {
  return request<SyncStatusResponse>(`/api/sync/${jobId}`);
}

const SYNC_POLL_MS = 2000;

// "Sync now" — lives in both the staleness banner and the empty-cache state.
// Submits the job, checks immediately, then polls every 2s while running;
// on completion the brief query is invalidated so the page picks up fresh
// data without a manual reload.
function SyncButton() {
  const queryClient = useQueryClient();
  const [submitting, setSubmitting] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const [status, setStatus] = useState<SyncStatusResponse | null>(null);

  const terminal = status?.state === "done" || status?.state === "error" || status?.state === "cancelled";
  const running = submitting || (jobId !== null && !terminal);

  useEffect(() => {
    if (!jobId || terminal) return;
    const id = setInterval(async () => {
      try {
        const s = await getSyncStatus(jobId);
        setStatus(s);
        if (s.state === "done") {
          queryClient.invalidateQueries({ queryKey: ["brief"] });
        }
      } catch {
        // transient poll failure — keep trying on the next tick
      }
    }, SYNC_POLL_MS);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId, terminal]);

  async function handleClick() {
    setSubmitting(true);
    setStatus(null);
    let submittedJobId: string | null = null;
    try {
      const { job_id } = await postSync();
      submittedJobId = job_id;
      setJobId(job_id);
      const s = await getSyncStatus(job_id);
      setStatus(s);
      if (s.state === "done") {
        queryClient.invalidateQueries({ queryKey: ["brief"] });
      }
    } catch (err) {
      if (submittedJobId === null) {
        // The POST itself failed — no job exists. Surface the error in the
        // status slot and clear any stale jobId so polling doesn't resume
        // against a previous run.
        setJobId(null);
        setStatus({ state: "error", error: err instanceof Error ? err.message : String(err) });
      }
      // else: submit succeeded but the immediate status check failed —
      // leave state as-is; the 2s poll loop owns retrying from here.
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <span className="inline-flex items-center gap-2 ml-2">
      <button
        type="button"
        data-testid="sync-now"
        onClick={handleClick}
        disabled={running}
        className="num text-[11px] border border-hairline px-2.5 py-1 text-ink hover:border-muted disabled:opacity-50 disabled:cursor-not-allowed"
      >
        {running ? "Syncing…" : "Sync now"}
      </button>
      {status?.state === "error" && (
        <span className="text-down text-[11px]">{status.error ?? "sync failed"}</span>
      )}
    </span>
  );
}

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

// Book vitals (2026-07-27: wired to the live portfolio — the panel was a
// static placeholder until the real account connected and proved it).
// Page-scoped types per the ownership rule; only the fields the vitals read.
interface VitalsResponse {
  valuation_ts: string | null;
  totals: { market_value: number | null; n_positions: number; unrealized_pnl: number | null } | null;
  totals_note: string | null;
  attribution: { available: boolean; beta: number | null; window_days: number | null } | null;
  options_sleeve: { available: boolean; reason: string | null } | null;
}
function getPortfolioVitals(): Promise<VitalsResponse> {
  return request<VitalsResponse>("/api/portfolio");
}

function money(x: number): string {
  const sign = x >= 0 ? "+" : "−";
  return `${sign}$${Math.abs(x).toFixed(2)}`;
}

function BookVitals() {
  const book = useQuery({
    queryKey: ["portfolio-vitals"],
    queryFn: getPortfolioVitals,
    staleTime: 60_000,
  });
  const totals = book.data?.totals ?? null;
  const attr = book.data?.attribution ?? null;
  const sleeve = book.data?.options_sleeve ?? null;
  const hasBook = (totals?.n_positions ?? 0) > 0;
  const pnl = hasBook ? totals?.unrealized_pnl ?? null : null;
  const beta = hasBook && attr?.available ? attr.beta : null;
  const asOf = book.data?.valuation_ts?.slice(0, 10);

  return (
    <Panel title="Your book" note={hasBook ? `live · as of ${asOf}` : "awaiting book"}>
      <div className="grid grid-cols-2 gap-x-4 gap-y-2">
        <div>
          <div className="text-[10px] tracking-wider uppercase text-muted">Est. open P&L</div>
          <div
            data-testid="vital-pnl"
            className={`num text-lg ${pnl !== null ? "text-you" : "text-muted"}`}
          >
            {pnl !== null ? money(pnl) : "—"}
          </div>
        </div>
        <div>
          <div className="text-[10px] tracking-wider uppercase text-muted">
            Beta ({beta !== null && attr?.window_days != null ? attr.window_days : 60}d)
          </div>
          <div
            data-testid="vital-beta"
            className={`num text-lg ${beta !== null ? "text-you" : "text-muted"}`}
          >
            {beta !== null ? beta.toFixed(2) : "—"}
          </div>
        </div>
        <div>
          <div className="text-[10px] tracking-wider uppercase text-muted">Expected shortfall</div>
          <div className="num text-lg text-muted">
            —<span className="text-[10px] ml-1">97.5%</span>
          </div>
        </div>
        <div>
          <div className="text-[10px] tracking-wider uppercase text-muted">Vega</div>
          <div className="num text-lg text-muted" title={sleeve?.reason ?? undefined}>
            —<span className="text-[10px] ml-1">/vol pt</span>
          </div>
        </div>
      </div>
      <p className="text-[11px] text-muted mt-2 border-t border-hairline pt-2">
        {hasBook ? (
          <>
            {totals!.n_positions} positions
            {totals!.market_value != null && (
              <>
                {" · $"}
                {totals!.market_value.toLocaleString("en-US", { maximumFractionDigits: 0 })}
                {" market value"}
              </>
            )}
            {" — ES & vega compute in Hedge / What-If"}
            {sleeve && !sleeve.available && sleeve.reason ? <>; {sleeve.reason}</> : null}.
            {book.data?.totals_note ? (
              <span className="text-warning"> {book.data.totals_note}.</span>
            ) : null}
          </>
        ) : (
          <>
            No positions yet — vitals light up in <span className="text-you">amber</span> when the
            book connects.
          </>
        )}
      </p>
    </Panel>
  );
}

export function Today() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["brief"],
    queryFn: api.brief,
    staleTime: 60 * 60 * 1000,
  });
  const models = useQuery({ queryKey: ["models"], queryFn: api.models, staleTime: Infinity });
  // RotationHeatmap owns its own fetch; it reports its data's as-of upward
  // so the Panel note can carry the stamp (DESIGN.md: every data panel — F7).
  const [rotationAsOf, setRotationAsOf] = useState<string | null>(null);

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
        <div className="mt-3">
          <SyncButton />
        </div>
      </Panel>
    );

  const days = staleDays(data.as_of);
  const ranked = [...data.tiles].sort((a, b) => Math.abs(b.change_1d) - Math.abs(a.change_1d));
  const asOfNote = `as of ${data.as_of?.slice(0, 10)}`;

  return (
    <div className="space-y-3 max-w-[1400px]">
      {days !== null && days > 3 && (
        <p
          data-testid="staleness"
          className="text-warning text-[12px] num border border-warning/40 px-3 py-1.5 flex items-center"
        >
          Data is {days} days old ({asOfNote}) — run the sync.
          <SyncButton />
        </p>
      )}

      {/* Zone A — regime + book vitals */}
      <div className="grid grid-cols-[1.4fr_1fr] gap-3">
        <Panel title="Regime" note={asOfNote}>
          <p className="text-[20px] leading-snug font-medium max-w-[34ch]">
            {regimeLine(data.tiles)}
          </p>
          {/* Dynamic in place of a static blurb (user direction): scroll
              through relevant headline news right under the regime call. */}
          <div className="mt-2 border-t border-hairline pt-2">
            <NewsTicker />
          </div>
        </Panel>
        <BookVitals />
      </div>

      {/* Zone A2 — glance charts: major indices, VIX, oil, gold, 2s10s */}
      <Panel title="At a glance" note="90d · indices · vol · commodities · curve">
        <GlanceCharts />
      </Panel>

      {/* Zone B — overnight strip, ranked */}
      <Panel
        title="Overnight — ranked by move"
        note="book-impact ranking arrives with positions"
      >
        <div className="grid grid-cols-11 gap-px bg-hairline -m-3">
          {ranked.map((t) => (
            <div key={t.symbol} className="bg-surface px-2.5 py-2">
              <div className="text-[10px] tracking-wider text-muted">
                <InstrumentHover symbol={t.symbol} change1d={t.change_1d}>
                  {t.symbol}
                </InstrumentHover>
              </div>
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
        <Panel
          title="Rotation"
          note={`${rotationAsOf ? `as of ${rotationAsOf.slice(0, 10)} · ` : ""}click a mover for the other side of the trade`}
        >
          <RotationHeatmap onAsOf={setRotationAsOf} />
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
