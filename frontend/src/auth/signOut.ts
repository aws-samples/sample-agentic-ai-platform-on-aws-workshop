import type { AuthContextProps } from "react-oidc-context";

import { cognitoLogoutUrl, type RuntimeConfig } from "../config/runtimeConfig";

/**
 * Sign out: revoke the refresh token at Cognito's /oauth2/revoke (the app client has token revocation enabled; the Hosted UI
 * /logout endpoint only clears its own session cookie and would leave the 30-day refresh token usable), forget the tokens,
 * then end the Hosted UI session (the logout URL is built by hand and must match a registered sign-out URL of the app client).
 * A failed revocation never blocks the sign-out.
 */
export async function signOut(auth: Pick<AuthContextProps, "removeUser" | "revokeTokens">, config: RuntimeConfig): Promise<void> {
  try {
    await auth.revokeTokens(["refresh_token"]).catch((error: unknown) => console.warn("refresh token revocation failed", error));
    await auth.removeUser();
  } finally {
    window.location.assign(cognitoLogoutUrl(config, window.location.origin));
  }
}
