/**
 * PortfolioStressGrid: pure presentational table over the book's option
 * sleeve spot x vol stress grid (GET /api/portfolio's
 * options_sleeve.stress_grid, exposure/book_greeks.py's
 * aggregate_book_stress_grid). No data fetching — a direct render test,
 * unlike the Plotly chart components (FanChart/RollingBetaChart), which are
 * always stubbed in the pages that use them (Plotly needs real canvas).
 */
import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { PortfolioStressGrid } from "../components/PortfolioStressGrid";

test("renders vol/spot axis headers and pnl cells", () => {
  render(
    <PortfolioStressGrid
      grid={{
        vol_shocks: [-0.05, 0.0, 0.05],
        spot_shocks: [-0.1, 0.0, 0.1],
        pnl: [
          [-500, -100, 400],
          [-450, 0, 450],
          [-400, 100, 500],
        ],
      }}
    />
  );
  // spot shock header labels
  expect(screen.getByText("-10%")).toBeInTheDocument();
  expect(screen.getByText("+10%")).toBeInTheDocument();
  // vol shock row labels
  expect(screen.getByText("+5%")).toBeInTheDocument();
  // pnl cells
  expect(screen.getByText("500")).toBeInTheDocument();
  expect(screen.getByText("-500")).toBeInTheDocument();
  expect(screen.getByText("0")).toBeInTheDocument();
});

test("cells are amber book P&L — sign carried by the number, never up/down colors", () => {
  // Amber law (DESIGN.md + commit 4b00125's precedent on this same page):
  // the stress grid is the book's own scenario P&L, so cell text is
  // text-you regardless of sign; green/red stays reserved for market data.
  // The magnitude opacity ramp survives, in amber.
  render(
    <PortfolioStressGrid
      grid={{
        vol_shocks: [0.0],
        spot_shocks: [-0.1, 0.1],
        pnl: [[-500, 500]],
      }}
    />
  );
  for (const cell of [screen.getByText("-500"), screen.getByText("500")]) {
    expect(cell.className).toMatch(/\btext-you\b/);
    expect(cell.className).not.toMatch(/\btext-up\b|\btext-down\b/);
    expect(cell.getAttribute("style") ?? "").not.toMatch(/--color-(up|down)/);
  }
});

test("null cells render as em-dash placeholders, not crashes", () => {
  render(
    <PortfolioStressGrid
      grid={{
        vol_shocks: [0.0],
        spot_shocks: [0.0, 0.1],
        pnl: [[null, 250]],
      }}
    />
  );
  expect(screen.getByText("—")).toBeInTheDocument();
  expect(screen.getByText("250")).toBeInTheDocument();
});
