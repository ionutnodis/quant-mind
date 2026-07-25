import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createRootRoute,
  createRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import "./index.css";
import { Shell } from "./shell/Shell";
import { Today } from "./pages/Today";

const Stub = (label: string) =>
  function StubPage() {
    return (
      <div>
        <h1 className="text-xl font-semibold">{label}</h1>
        <p className="text-muted mt-2">Coming in a later phase.</p>
      </div>
    );
  };

export const rootRoute = createRootRoute({ component: Shell });
const routes = [
  createRoute({ getParentRoute: () => rootRoute, path: "/", component: Today }),
  createRoute({ getParentRoute: () => rootRoute, path: "/portfolio", component: Stub("Portfolio") }),
  createRoute({ getParentRoute: () => rootRoute, path: "/risk", component: Stub("Risk") }),
  createRoute({ getParentRoute: () => rootRoute, path: "/hedge", component: Stub("Hedge Lab") }),
  createRoute({ getParentRoute: () => rootRoute, path: "/whatif", component: Stub("What-If") }),
  createRoute({ getParentRoute: () => rootRoute, path: "/macro", component: Stub("Macro") }),
  createRoute({ getParentRoute: () => rootRoute, path: "/lab", component: Stub("Lab") }),
];

export const router = createRouter({ routeTree: rootRoute.addChildren(routes) });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}

const queryClient = new QueryClient();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>
);
