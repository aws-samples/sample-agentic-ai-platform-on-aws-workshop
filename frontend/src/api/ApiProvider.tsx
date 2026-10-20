import { createContext, useContext, useMemo, useRef, type ReactNode } from "react";
import { useAuth } from "react-oidc-context";

import { currentReturnTo } from "../auth/AppAuthProvider";
import type { RuntimeConfig } from "../config/runtimeConfig";
import { PlatformClient } from "./client";

const ApiContext = createContext<PlatformClient | null>(null);

/**
 * One PlatformClient for the application. The bearer is read through a ref on every request, so the client instance
 * stays stable while oidc-client-ts renews the ID token. A 401 (the token expired and could not be renewed) sends the
 * browser back through the Hosted UI and returns to the current page afterwards.
 */
export function ApiProvider({ config, children }: { config: RuntimeConfig; children: ReactNode }) {
  const auth = useAuth();
  const authRef = useRef(auth);
  authRef.current = auth;
  const client = useMemo(
    () =>
      new PlatformClient({
        baseUrl: config.apiUrl,
        getToken: () => authRef.current.user?.id_token,
        onUnauthorized: () => {
          const current = authRef.current;
          if (current.isAuthenticated && !current.activeNavigator) {
            void current.signinRedirect({ state: { returnTo: currentReturnTo() } });
          }
        },
      }),
    [config.apiUrl],
  );
  return <ApiContext.Provider value={client}>{children}</ApiContext.Provider>;
}

export function useApi(): PlatformClient {
  const client = useContext(ApiContext);
  if (!client) {
    throw new Error("useApi must be used inside ApiProvider");
  }
  return client;
}
