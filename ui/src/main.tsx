import { MutationCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { ApiError } from "./api";
import App from "./App";
import { refreshShown } from "./events";
import { LangProvider } from "./i18n";
import "./styles.css";

const client: QueryClient = new QueryClient({
  // Whatever a person did shows everywhere it appears on screen, not only in the
  // queries the button names; work that finishes later arrives as a live event.
  mutationCache: new MutationCache({ onSuccess: () => refreshShown(client) }),
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      refetchOnWindowFocus: true,
      // A 4xx will not change on retry, and 503 means the host is not set up yet.
      retry: (count, error) => !(error instanceof ApiError && (error.status < 500 || error.status === 503)) && count < 2,
    },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={client}>
      <LangProvider>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </LangProvider>
    </QueryClientProvider>
  </StrictMode>,
);
