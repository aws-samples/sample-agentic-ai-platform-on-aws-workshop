/**
 * Other tabs of the same browser share the stored user. After a Switch persona in one tab, another tab would keep the old
 * persona's navigation and buttons while sending the new persona's token once its own token renews. The `storage` event tells
 * the other tabs right away: when the stored persona changed, the tab loads itself again and follows.
 */
import { useEffect } from "react";

import { cognitoAuthority, type RuntimeConfig } from "../config/runtimeConfig";

/** The localStorage key oidc-client-ts uses for the signed-in user: the WebStorageStateStore prefix "oidc." + "user:<authority>:<client_id>". */
export function oidcUserStorageKey(config: Pick<RuntimeConfig, "region" | "userPoolId" | "clientId">): string {
  return `oidc.user:${cognitoAuthority(config)}:${config.clientId}`;
}

function usernameOf(stored: string | null): string | undefined {
  if (!stored) {
    return undefined;
  }
  try {
    const profile = (JSON.parse(stored) as { profile?: Record<string, unknown> }).profile;
    const username = profile?.["cognito:username"];
    return typeof username === "string" ? username : undefined;
  } catch {
    return undefined;
  }
}

/** True when a `storage` event replaced the stored user with another persona; a renewal of the same persona, a sign-in or a sign-out is not. */
export function personaChangedInStorage(event: Pick<StorageEvent, "key" | "oldValue" | "newValue">, key: string): boolean {
  if (event.key !== key) {
    return false;
  }
  const before = usernameOf(event.oldValue);
  const after = usernameOf(event.newValue);
  return before !== undefined && after !== undefined && before !== after;
}

/** Loads the page again when another tab switched the stored user to a different persona (the browser never fires the event in the tab that wrote). */
export function useFollowPersonaAcrossTabs(config: RuntimeConfig): void {
  useEffect(() => {
    const key = oidcUserStorageKey(config);
    const onStorage = (event: StorageEvent) => {
      if (personaChangedInStorage(event, key)) {
        window.location.reload();
      }
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, [config]);
}
