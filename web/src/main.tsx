import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createBrowserRouter, RouterProvider } from "react-router-dom";

import "./index.css";
import { Splash } from "./routes/Splash";
import { Signin } from "./routes/Signin";
import { Dashboard } from "./routes/Dashboard";
import { Admin } from "./routes/Admin";
import { NotFound } from "./routes/NotFound";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 30_000, refetchOnWindowFocus: false },
  },
});

const router = createBrowserRouter([
  { path: "/", element: <Splash /> },
  { path: "/signin", element: <Signin /> },
  { path: "/dashboard", element: <Dashboard /> },
  { path: "/admin", element: <Admin /> },
  { path: "*", element: <NotFound /> },
]);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </React.StrictMode>,
);
