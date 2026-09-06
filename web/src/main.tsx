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
import { deferredComponent } from "./components/DeferredContent";

// Explicit import paths let Vite emit a separate entry for each page. The
// shell stays mounted while a page loads or its download needs recovery.
const Today = deferredComponent(async () => ({ default: (await import("./pages/Today")).Today }), "Today");
const Portfolio = deferredComponent(async () => ({ default: (await import("./pages/Portfolio")).Portfolio }), "Portfolio");
const Risk = deferredComponent(async () => ({ default: (await import("./pages/Risk")).Risk }), "Risk");
const Lab = deferredComponent(async () => ({ default: (await import("./pages/Lab")).Lab }), "Lab");
const WhatIf = deferredComponent(async () => ({ default: (await import("./pages/WhatIf")).WhatIf }), "What-If");
const Macro = deferredComponent(async () => ({ default: (await import("./pages/Macro")).Macro }), "Macro");
const Hedge = deferredComponent(async () => ({ default: (await import("./pages/Hedge")).Hedge }), "Hedge Lab");
const Setup = deferredComponent(async () => ({ default: (await import("./pages/Setup")).Setup }), "Setup");
const World = deferredComponent(async () => ({ default: (await import("./pages/World")).World }), "World");

export const rootRoute = createRootRoute({ component: Shell });
const routes = [
  createRoute({ getParentRoute: () => rootRoute, path: "/", component: Today }),
  createRoute({ getParentRoute: () => rootRoute, path: "/portfolio", component: Portfolio }),
  createRoute({ getParentRoute: () => rootRoute, path: "/risk", component: Risk }),
  createRoute({ getParentRoute: () => rootRoute, path: "/hedge", component: Hedge }),
  createRoute({ getParentRoute: () => rootRoute, path: "/whatif", component: WhatIf }),
  createRoute({ getParentRoute: () => rootRoute, path: "/macro", component: Macro }),
  createRoute({ getParentRoute: () => rootRoute, path: "/world", component: World }),
  createRoute({ getParentRoute: () => rootRoute, path: "/lab", component: Lab }),
  createRoute({ getParentRoute: () => rootRoute, path: "/book/setup", component: Setup }),
  // First-use alias retained for direct links in onboarding documentation.
  createRoute({ getParentRoute: () => rootRoute, path: "/setup", component: Setup }),
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
