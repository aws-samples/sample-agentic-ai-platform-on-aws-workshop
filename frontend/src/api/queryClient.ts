import { QueryClient } from "@tanstack/react-query";

import { isApiError } from "./client";

/** Shared defaults: no retries on 4xx (the message is the answer), one retry on transport / 5xx errors. */
export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 15_000,
        refetchOnWindowFocus: false,
        retry: (failureCount, error) => {
          if (isApiError(error) && error.status !== null && error.status < 500) {
            return false;
          }
          return failureCount < 1;
        },
      },
      mutations: { retry: 0 },
    },
  });
}
