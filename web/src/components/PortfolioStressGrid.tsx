// PortfolioStressGrid: the book's option sleeve spot x vol stress grid
// (exposure/book_greeks.py's `aggregate_book_stress_grid`, reused not
// re-derived — GET /api/portfolio's `options_sleeve.stress_grid`). This is
// the book's own scenario P&L, so cells are AMBER (DESIGN.md amber law +
// commit 4b00125's precedent on this same page: book P&L renders text-you
// regardless of sign, the sign carried by the number itself; green/red
// stays reserved for market up/down data). Magnitude still pops via an
// amber opacity ramp — no decorative heatmap gradient; hairline borders +
// the data itself do the separating (DESIGN.md decoration law).
export interface StressGrid {
  vol_shocks: number[];
  spot_shocks: number[];
  pnl: (number | null)[][]; // rows = vol_shocks, cols = spot_shocks
}

function fmtPct(x: number): string {
  const sign = x > 0 ? "+" : "";
  return `${sign}${(x * 100).toFixed(0)}%`;
}

function fmtPnl(x: number | null): string {
  if (x === null || !Number.isFinite(x)) return "—";
  return x.toLocaleString("en-US", { maximumFractionDigits: 0 });
}

export function PortfolioStressGrid({ grid }: { grid: StressGrid }) {
  const flat = grid.pnl.flat().filter((v): v is number => v !== null && Number.isFinite(v));
  const maxAbs = flat.length > 0 ? Math.max(...flat.map((v) => Math.abs(v)), 1) : 1;

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[11px]" data-testid="portfolio-stress-grid">
        <thead>
          <tr>
            <th className="text-left py-1 pr-2 text-[10px] tracking-wider uppercase text-muted">
              vol \ spot
            </th>
            {grid.spot_shocks.map((s) => (
              <th key={s} className="num text-right py-1 px-2 text-[10px] text-muted font-normal">
                {fmtPct(s)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {grid.vol_shocks.map((v, ri) => (
            <tr key={v} className="border-t border-hairline/60">
              <td className="num py-1 pr-2 text-muted">{fmtPct(v)}</td>
              {grid.pnl[ri]?.map((cell, ci) => {
                const isNum = cell !== null && Number.isFinite(cell);
                const opacity = isNum ? Math.min(1, Math.abs(cell) / maxAbs) : 0;
                return (
                  <td
                    key={ci}
                    className={`num text-right py-1 px-2 ${isNum ? "text-you" : "text-muted"}`}
                    style={isNum ? { backgroundColor: `color-mix(in srgb, var(--color-you) ${opacity * 22}%, transparent)` } : undefined}
                  >
                    {fmtPnl(cell)}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
