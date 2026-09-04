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
import { Portfolio } from "./pages/Portfolio";
import { Risk } from "./pages/Risk";
import { Lab } from "./pages/Lab";
import { WhatIf } from "./pages/WhatIf";
import { Macro } from "./pages/Macro";
import { Hedge } from "./pages/Hedge";
import { Setup } from "./pages/Setup";

export const rootRoute = createRootRoute({ component: Shell });
const routes = [
  createRoute({ getParentRoute: () => rootRoute, path: "/", component: Today }),
  createRoute({ getParentRoute: () => rootRoute, path: "/portfolio", component: Portfolio }),
  createRoute({ getParentRoute: () => rootRoute, path: "/risk", component: Risk }),
  createRoute({ getParentRoute: () => rootRoute, path: "/hedge", component: Hedge }),
  createRoute({ getParentRoute: () => rootRoute, path: "/whatif", component: WhatIf }),
  createRoute({ getParentRoute: () => rootRoute, path: "/macro", component: Macro }),
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
