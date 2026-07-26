// NewsTicker: auto-scrolling headline strip under Today's regime line
// (wave-3B Today task — "the Regime box must be dynamic: after the regime
// statement, scroll through relevant headline news instead of a static
// section"). Reads GET /api/news, which is a LIVE broker read (news.py's
// own module docstring — unlike most GET routers, there's no meaningful
// "cached" news), so a Gateway-down account gets the same honest empty
// state as everywhere else, never a crash. Pauses on hover so a headline
// can actually be read; click-through to the source when a `url` is
// present. Instrument identity law (DESIGN.md): every matched symbol
// renders through InstrumentHover, never a bare span.
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { request } from "../lib/api";
import { InstrumentHover } from "./InstrumentHover";

export interface NewsItem {
  time: string;
  source: string;
  headline: string;
  symbol?: string | null;
  url?: string | null;
}

interface NewsResponse {
  items: NewsItem[];
  as_of: string | null;
  note: string | null;
}

function getNews(): Promise<NewsResponse> {
  return request<NewsResponse>("/api/news");
}

const POLL_MS = 5 * 60 * 1000;
const TRACK_HEIGHT = 96;
const SCROLL_SECONDS = 22;
const DEFAULT_EMPTY_NOTE = "news source unavailable — Gateway down or no entitled providers";

function hhmm(iso: string): string {
  return iso.length >= 16 ? `${iso.slice(11, 16)}Z` : iso;
}

function NewsRow({ item }: { item: NewsItem }) {
  const body = (
    <>
      <span className="num text-muted text-[10px] shrink-0 w-10">{hhmm(item.time)}</span>
      <span className="text-muted text-[9px] uppercase tracking-wider shrink-0 w-12 truncate">
        {item.source}
      </span>
      {item.symbol && (
        <span className="shrink-0" onClick={(e) => e.stopPropagation()}>
          <InstrumentHover symbol={item.symbol}>
            <span className="num text-market text-[11px]">{item.symbol}</span>
          </InstrumentHover>
        </span>
      )}
      <span className="text-ink text-[12px] truncate">{item.headline}</span>
    </>
  );

  const rowClass = "flex items-center gap-2 py-1.5 border-b border-hairline/60 last:border-b-0";

  if (item.url) {
    return (
      <a
        href={item.url}
        target="_blank"
        rel="noreferrer"
        data-testid="news-item"
        className={`${rowClass} hover:bg-elevated`}
      >
        {body}
      </a>
    );
  }
  return (
    <div data-testid="news-item" className={rowClass}>
      {body}
    </div>
  );
}

export function NewsTicker() {
  const [paused, setPaused] = useState(false);
  const { data, isLoading } = useQuery({
    queryKey: ["news"],
    queryFn: getNews,
    staleTime: 60 * 1000,
    refetchInterval: POLL_MS,
    retry: false,
  });

  if (isLoading) {
    return <p className="text-muted text-[11px]">Loading headlines…</p>;
  }

  if (!data || data.items.length === 0) {
    return (
      <p data-testid="news-empty" className="text-muted text-[11px]">
        {data?.note ?? DEFAULT_EMPTY_NOTE}
      </p>
    );
  }

  return (
    <div
      data-testid="news-ticker"
      className="relative overflow-hidden"
      style={{ height: TRACK_HEIGHT }}
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
    >
      {/* Scoped keyframes — no global CSS file is owned by this task, so the
          loop animation is declared inline rather than added to a shared
          stylesheet. -50% because the track below renders the item list
          twice back-to-back (seamless loop). */}
      <style>{`
        @keyframes qm-news-scroll {
          0% { transform: translateY(0); }
          100% { transform: translateY(-50%); }
        }
      `}</style>
      <div
        data-testid="news-ticker-track"
        className="absolute inset-x-0 top-0"
        style={{
          animationName: "qm-news-scroll",
          animationDuration: `${SCROLL_SECONDS}s`,
          animationTimingFunction: "linear",
          animationIterationCount: "infinite",
          animationPlayState: paused ? "paused" : "running",
        }}
      >
        {data.items.map((item, i) => (
          <NewsRow key={`a-${item.time}-${i}`} item={item} />
        ))}
        {data.items.map((item, i) => (
          <NewsRow key={`b-${item.time}-${i}`} item={item} />
        ))}
      </div>
    </div>
  );
}
