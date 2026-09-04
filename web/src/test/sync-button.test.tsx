import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { delay, http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, expect, test } from "vitest";
import { SyncButton } from "../components/SyncButton";

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

function renderSyncButton() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <SyncButton />
    </QueryClientProvider>,
  );
}

test("retry ignores the completed job's stale poll and monitors the new job", async () => {
  let submissions = 0;
  server.use(
    http.post("/api/sync", async () => {
      submissions += 1;
      if (submissions === 2) await delay(100);
      return HttpResponse.json({ job_id: submissions === 1 ? "old-job" : "new-job" });
    }),
    http.get("/api/sync/old-job", () =>
      HttpResponse.json({ state: "done", result: "old result" }),
    ),
    http.get("/api/sync/new-job", () =>
      HttpResponse.json({ state: "done", result: "new result" }),
    ),
  );

  renderSyncButton();
  const button = screen.getByTestId("sync-now");
  fireEvent.click(button);
  expect(await screen.findByText("old result")).toBeInTheDocument();
  await waitFor(() => expect(button).not.toBeDisabled());

  fireEvent.click(button);

  expect(await screen.findByText("new result")).toBeInTheDocument();
});
