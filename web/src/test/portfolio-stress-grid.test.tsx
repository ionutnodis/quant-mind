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
