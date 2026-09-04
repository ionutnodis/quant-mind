import { useCallback, useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { request } from "../lib/api";

interface SyncSubmitResponse {
  job_id: string;
}

interface SyncStatusResponse {
  state: "running" | "done" | "error" | "cancelled";
  result?: string | null;
  error?: string | null;
}

const SYNC_POLL_MS = 2_000;
const MAX_POLL_FAILURES = 3;

function postSync(): Promise<SyncSubmitResponse> {
  return request<SyncSubmitResponse>("/api/sync", { method: "POST" });
}

function getSyncStatus(jobId: string): Promise<SyncStatusResponse> {
  return request<SyncStatusResponse>(`/api/sync/${jobId}`);
}

function compactResult(result: string | null | undefined): string | null {
  return result?.trim() || null;
}

export function SyncButton({
  label = "Sync now",
  onCompleted,
}: {
  label?: string;
  onCompleted?: (result: string | null) => void;
}) {
  const queryClient = useQueryClient();
  const [submitting, setSubmitting] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const [status, setStatus] = useState<SyncStatusResponse | null>(null);
  const activeJobId = useRef<string | null>(null);
  const completedJob = useRef<string | null>(null);

  const terminal =
    status?.state === "done" || status?.state === "error" || status?.state === "cancelled";
  const running = submitting || (jobId !== null && !terminal);
  const result = compactResult(status?.result);
  const partial = result?.includes("SYNC_RESULT: partial") ?? false;

  const refreshReadiness = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ["brief"] });
    queryClient.invalidateQueries({ queryKey: ["setup-status"] });
  }, [queryClient]);

  const applyStatus = useCallback(
    (nextStatus: SyncStatusResponse, statusJobId: string) => {
      if (activeJobId.current !== statusJobId) return;
      setStatus(nextStatus);
      const isTerminal = ["done", "error", "cancelled"].includes(nextStatus.state);
      if (isTerminal && completedJob.current !== statusJobId) {
        completedJob.current = statusJobId;
        refreshReadiness();
        if (nextStatus.state === "done") {
          onCompleted?.(compactResult(nextStatus.result));
        }
      }
    },
    [onCompleted, refreshReadiness],
  );

  useEffect(() => {
    if (!jobId || terminal) return;
    let cancelled = false;
    let timeoutId: ReturnType<typeof setTimeout> | null = null;
    let failures = 0;

    const poll = async () => {
      try {
        const nextStatus = await getSyncStatus(jobId);
        if (cancelled) return;
        failures = 0;
        applyStatus(nextStatus, jobId);
        if (nextStatus.state === "running") {
          timeoutId = setTimeout(poll, SYNC_POLL_MS);
        }
      } catch (error) {
        if (cancelled) return;
        failures += 1;
        if (failures >= MAX_POLL_FAILURES) {
          applyStatus(
            {
              state: "error",
              error: `Sync status unavailable after ${MAX_POLL_FAILURES} attempts: ${error instanceof Error ? error.message : String(error)}`,
            },
            jobId,
          );
        } else {
          timeoutId = setTimeout(poll, SYNC_POLL_MS);
        }
      }
    };

    void poll();
    return () => {
      cancelled = true;
      if (timeoutId !== null) clearTimeout(timeoutId);
    };
  }, [applyStatus, jobId, terminal]);

  async function handleClick() {
    setSubmitting(true);
    activeJobId.current = null;
    setJobId(null);
    setStatus(null);
    completedJob.current = null;
    try {
      const { job_id } = await postSync();
      activeJobId.current = job_id;
      setJobId(job_id);
    } catch (error) {
      activeJobId.current = null;
      setJobId(null);
      setStatus({
        state: "error",
        error: error instanceof Error ? error.message : String(error),
      });
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <span className="inline-flex min-w-0 flex-wrap items-center gap-2">
      <button
        type="button"
        data-testid="sync-now"
        onClick={handleClick}
        disabled={running}
        className="qm-target num border border-hairline px-3 py-1.5 text-[11px] text-ink hover:border-muted disabled:cursor-not-allowed disabled:opacity-50"
      >
        {running ? "Syncing…" : label}
      </button>
      {status?.state === "done" && result && (
        <span className={`whitespace-pre-line text-[11px] ${partial ? "text-warning" : "text-up"}`}>{result}</span>
      )}
      {status?.state === "error" && (
        <span className="text-[11px] text-down">{status.error ?? "sync failed"}</span>
      )}
      {status?.state === "cancelled" && (
        <span className="text-[11px] text-warning">Sync cancelled</span>
      )}
    </span>
  );
}
