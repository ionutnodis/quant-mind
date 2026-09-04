import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, type SetupStatus } from "../lib/api";
import { Panel, Skeleton } from "../components/Panel";
import { SyncButton } from "../components/SyncButton";
import { pinCurrentBook, type BookSnapshotOut } from "../lib/book";

const NEXT_ACTION: Record<SetupStatus["next_action"], { title: string; body: string }> = {
  configure_account: {
    title: "Select one IBKR account",
    body: "Set QM_ACCOUNT_ID in .env to the one account this workspace may analyse, then restart QuantMind.",
  },
  start_gateway: {
    title: "Start IBKR Gateway or TWS",
    body: "Enable API connections and match QM_PORT to Gateway (4002 paper / 4001 live) or TWS (7497 paper / 7496 live), then restart QuantMind.",
  },
  wait_for_gateway: {
    title: "Wait for the broker connection",
    body: "QuantMind is establishing its read-only IBKR session.",
  },
  sync_market_data: {
    title: "Sync the market cache",
    body: "Load adjusted daily bars and macro evidence before running portfolio risk.",
  },
  sync_option_data: {
    title: "Sync the held option contracts",
    body: "Load fresh quotes and implied volatility for every held option, including weeklies, LEAPS, and far strikes.",
  },
  pin_book: {
    title: "Pin the current IBKR book",
    body: "Create an immutable portfolio reference so Risk, What-If, and Hedge Lab analyse the same positions.",
  },
  resolve_currency: {
    title: "FX normalization required",
    body: "This alpha refuses to combine local-currency prices as though they were USD. Use a USD-only book for the first acceptance pass; dated FX normalization is the next portfolio capability.",
  },
  resolve_instruments: {
    title: "Unsupported instruments are present",
    body: "This release analyses stocks, ETFs, and equity options only. Split futures, bonds, CFDs, cash, and other unsupported contracts out of the acceptance book before continuing.",
  },
  ready: {
    title: "The workbench is ready",
    body: "Broker, market evidence, and a pinned book are available for analysis.",
  },
};

function statusLabel(status: string): string {
  return status.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
}

const STATUS_PRESENTATION: Record<string, { glyph: string; tone: string }> = {
  ready: { glyph: "●", tone: "text-up" },
  connected: { glyph: "●", tone: "text-up" },
  connecting: { glyph: "▲", tone: "text-warning" },
  stale: { glyph: "▲", tone: "text-warning" },
  incomplete: { glyph: "▲", tone: "text-warning" },
  partial: { glyph: "▲", tone: "text-warning" },
  missing: { glyph: "×", tone: "text-down" },
  not_required: { glyph: "◇", tone: "text-market" },
  unavailable: { glyph: "×", tone: "text-down" },
  unsupported: { glyph: "×", tone: "text-down" },
  empty: { glyph: "◇", tone: "text-market" },
  not_pinned: { glyph: "◇", tone: "text-market" },
};

function StatusCard({
  label,
  status,
  detail,
  ariaLabel,
}: {
  label: string;
  status: string;
  detail: string;
  ariaLabel: string;
}) {
  const presentation = STATUS_PRESENTATION[status] ?? {
    glyph: "◇",
    tone: "text-market",
  };
  return (
    <section aria-label={ariaLabel} className="border border-hairline bg-surface p-3 min-w-0">
      <div className="text-[12px] uppercase tracking-[0.14em] text-muted md:text-[10px]">{label}</div>
      <div className={`num mt-2 flex items-center gap-2 text-base ${presentation.tone}`}>
        <span data-testid="status-glyph" aria-hidden="true">{presentation.glyph}</span>
        <span>{statusLabel(status)}</span>
      </div>
      <p className="mt-1 text-[13px] leading-relaxed text-muted break-words md:text-[11px]">{detail}</p>
    </section>
  );
}

export function Setup() {
  const [syncResult, setSyncResult] = useState<string | null>(null);
  const [pinnedBook, setPinnedBook] = useState<BookSnapshotOut | null>(null);
  const queryClient = useQueryClient();
  const { data, isLoading, error } = useQuery({
    queryKey: ["setup-status"],
    queryFn: api.setupStatus,
    // Broker connectivity can change after a successful first load (for
    // example when Gateway restarts). The endpoint only reads lightweight
    // watermarks and local metadata, so a slow heartbeat keeps the safety
    // state honest without scanning full datasets.
    refetchInterval: 15_000,
  });
  const pinBook = useMutation({
    mutationFn: pinCurrentBook,
    onSuccess: async (snapshot) => {
      setPinnedBook(snapshot);
      await queryClient.invalidateQueries({ queryKey: ["setup-status"] });
    },
  });

  if (isLoading) {
    return (
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-6">
        <Skeleton className="h-28" />
        <Skeleton className="h-28" />
        <Skeleton className="h-28" />
        <Skeleton className="h-28" />
        <Skeleton className="h-28" />
        <Skeleton className="h-28" />
      </div>
    );
  }
  if (error || !data) return <p className="text-down">Setup unavailable: {String(error)}</p>;

  const action =
    data.next_action === "pin_book" && data.book.status === "stale"
      ? {
          title: "Refresh the pinned book",
          body: "The latest snapshot is empty, out of date, or belongs to a different broker scope. Pin the current IBKR book before analysis.",
        }
      : NEXT_ACTION[data.next_action];
  const brokerDetail = [data.broker.provider, data.broker.mode, data.broker.error]
    .filter(Boolean)
    .join(" · ");
  const marketDetail = data.market_data.as_of
    ? `${data.market_data.ready_symbols ?? data.market_data.symbols}/${data.market_data.symbols} symbols ready · ${data.market_data.series} macro series · weakest as of ${data.market_data.as_of}${(data.market_data.missing_symbols ?? []).length ? ` · missing ${data.market_data.missing_symbols.join(", ")}` : ""}${(data.market_data.stale_symbols ?? []).length ? ` · stale ${data.market_data.stale_symbols.join(", ")}` : ""}${(data.market_data.corrupt_symbols ?? []).length ? ` · corrupt ${data.market_data.corrupt_symbols.join(", ")}` : ""}`
    : "No adjusted daily bars are cached yet.";
  const bookDetail = data.book.latest_snapshot_id
    ? `${data.book.snapshot_count} snapshot${data.book.snapshot_count === 1 ? "" : "s"} · ${data.book.option_positions} option position${data.book.option_positions === 1 ? "" : "s"} · latest ${data.book.latest_snapshot_id}${(data.book.unsupported_currencies ?? []).length ? ` · unsupported currencies ${data.book.unsupported_currencies.join(", ")}` : ""}${(data.book.unsupported_security_types ?? []).length ? ` · unsupported instruments ${data.book.unsupported_security_types.join(", ")}` : ""}${data.book.reason ? ` · ${data.book.reason.replaceAll("_", " ")}` : ""}`
    : "No immutable portfolio snapshot has been created.";
  const macroDetail = data.macro_data.as_of
    ? `${data.macro_data.ready_series}/${data.macro_data.required_series} series ready · weakest as of ${data.macro_data.as_of}${data.macro_data.missing_series.length ? ` · missing ${data.macro_data.missing_series.join(", ")}` : ""}${data.macro_data.stale_series.length ? ` · stale ${data.macro_data.stale_series.join(", ")}` : ""}${data.macro_data.corrupt_series.length ? ` · corrupt ${data.macro_data.corrupt_series.join(", ")}` : ""}`
    : `No macro evidence cached · missing ${data.macro_data.missing_series.join(", ")}`;
  const optionsDetail = data.options_data.total_positions === 0
    ? "No held option positions require chain evidence."
    : `${data.options_data.priced_positions}/${data.options_data.total_positions} held contracts priced${data.options_data.chain_as_of ? ` · weakest chain ${data.options_data.chain_as_of}` : ""}${data.options_data.missing_contracts.length ? ` · missing ${data.options_data.missing_contracts.join(", ")}` : ""}${data.options_data.stale_chains.length ? ` · stale ${data.options_data.stale_chains.join(", ")}` : ""}`;
  const activeBookRef = data.overall === "ready"
    ? pinnedBook?.snapshot_id ?? data.book.latest_snapshot_id
    : null;
  const activeOptionPositions = pinnedBook
    ? pinnedBook.positions.filter((position) => position.sec_type === "OPT").length
    : data.book.option_positions;

  return (
    <div className="w-full space-y-4">
      <header className="flex flex-col gap-2 border-b border-hairline pb-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <div className="text-[12px] uppercase tracking-[0.18em] text-muted md:text-[10px]">First run</div>
          <h1 className="mt-1 text-2xl font-medium">Finish local setup</h1>
          <p className="mt-1 max-w-[68ch] text-base text-muted md:text-[12px]">
            QuantMind stays read-only. These checks confirm the evidence chain before analysis.
          </p>
        </div>
        <span
          data-testid="overall-status"
          className={`num text-[13px] md:text-[11px] ${data.overall === "ready" ? "text-up" : "text-warning"}`}
        >
          {data.overall === "ready" ? "● READY" : "▲ ACTION REQUIRED"} · v{data.api.version}
        </span>
      </header>

      <Panel title="Next action" note={`${data.next_action.replaceAll("_", " ")}`}>
        <h2 className="text-lg font-medium">{action.title}</h2>
        <p className="mt-1 max-w-[70ch] text-base leading-relaxed text-muted md:text-[12px]">{action.body}</p>
        {(["sync_market_data", "sync_option_data", "pin_book"] as string[]).includes(data.next_action) && (
          <p className="companion-only mt-3 text-[13px] leading-relaxed text-market">
            Open QuantMind on a screen at least 768 × 600 to run setup actions.
          </p>
        )}
        {(data.next_action === "sync_market_data" || data.next_action === "sync_option_data") && (
          <div className="authoring-only mt-3">
            <SyncButton
              label={data.next_action === "sync_option_data" ? "Sync option data" : "Sync market data"}
              onCompleted={setSyncResult}
            />
          </div>
        )}
        {data.next_action === "pin_book" && (
          <div className="authoring-only mt-3 min-w-0 flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => pinBook.mutate()}
              disabled={pinBook.isPending}
              className="qm-target num border border-hairline px-3 py-1.5 text-[11px] text-ink hover:border-market hover:bg-elevated disabled:cursor-not-allowed disabled:opacity-50"
            >
              {pinBook.isPending ? "Pinning…" : "Pin current book"}
            </button>
            {pinBook.error && (
              <span className="text-[11px] text-down">{String(pinBook.error)}</span>
            )}
          </div>
        )}
        {syncResult && (
          <p className={`mt-3 whitespace-pre-line text-[11px] ${syncResult.includes("SYNC_RESULT: partial") ? "text-warning" : "text-up"}`}>
            {syncResult}
          </p>
        )}
        {pinnedBook && (
          <p className="num mt-3 text-[11px] text-up">Book pinned · {pinnedBook.snapshot_id}</p>
        )}
      </Panel>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-6">
        <StatusCard
          ariaLabel="API status"
          label="Local API"
          status={data.api.status}
          detail={`FastAPI ${data.api.version} · loopback only`}
        />
        <StatusCard
          ariaLabel="IBKR Gateway status"
          label="IBKR Gateway"
          status={data.broker.status}
          detail={brokerDetail || "No broker session is active."}
        />
        <StatusCard
          ariaLabel="Market cache status"
          label="Market cache"
          status={data.market_data.status}
          detail={marketDetail}
        />
        <StatusCard
          ariaLabel="Macro evidence status"
          label="Macro evidence"
          status={data.macro_data.status}
          detail={macroDetail}
        />
        <StatusCard
          ariaLabel="Held option evidence status"
          label="Held options"
          status={data.options_data.status}
          detail={optionsDetail}
        />
        <StatusCard
          ariaLabel="Current book status"
          label="Current book"
          status={data.book.status}
          detail={bookDetail}
        />
      </div>

      {activeBookRef && (
        <Panel title="Begin analysis" note={`book ${activeBookRef}`}>
          {activeOptionPositions > 0 && (
            <p className="mb-3 max-w-[76ch] text-[13px] leading-relaxed text-warning md:text-[11px]">
              {activeOptionPositions} option position{activeOptionPositions === 1 ? " is" : "s are"} preserved in this snapshot. Portfolio can price the cached options sleeve; What-If and Hedge Lab remain equity-only and are withheld for this book.
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            {[
              ["Open Portfolio", "/portfolio"],
              ...(activeOptionPositions === 0
                ? [["Open What-If", "/whatif"], ["Open Hedge Lab", "/hedge"]]
                : []),
            ].map(([label, path]) => (
              <a
                key={path}
                href={`${path}?book_ref=${activeBookRef}`}
                className="qm-target num border border-hairline px-3 py-1.5 text-[13px] text-ink hover:border-muted md:text-[11px]"
              >
                {label}
              </a>
            ))}
            {data.broker.status === "connected" && (
              <button
                type="button"
                onClick={() => pinBook.mutate()}
                disabled={pinBook.isPending}
                className="authoring-only qm-target num min-w-0 items-center border border-hairline px-3 py-1.5 text-[11px] text-muted hover:border-market hover:text-ink disabled:cursor-not-allowed disabled:opacity-50"
              >
                {pinBook.isPending ? "Refreshing…" : "Refresh current book"}
              </button>
            )}
          </div>
          {pinBook.error && (
            <p className="mt-3 text-[11px] text-down">{String(pinBook.error)}</p>
          )}
        </Panel>
      )}
    </div>
  );
}
