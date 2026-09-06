import { Component, Suspense, lazy, type ReactNode } from "react";

// Keep a failed download inside its page/chart, with the shell and sibling
// evidence still usable. Never reload automatically: that could discard edits.
class DownloadBoundary extends Component<{
  children: ReactNode;
  label: string;
  minHeight: number;
}, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <div role="alert" className="min-w-0 space-y-2 p-3 text-muted" style={{ minHeight: this.props.minHeight }}>
        <p>{this.props.label} could not be loaded. Other sections remain available.</p>
        <p className="text-sm">Reload to retry. Unsaved edits will be lost.</p>
        <button
          type="button"
          className="qm-target min-h-11 border border-hairline px-3 text-ink hover:border-market focus-visible:outline-market"
          onClick={() => window.location.reload()}
        >
          Reload page
        </button>
      </div>
    );
  }
}

/** Declare at module scope so React preserves component identity and only
 * requests the module when rendered. Import failures stay local, including
 * missing hashed assets after an update; reload is always an explicit action. */
export function deferredComponent<Props extends object>(
  load: () => Promise<{ default: (props: Props) => ReactNode }>,
  label: string,
  minHeight = 96,
) {
  const Loaded = lazy(load);
  return function Deferred(props: Props) {
    return (
      <DownloadBoundary label={label} minHeight={minHeight}>
        <Suspense fallback={
          <div role="status" aria-label={`Loading ${label}`} className="flex min-w-0 items-center justify-center text-muted" style={{ minHeight }}>
            Loading {label}…
          </div>
        }>
          <Loaded {...props} />
        </Suspense>
      </DownloadBoundary>
    );
  };
}
