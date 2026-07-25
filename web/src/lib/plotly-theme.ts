// The ONE Plotly theme (design decision 7A). tokens.css is the single source of
// truth — values are read from the CSS custom properties at call time, never
// duplicated here (design-review FINDING-003). Fallbacks only guard non-DOM
// environments (tests).
import type { Layout } from "plotly.js";

const FALLBACK: Record<string, string> = {
  ground: "#0a0a0b",
  surface: "#131316",
  hairline: "#26262b",
  ink: "#ededef",
  muted: "#8b8b93",
  you: "#e8a33d",
  market: "#7fa0b4",
  up: "#2fbf71",
  down: "#e5484d",
};

function cssToken(name: string): string {
  if (typeof document === "undefined") return FALLBACK[name];
  const v = getComputedStyle(document.documentElement)
    .getPropertyValue(`--color-${name}`)
    .trim();
  return v || FALLBACK[name];
}

export const tokens = {
  get ground() { return cssToken("ground"); },
  get surface() { return cssToken("surface"); },
  get hairline() { return cssToken("hairline"); },
  get ink() { return cssToken("ink"); },
  get muted() { return cssToken("muted"); },
  get you() { return cssToken("you"); },
  get market() { return cssToken("market"); },
  get up() { return cssToken("up"); },
  get down() { return cssToken("down"); },
};

export function baseLayout(): Partial<Layout> {
  return {
    paper_bgcolor: tokens.ground,
    plot_bgcolor: tokens.ground,
    font: { family: "Geist Mono, monospace", size: 11, color: tokens.muted },
    margin: { l: 40, r: 10, t: 10, b: 30 },
    xaxis: { gridcolor: tokens.hairline, zerolinecolor: tokens.hairline },
    yaxis: { gridcolor: tokens.hairline, zerolinecolor: tokens.hairline },
    showlegend: false,
  };
}
