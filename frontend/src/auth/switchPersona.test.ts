import { User } from "oidc-client-ts";
import { describe, expect, it } from "vitest";

import type { SwitchPersonaResult } from "../api/types";
import type { RuntimeConfig } from "../config/runtimeConfig";
import { oidcUserStorageKey } from "./followPersonaAcrossTabs";
import {
  SWITCHED_PERSONA_KEY,
  buildSwitchedUser,
  personaFromItemId,
  personaLabel,
  personaMenuItems,
  storeSwitchedUser,
  takeSwitchedPersona,
} from "./switchPersona";

const NOW = Math.floor(Date.now() / 1000);
const RESULT: SwitchPersonaResult = {
  persona: "developer",
  username: "developer",
  idToken: "id.token",
  accessToken: "access.token",
  refreshToken: "refresh.token",
  expiresIn: 28800,
  claims: {
    sub: "sub-developer",
    iss: "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_abc",
    aud: "client-1",
    exp: NOW + 28800,
    iat: NOW,
    token_use: "id",
    "cognito:username": "developer",
    "cognito:groups": ["agent-developers"],
    email: "developer@anycompany.example",
  },
};
const CONFIG: RuntimeConfig = {
  apiUrl: "https://api.example.test",
  region: "us-east-1",
  userPoolId: "us-east-1_abc",
  clientId: "client-1",
  authDomain: "https://auth.example.test",
  modelGatewayUrl: "",
  uiUrl: "",
};

describe("buildSwitchedUser", () => {
  it("arms silent renew (expires_at and refresh token) and survives the storage round trip", () => {
    const user = buildSwitchedUser(RESULT);
    expect(user.expired).toBe(false);
    expect(user.expires_in).toBeGreaterThan(28_000);
    expect(user.refresh_token).toBe("refresh.token");
    expect(user.token_type).toBe("Bearer");
    expect(user.scope).toBe("openid email profile");
    expect(user.profile["cognito:username"]).toBe("developer");
    const restored = User.fromStorageString(user.toStorageString());
    expect(restored.id_token).toBe("id.token");
    expect(restored.refresh_token).toBe("refresh.token");
    expect(restored.expires_at).toBe(RESULT.claims.exp);
    expect(restored.profile.sub).toBe("sub-developer");
  });

  it("refuses an answer without the tokens or without usable claims", () => {
    expect(() => buildSwitchedUser({ ...RESULT, refreshToken: null })).toThrow(/tokens/);
    expect(() => buildSwitchedUser({ ...RESULT, idToken: "" })).toThrow(/tokens/);
    expect(() => buildSwitchedUser({ ...RESULT, claims: { sub: "sub-developer" } })).toThrow(/claims/);
    expect(() => buildSwitchedUser({ ...RESULT, claims: { exp: NOW + 10 } })).toThrow(/claims/);
  });
});

describe("storeSwitchedUser", () => {
  it("writes the user under the key the provider's UserManager reads (oidc.user:<authority>:<client>)", async () => {
    window.localStorage.clear();
    await storeSwitchedUser(CONFIG, buildSwitchedUser(RESULT));
    const stored = window.localStorage.getItem("oidc.user:https://cognito-idp.us-east-1.amazonaws.com/us-east-1_abc:client-1");
    expect(stored).not.toBeNull();
    expect(window.localStorage.getItem(oidcUserStorageKey(CONFIG))).toBe(stored);
    const user = User.fromStorageString(stored!);
    expect(user.id_token).toBe("id.token");
    expect(user.refresh_token).toBe("refresh.token");
    expect(user.expired).toBe(false);
    expect(Object.keys(window.localStorage).filter((key) => key.startsWith("oidc."))).toHaveLength(1);
    window.localStorage.clear();
  });
});

describe("persona menu", () => {
  it("labels role first with the username in parentheses and disables the signed-in persona with a reason", () => {
    expect(personaLabel("approver")).toBe("Domain approver (approver)");
    const items = personaMenuItems("developer");
    expect(items.map((item) => item.id)).toEqual(["switch:admin", "switch:developer", "switch:approver"]);
    expect(items.map((item) => item.text)).toEqual(["Platform admin (admin)", "Agent developer (developer)", "Domain approver (approver)"]);
    expect(items.map((item) => item.disabled)).toEqual([false, true, false]);
    expect(items[1].disabledReason).toBe("You are signed in as this persona");
    expect(items[0].disabledReason).toBeUndefined();
    expect(personaMenuItems(undefined).every((item) => !item.disabled)).toBe(true);
  });

  it("maps item ids back to personas and ignores every other item", () => {
    expect(personaFromItemId("switch:admin")).toBe("admin");
    expect(personaFromItemId("switch:approver")).toBe("approver");
    expect(personaFromItemId("switch:root")).toBeNull();
    expect(personaFromItemId("signout")).toBeNull();
    expect(personaFromItemId("roles")).toBeNull();
  });
});

describe("takeSwitchedPersona", () => {
  it("returns the stored persona once and ignores unknown values", () => {
    window.sessionStorage.setItem(SWITCHED_PERSONA_KEY, "approver");
    expect(takeSwitchedPersona()).toBe("approver");
    expect(takeSwitchedPersona()).toBeNull();
    window.sessionStorage.setItem(SWITCHED_PERSONA_KEY, "root");
    expect(takeSwitchedPersona()).toBeNull();
    expect(window.sessionStorage.getItem(SWITCHED_PERSONA_KEY)).toBeNull();
  });
});
