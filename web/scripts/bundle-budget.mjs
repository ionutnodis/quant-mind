// Run with node --test (separate from Vitest). Compile the real app in memory.
// A static chart import or eager page import
// must fail this gate even if someone splits/renames the emitted vendor file.
import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";
import { build } from "vite";

const result = await build({
  root: fileURLToPath(new URL("..", import.meta.url)),
  logLevel: "error",
  build: { write: false },
});
const chunks = result.output.filter((item) => item.type === "chunk");
const byName = new Map(chunks.map((chunk) => [chunk.fileName, chunk]));
const entry = chunks.find((chunk) => chunk.isEntry);

function dependencies(start) {
  const visited = new Set();
  function visit(chunk) {
    assert.ok(chunk, "Every static import must resolve to an emitted chunk");
    if (visited.has(chunk)) return;
    visited.add(chunk);
    chunk.imports.forEach((name) => visit(byName.get(name)));
  }
  visit(start);
  return [...visited];
}

function containsPlotly(group) {
  return group.some((chunk) => Object.keys(chunk.modules).some((id) => id.includes("/plotly.js")));
}

test("initial shell stays below 500 kB raw / 150 kB gzip without loading charts or pages", () => {
  const initial = dependencies(entry);
  const raw = initial.reduce((sum, chunk) => sum + Buffer.byteLength(chunk.code), 0);
  const gzip = initial.reduce((sum, chunk) => sum + gzipSync(chunk.code).length, 0);
  assert.ok(raw < 500_000, `Initial JavaScript is ${raw} bytes; budget is 500000`);
  assert.ok(gzip < 150_000, `Initial gzip JavaScript is ${gzip} bytes; budget is 150000`);
  assert.equal(containsPlotly(initial), false, "Plotly must not be in the shell's static dependency graph");
  assert.equal(initial.some((chunk) => Object.keys(chunk.modules).some((id) => id.includes("/src/pages/"))), false);
  console.log(`Initial JavaScript: ${raw} bytes; gzip: ${gzip} bytes`);
});

for (const page of ["Today", "World", "Setup"]) {
  test(`${page} can load without downloading Plotly`, () => {
    const chunk = chunks.find((item) => Object.keys(item.modules).some((id) => id.endsWith(`/pages/${page}.tsx`)));
    assert.ok(chunk, `${page} must be in the production build`);
    assert.equal(containsPlotly(dependencies(chunk)), false, `${page} eagerly imports the chart runtime`);
  });
}

test("all routes remain separately loadable and the chart runtime is not duplicated", () => {
  for (const page of ["Today", "Portfolio", "Risk", "Hedge", "WhatIf", "Macro", "World", "Lab", "Setup"]) {
    assert.ok(chunks.some((chunk) => chunk.isDynamicEntry && chunk.facadeModuleId?.endsWith(`/pages/${page}.tsx`)), `${page} needs an on-demand entry`);
  }
  const chartChunks = chunks.filter((chunk) => containsPlotly([chunk]));
  assert.equal(chartChunks.length, 1, "One shared Plotly runtime, not a copy per chart");
  assert.ok(Buffer.byteLength(chartChunks[0].code) < 1_600_000, "Review any growth of the deferred Plotly runtime");
});
