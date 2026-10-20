import { useMemo, type ReactNode } from "react";
import { WebStorageStateStore, type User, type UserManagerSettings } from "oidc-client-ts";
import { AuthProvider } from "react-oidc-context";

import { cognitoAuthority, type RuntimeConfig } from "../config/runtimeConfig";
import { useFollowPersonaAcrossTabs } from "./followPersonaAcrossTabs";

export interface SigninState {
  returnTo?: string;
}

/** OIDC callback parameters: never part of a return target (a stale `?code=&state=` would re-run the callback on every reload). */
const CALLBACK_PARAMS = ["code", "state", "error", "error_description", "error_uri", "iss", "session_state"];

/**
 * Only same-origin paths are honoured as a return target after sign-in; the value is parsed as a URL (so `/\\evil.example`,
 * which the parser reads as `//evil.example`, is refused like `//evil.example` and `https://...`) and the OIDC callback
 * parameters are removed, so a sign-in started from a failed-callback page returns to a clean address.
 */
export function safeReturnTo(state: unknown, origin: string = window.location.origin): string {
  const returnTo = (state as SigninState | undefined)?.returnTo;
  if (typeof returnTo !== "string" || !returnTo.startsWith("/")) {
    return "/";
  }
  let url: URL;
  try {
    url = new URL(returnTo, origin);
  } catch {
    return "/";
  }
  if (url.origin !== origin) {
    return "/";
  }
  for (const name of CALLBACK_PARAMS) {
    url.searchParams.delete(name);
  }
  return `${url.pathname}${url.search}${url.hash}`;
}

/** The current page as a sign-in return target (path + query + hash without callback parameters). */
export function currentReturnTo(): string {
  return safeReturnTo({ returnTo: `${window.location.pathname}${window.location.search}${window.location.hash}` });
}

/**
 * The oidc-client-ts settings of the application. `switchPersona` builds a UserManager over the same settings, so it writes
 * the switched-to user exactly where the provider's UserManager reads the signed-in user (localStorage, one key per
 * authority and client).
 */
export function oidcSettings(config: RuntimeConfig): UserManagerSettings {
  return {
    authority: cognitoAuthority(config),
    client_id: config.clientId,
    redirect_uri: `${window.location.origin}/`,
    post_logout_redirect_uri: `${window.location.origin}/`,
    response_type: "code",
    scope: "openid email profile",
    automaticSilentRenew: true,
    loadUserInfo: false,
    userStore: new WebStorageStateStore({ store: window.localStorage }),
  };
}

/**
 * Cognito Hosted UI sign-in with the authorization code flow + PKCE (oidc-client-ts). The authority is the user pool's
 * OIDC issuer, the ID token is renewed with the refresh token (automaticSilentRenew) and kept in localStorage so a page
 * reload or a second tab stays signed in. `onSigninCallback` removes `?code=&state=` from the address bar and restores
 * the page the user came from.
 */
export function AppAuthProvider({ config, children }: { config: RuntimeConfig; children: ReactNode }) {
  useFollowPersonaAcrossTabs(config);
  const settings = useMemo(
    () => ({
      ...oidcSettings(config),
      onSigninCallback: (user: User | undefined) => {
        window.history.replaceState({}, document.title, safeReturnTo(user?.state));
      },
    }),
    [config],
  );
  return <AuthProvider {...settings}>{children}</AuthProvider>;
}
