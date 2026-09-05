import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { request } from "../lib/api";
import type { components } from "../lib/api-types";
import { readActiveBookRef, writeActiveBookRef } from "../lib/book";
import "./world.css";

// FastAPI includes model defaults in responses; OpenAPI marks them optional
// because the same schema also accepts partial/defaulted input profiles.
type Profile = Required<components["schemas"]["WorldProfile"]>;
type WorldResponse = Omit<components["schemas"]["WorldResponse"], "items" | "profile"> & {
  profile: Profile;
  items: Required<components["schemas"]["RankedEvent"]>[];
};
type RefreshResult = components["schemas"]["WorldRefreshResult"];

const csv = (value: string, upper = false) => [...new Set(value.split(",").map((v) => v.trim()).filter(Boolean).map((v) => upper ? v.toUpperCase() : v))];
const dateTime = (value: string | null) => { if (!value) return "Never"; const parsed = new Date(value); return Number.isNaN(parsed.getTime()) ? "Invalid time" : new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(parsed); };
const safeUrl = (value: string) => { try { const url = new URL(value); return url.protocol === "http:" || url.protocol === "https:" ? value : null; } catch { return null; } };

function getWorld(bookRef: string | null) {
  const suffix = bookRef ? `?book_ref=${encodeURIComponent(bookRef)}` : "";
  return request<WorldResponse>(`/api/world${suffix}`);
}

export function World() {
  const queryClient = useQueryClient();
  const [bookRef, setBookRef] = useState(() => readActiveBookRef());
  const [bookDraft, setBookDraft] = useState(() => readActiveBookRef() ?? "");
  const [search, setSearch] = useState("");
  const [lensOnly, setLensOnly] = useState(false);
  const [topic, setTopic] = useState("all");
  const [source, setSource] = useState("all");
  const [draft, setDraft] = useState({ watch_symbols: "", interests: "", regions: "" });
  const [draftDirty, setDraftDirty] = useState(false);
  const [saved, setSaved] = useState(false);
  const [validationError, setValidationError] = useState<string | null>(null);

  const world = useQuery({ queryKey: ["world", bookRef], queryFn: () => getWorld(bookRef), retry: false, refetchInterval: (query) => query.state.data?.refreshing ? 2000 : 30_000 });
  useEffect(() => { if (world.data && !draftDirty) setDraft({ watch_symbols: world.data.profile.watch_symbols.join(", "), interests: world.data.profile.interests.join(", "), regions: world.data.profile.regions.join(", ") }); }, [world.data, draftDirty]);

  const save = useMutation({
    mutationFn: (profile: Profile) => request<Profile>("/api/world/profile", { method: "PUT", body: JSON.stringify(profile) }),
    onSuccess: async (profile) => { setDraft({ watch_symbols: profile.watch_symbols.join(", "), interests: profile.interests.join(", "), regions: profile.regions.join(", ") }); setDraftDirty(false); setSaved(true); await queryClient.invalidateQueries({ queryKey: ["world"] }); },
    onMutate: () => setSaved(false),
  });
  const refresh = useMutation({ mutationFn: () => request<RefreshResult>("/api/world/refresh", { method: "POST" }), onSuccess: async () => { await queryClient.invalidateQueries({ queryKey: ["world"] }); } });

  const topics = useMemo(() => [...new Set((world.data?.items ?? []).flatMap((item) => item.topics))].sort(), [world.data]);
  const filtered = useMemo(() => (world.data?.items ?? []).filter((item) => {
    const needle = search.trim().toLowerCase();
    const matchesSearch = !needle || [item.title, item.summary, item.source_name, ...item.topics, ...item.regions, ...item.matched_symbols].some((v) => v.toLowerCase().includes(needle));
    return matchesSearch && (!lensOnly || item.reasons.length > 0) && (topic === "all" || item.topics.includes(topic)) && (source === "all" || item.source_id === source);
  }), [world.data, search, lensOnly, topic, source]);

  const applyBook = () => {
    const next = bookDraft.trim() || null;
    if (next && !/^[0-9a-fA-F]{12}$/.test(next)) { setValidationError("Pinned book reference must be exactly 12 hexadecimal characters."); return; }
    setValidationError(null);
    writeActiveBookRef(next);
    setBookRef(next);
  };
  const saveLens = () => {
    const profile = { watch_symbols: csv(draft.watch_symbols, true), interests: csv(draft.interests), regions: csv(draft.regions) };
    const values = [...profile.watch_symbols, ...profile.interests, ...profile.regions];
    if (values.some((value) => value.length > 100)) {
      setValidationError("Each lens value must be 100 characters or fewer.");
      return;
    }
    if (profile.watch_symbols.length > 100 || profile.interests.length > 20 || profile.regions.length > 20) {
      setValidationError("Use at most 100 watch symbols, 20 interests, and 20 regions.");
      return;
    }
    setValidationError(null);
    save.mutate(profile);
  };

  const lensEditor = <form className="authoring-only-block" onSubmit={(event) => { event.preventDefault(); saveLens(); }}>
    <label>Watch symbols<input disabled={save.isPending} value={draft.watch_symbols} onChange={(e) => { setDraftDirty(true); setDraft((p) => ({ ...p, watch_symbols: e.target.value })); }} placeholder="NVDA, BP" /></label>
    <label>Interests<input disabled={save.isPending} value={draft.interests} onChange={(e) => { setDraftDirty(true); setDraft((p) => ({ ...p, interests: e.target.value })); }} placeholder="energy, semiconductors" /></label>
    <label>Regions<input disabled={save.isPending} value={draft.regions} onChange={(e) => { setDraftDirty(true); setDraft((p) => ({ ...p, regions: e.target.value })); }} placeholder="Europe, US" /></label>
    <button type="submit" disabled={save.isPending}>{save.isPending ? "Saving…" : "Save lens"}</button>
    {saved && !draftDirty && <p className="world-success" role="status">Lens saved.</p>}
    {validationError && <p className="world-error" role="alert">{validationError}</p>}
    {save.error && <p className="world-error" role="alert">Could not save lens: {(save.error as Error).message}</p>}
  </form>;

  return <section className="world" aria-labelledby="world-title">
    <header className="world-head">
      <div>
        <h1 id="world-title">World monitor</h1>
        <p>Cached world events, with the evidence for why each one reached your desk.</p>
      </div>
      <div className="world-asof"><span>Latest successful refresh</span><strong>{dateTime(world.data?.as_of ?? null)}</strong></div>
    </header>

    <section className="world-book" aria-label="Book context">
      <div className="world-book-controls authoring-only"><label htmlFor="world-book-ref">Pinned book reference</label><input id="world-book-ref" value={bookDraft} onChange={(e) => setBookDraft(e.target.value)} placeholder="12-character snapshot ID" /><button type="button" onClick={applyBook}>Apply</button></div>
      <span className={world.data?.context.book_ref ? "world-book-active" : "text-muted"}>{world.data?.context.book_ref ? `${world.data.context.label} · ${world.data.context.symbols.join(", ") || "no symbols"}` : "No book pinned — using your saved lens only"}</span>
    </section>

    {world.error && <div className="world-alert world-alert-error" role="alert">World cache unavailable: {(world.error as Error).message}</div>}
    {world.isLoading && !world.data && <p className="text-muted">Reading the local event cache…</p>}
    {world.error && !world.data && <section className="world-rail-section" aria-label="Personal lens recovery">
      <div className="world-section-head"><h2>Personal lens</h2><span>local</span></div>
      <p className="world-source-note">If saved preferences are invalid, enter and save a replacement lens here. For an invalid book reference, clear it above and apply.</p>
      {lensEditor}
      <p className="companion-only world-companion">Use the full workspace to repair saved preferences or change the selected book.</p>
    </section>}

    {world.data && <>
      <div className="world-toolbar">
        <input type="search" aria-label="Search world events" placeholder="Search events, regions, symbols" value={search} onChange={(e) => setSearch(e.target.value)} />
        <div className="world-segment" aria-label="Relevance filter"><button type="button" aria-pressed={!lensOnly} onClick={() => setLensOnly(false)}>All</button><button type="button" aria-pressed={lensOnly} onClick={() => setLensOnly(true)}>My lens</button></div>
        <label>Topic<select value={topic} onChange={(e) => setTopic(e.target.value)}><option value="all">All topics</option>{topics.map((value) => <option key={value}>{value}</option>)}</select></label>
        <label>Source<select value={source} onChange={(e) => setSource(e.target.value)}><option value="all">All sources</option>{world.data.sources.map((value) => <option value={value.id} key={value.id}>{value.name}</option>)}</select></label>
        <button className="world-refresh authoring-only" type="button" disabled={refresh.isPending || world.data.refreshing} onClick={() => refresh.mutate()}>{refresh.isPending || world.data.refreshing ? "Refresh in progress…" : "Refresh sources"}</button>
      </div>
      <p className="companion-only world-companion">Browsing and filters remain available. Source refresh and lens editing require the full workspace.</p>
      {refresh.data && <div className={refresh.data.failed ? "world-alert world-alert-warn" : "world-alert"} role="status">{refresh.data.updated} updated, {refresh.data.failed} failed, {refresh.data.skipped} skipped. Cached events remain available.</div>}
      {refresh.error && <div className="world-alert world-alert-error" role="alert">Refresh failed: {(refresh.error as Error).message}. Cached events remain available.</div>}

      <div className="world-grid">
        <section className="world-stream" aria-label="Event stream">
          <div className="world-section-head"><h2>Event stream</h2><span>{filtered.length} of {world.data.items.length}</span></div>
          {filtered.length === 0 && <div className="world-empty"><strong>{world.data.items.length === 0 ? "No events cached yet." : "No events match these filters."}</strong><span>{world.data.items.length === 0 ? "Refresh sources to fetch the first events." : "Clear search or switch to All to widen the cached view."}</span></div>}
          {filtered.map((item) => <article className="world-event" key={item.id}>
            <div className="world-event-main">
              <div className="world-event-meta"><span>{item.source_name}</span><time dateTime={item.published_at}>{item.time_kind === "observed" ? "Observed" : "Published"} {dateTime(item.published_at)}</time></div>
              <h3>{safeUrl(item.url) ? <a href={safeUrl(item.url)!} target="_blank" rel="noopener noreferrer">{item.title}</a> : item.title}</h3>
              {item.summary && <p>{item.summary}</p>}
              <div className="world-tags">{item.topics.map((value) => <span key={value}>{value}</span>)}{item.regions.map((value) => <span key={value}>{value}</span>)}</div>
            </div>
            <aside className={`world-reasons${item.reasons.some((reason) => reason.startsWith("Holding ")) ? " is-book" : item.reasons.length ? " is-lens" : ""}`} aria-label="Why this event is shown">
              <strong>{item.reasons.length ? "Why it matches" : "Outside your lens"}</strong>
              {item.reasons.length ? <ul>{item.reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul> : <p>No direct personal match. Included in the full world feed.</p>}
            </aside>
          </article>)}
        </section>

        <aside className="world-rail">
          <section className="world-rail-section">
            <div className="world-section-head"><h2>Personal lens</h2><span>local</span></div>
            {lensEditor}
            <div className="companion-only world-lens-readonly"><span>{draft.watch_symbols || "No watch symbols"}</span><span>{draft.interests || "No interests"}</span><span>{draft.regions || "No regions"}</span></div>
          </section>
          <section className="world-rail-section">
            <div className="world-section-head"><h2>Source status</h2><span>{world.data.sources.length}</span></div>
            <p className="world-source-note">This view checks the local cache every 30 seconds. New ingestion starts only from the manual button or CLI. <a href="https://github.com/ionutnodis/quant-mind/blob/main/docs/data-sources.md" target="_blank" rel="noopener noreferrer">Source setup</a></p>
            <div className="world-sources" role="region" aria-label="Source feed health" tabIndex={0}>{world.data.sources.map((item) => { const homepage = safeUrl(item.homepage); return <div className="world-source" data-testid={`source-${item.id}`} key={item.id}>
              <div><strong>{homepage ? <a href={homepage} target="_blank" rel="noopener noreferrer">{item.name}</a> : item.name}</strong><span className="world-source-states"><span className={`world-state state-${item.state}`}>{item.state === "ok" ? "●" : item.state === "error" ? "×" : item.state === "disabled" ? "◇" : "▲"} {item.state}</span>{item.stale && <span className="world-state state-stale">▲ stale</span>}</span></div>
              <p>{item.description}</p><dl><div><dt>Access</dt><dd>{item.access}</dd></div><div><dt>Cached</dt><dd>{item.item_count} items</dd></div><div><dt>Last success</dt><dd>{dateTime(item.last_success)}</dd></div></dl>
              {item.error && <p className="world-error">{item.error}</p>}
            </div>; })}</div>
          </section>
        </aside>
      </div>
    </>}
  </section>;
}
