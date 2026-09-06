import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, expect, test } from "vitest";
import { request } from "../lib/api";

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

test("formats FastAPI validation errors without serializing rejected input or context", async () => {
  server.use(http.put("/api/world/profile", () => HttpResponse.json({ detail: [
    { type: "value_error", loc: ["body", "watch_symbols"], msg: "Value error, watch symbols must be ticker identifiers", input: ["private-input"], ctx: { error: "private-context" } },
    { type: "string_too_long", loc: ["body", "interests", 0], msg: "String should have at most 100 characters", input: "private-token", ctx: { max_length: 100 } },
  ] }, { status: 422 })));
  const error = await request<never>("/api/world/profile", { method: "PUT", body: "{}" }).catch((error: Error) => error);
  expect(error).toBeInstanceOf(Error);
  expect(error.message).toContain("watch_symbols: Value error, watch symbols must be ticker identifiers");
  expect(error.message).toContain("interests[0]: String should have at most 100 characters");
  expect(error.message).not.toMatch(/private-|\[object Object\]/);
});

test.each([
  { detail: "World cache unavailable", expected: "World cache unavailable" },
  { detail: { input: "private-token" }, expected: "/api/world → 503" },
  { detail: [{ input: "private-token", ctx: { error: "private-context" } }], expected: "/api/world → 503" },
])("preserves string errors and falls back safely for malformed structured detail", async ({ detail, expected }) => {
  server.use(http.get("/api/world", () => HttpResponse.json({ detail }, { status: 503 })));
  await expect(request("/api/world")).rejects.toThrow(expected);
});

test("keeps the status fallback for a non-JSON failure", async () => {
  server.use(http.get("/api/world", () => new HttpResponse("private upstream body", { status: 502 })));
  await expect(request("/api/world")).rejects.toThrow("/api/world → 502");
});
