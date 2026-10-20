/**
 * "Switch persona" (user menu, top right): a signed-in learner becomes another persona without typing its password.
 *
 * A workshop shortcut, not a production pattern. POST /me/switch signs in as the persona on the API side (USER_PASSWORD_AUTH
 * with the password from the users secret, the path `platform_sdk.auth.login` and `workshop apply 2` use) and returns the tokens;
 * the browser stores them where oidc-client-ts keeps the signed-in user and loads the current page again, so the whole
 * application (React Query cache, GET /me, navigation, buttons) restarts under the new identity. First sign-in still happens
 * through the Hosted UI. The Hosted UI session cookie is untouched: it belongs to the persona who signed in first and is only
 * consulted when the browser is sent to the Hosted UI again (Sign out clears it).
 */
import { User, UserManager, type UserProfile } from "oidc-client-ts";
import type { AuthContextProps } from "react-oidc-context";

import type { PlatformClient } from "../api/client";
import type { Persona, SwitchPersonaResult } from "../api/types";
import type { RuntimeConfig } from "../config/runtimeConfig";
import { oidcSettings } from "./AppAuthProvider";
import { ROLE_LABELS, ROLES } from "./roles";

/** sessionStorage key written right before the page loads again; the Shell shows the persona once as a confirmation. */
export const SWITCHED_PERSONA_KEY = "agent-platform.switched-persona";

/** Menu item ids are "switch:<persona>". */
export const SWITCH_ITEM_PREFIX = "switch:";

/** The three personas; their usernames equal the role names (platform_sdk.auth.PERSONA_USERS). */
export const PERSONAS: ReadonlyArray<Persona> = ROLES;

export function isPersona(value: unknown): value is Persona {
  return typeof value === "string" && (PERSONAS as ReadonlyArray<string>).includes(value);
}

/** "Agent developer (developer)": the role label the UI uses everywhere, then the username the course names. */
export function personaLabel(persona: Persona): string {
  return `${ROLE_LABELS[persona]} (${persona})`;
}

export interface PersonaMenuItem {
  id: string;
  text: string;
  disabled: boolean;
  disabledReason?: string;
}

/** The entries of the "Switch persona" menu group; the signed-in persona is disabled with the reason. */
export function personaMenuItems(currentUsername: string | undefined): PersonaMenuItem[] {
  return PERSONAS.map((persona) => {
    const current = persona === currentUsername;
    return {
      id: `${SWITCH_ITEM_PREFIX}${persona}`,
      text: personaLabel(persona),
      disabled: current,
      disabledReason: current ? "You are signed in as this persona" : undefined,
    };
  });
}

/** The persona of a menu item id ("switch:developer" -> "developer"); null for every other item. */
export function personaFromItemId(id: string): Persona | null {
  if (!id.startsWith(SWITCH_ITEM_PREFIX)) {
    return null;
  }
  const persona = id.slice(SWITCH_ITEM_PREFIX.length);
  return isPersona(persona) ? persona : null;
}

/**
 * The oidc-client-ts User for the tokens POST /me/switch returned. `expires_at` is the ID token's `exp` and the refresh token is
 * kept: without both, oidc-client-ts never arms automaticSilentRenew, and the 401 after the 8 h token life would send the browser
 * through the Hosted UI, whose session cookie still belongs to the persona who signed in first.
 */
export function buildSwitchedUser(result: SwitchPersonaResult): User {
  if (!result.idToken || !result.accessToken || !result.refreshToken) {
    throw new Error("POST /me/switch answered without the tokens the browser needs (idToken, accessToken, refreshToken)");
  }
  const claims = result.claims;
  if (typeof claims?.sub !== "string" || typeof claims.exp !== "number") {
    throw new Error("POST /me/switch answered without usable ID-token claims (sub, exp)");
  }
  return new User({
    id_token: result.idToken,
    access_token: result.accessToken,
    refresh_token: result.refreshToken,
    token_type: "Bearer",
    scope: "openid email profile",
    profile: claims as UserProfile,
    expires_at: claims.exp,
  });
}

/** Writes `user` where the application's UserManager reads the signed-in user (same settings, so the same localStorage key). */
export async function storeSwitchedUser(config: RuntimeConfig, user: User): Promise<void> {
  const manager = new UserManager({ ...oidcSettings(config), automaticSilentRenew: false });
  await manager.storeUser(user);
}

export interface SwitchPersonaOptions {
  api: Pick<PlatformClient, "switchPersona">;
  auth: Pick<AuthContextProps, "stopSilentRenew" | "revokeTokens">;
  config: RuntimeConfig;
  persona: Persona;
}

/**
 * Become another persona: POST /me/switch, stop renewing the outgoing persona's token and revoke its refresh token (the stored
 * user is still the outgoing one at that point), store the new user, then load the current page again. A screen the new persona
 * may not see shows the role notice of RequireRole, which points back at this menu.
 */
export async function switchPersona({ api, auth, config, persona }: SwitchPersonaOptions): Promise<void> {
  const user = buildSwitchedUser(await api.switchPersona(persona));
  auth.stopSilentRenew();
  await auth.revokeTokens(["refresh_token"]).catch((error: unknown) => console.warn("refresh token revocation failed", error));
  await storeSwitchedUser(config, user);
  window.sessionStorage.setItem(SWITCHED_PERSONA_KEY, persona);
  // reload() keeps path, query and hash and always loads the document again (location.replace() of the same URL with a hash
  // would only be a fragment navigation). The Shell never renders on a callback URL: AuthGate navigates away from it first.
  window.location.reload();
}

/** The persona a just-finished switch stored, cleared on read (null when there was no switch or the value is not a persona). */
export function takeSwitchedPersona(): Persona | null {
  const value = window.sessionStorage.getItem(SWITCHED_PERSONA_KEY);
  if (value !== null) {
    window.sessionStorage.removeItem(SWITCHED_PERSONA_KEY);
  }
  return isPersona(value) ? value : null;
}
