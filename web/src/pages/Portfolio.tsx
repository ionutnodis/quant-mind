// Portfolio: the truth about the book (DESIGN.md IA #2). Dense positions
// table over GET /api/portfolio. Amber marks the user's book (market value,
// weight); market facts (symbol/type/qty/last) stay neutral. Honest empty
// state when the paper book has no positions yet, matching Today's language.
import { useQuery } from "@tanstack/react-query";
import { Panel, Skeleton } from "../components/Panel";
import { request } from "../lib/api";

interface Position {
  con_id: number;
  symbol: string;
  qty: number;
  sec_type: string;
  multiplier: number;
  last_close: number | null;
  market_value: number | null;
  weight: number | null;
}

interface Totals {
  market_value: number | null;
  n_positions: number;
}

interface PortfolioResponse {
  snapshot_id: string;
  valuation_ts: string;
  base_currency: string;
  positions: Position[];
  totals: Totals;
}

function fetchPortfolio(): Promise<PortfolioResponse> {
  return request<PortfolioResponse>("/api/portfolio");
}

function fmtNum(v: number | null, digits = 2): string {
  return v === null ? "—" : v.toFixed(digits);
}

function fmtWeight(v: number | null): string {
  return v === null ? "—" : `${(v * 100).toFixed(1)}%`;
}

export function Portfolio() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["portfolio"],
    queryFn: fetchPortfolio,
    staleTime: 60 * 1000,
  });

  if (isLoading) return <Skeleton className="h-48" />;
  if (error) return <p className="text-down">Portfolio unavailable: {String(error)}</p>;
  if (!data) return null;

  const note = `snapshot ${data.snapshot_id} · as of ${data.valuation_ts.slice(0, 10)}`;

  if (data.positions.length === 0) {
    return (
      <Panel title="Portfolio" note={note}>
        <p className="text-muted">
          No positions in the paper book yet — the table fills in once the broker connects and
          reports a book.
        </p>
      </Panel>
    );
  }

  return (
    <Panel title="Portfolio" note={note}>
      <table className="w-full text-[12px]">
        <thead>
          <tr className="text-[10px] tracking-wider uppercase text-muted border-b border-hairline">
            <th className="text-left py-1.5 font-normal">Symbol</th>
            <th className="text-left py-1.5 font-normal">Type</th>
            <th className="text-right py-1.5 font-normal">Qty</th>
            <th className="text-right py-1.5 font-normal">Last</th>
            <th className="text-right py-1.5 font-normal">Mkt value</th>
            <th className="text-right py-1.5 font-normal">Weight</th>
          </tr>
        </thead>
        <tbody>
          {data.positions.map((p) => (
            <tr key={p.con_id} className="border-b border-hairline/60">
              <td className="py-1.5 text-ink">{p.symbol}</td>
              <td className="py-1.5 text-muted">{p.sec_type}</td>
              <td className="num py-1.5 text-right">{fmtNum(p.qty, 0)}</td>
              <td className="num py-1.5 text-right">{fmtNum(p.last_close)}</td>
              <td className="num py-1.5 text-right text-you">{fmtNum(p.market_value)}</td>
              <td className="num py-1.5 text-right text-you">{fmtWeight(p.weight)}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr className="border-t border-hairline">
            <td className="py-1.5 text-ink" colSpan={2}>
              Total ({data.totals.n_positions})
            </td>
            <td />
            <td />
            <td className="num py-1.5 text-right text-you">{fmtNum(data.totals.market_value)}</td>
            <td />
          </tr>
        </tfoot>
      </table>
    </Panel>
  );
}
