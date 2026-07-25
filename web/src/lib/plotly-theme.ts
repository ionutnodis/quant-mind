// The ONE Plotly theme (design doc decision 7A). Every chart imports this;
// no color literals in chart code — the amber law is enforceable only if
// amber has exactly one definition (tokens.css). Values mirror tokens.css.
import type { Layout } from "plotly.js";

export const tokens = {
  ground: "#0a0a0b",
  surface: "#131316",
  hairline: "#26262b",
  ink: "#ededef",
  muted: "#8b8b93",
  you: "#e8a33d",
  market: "#7fa0b4",
  up: "#2fbf71",
  down: "#e5484d",
} as const;

export const baseLayout: Partial<Layout> = {
  paper_bgcolor: tokens.ground,
  plot_bgcolor: tokens.ground,
  font: { family: "Geist Mono, monospace", size: 11, color: tokens.muted },
  margin: { l: 40, r: 10, t: 10, b: 30 },
  xaxis: { gridcolor: tokens.hairline, zerolinecolor: tokens.hairline },
  yaxis: { gridcolor: tokens.hairline, zerolinecolor: tokens.hairline },
  showlegend: false,
};
