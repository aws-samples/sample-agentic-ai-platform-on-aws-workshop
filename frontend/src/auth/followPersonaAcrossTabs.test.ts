import { describe, expect, it } from "vitest";

import { oidcUserStorageKey, personaChangedInStorage } from "./followPersonaAcrossTabs";

const KEY = oidcUserStorageKey({ region: "us-east-1", userPoolId: "us-east-1_abc", clientId: "client-1" });
const stored = (username: string) => JSON.stringify({ id_token: "x", profile: { sub: "s", "cognito:username": username } });

describe("followPersonaAcrossTabs", () => {
  it("names the localStorage key oidc-client-ts uses for the signed-in user", () => {
    expect(KEY).toBe("oidc.user:https://cognito-idp.us-east-1.amazonaws.com/us-east-1_abc:client-1");
  });

  it("reacts to another persona only: not to a renewal, a sign-in, a sign-out, another key or an unreadable value", () => {
    expect(personaChangedInStorage({ key: KEY, oldValue: stored("developer"), newValue: stored("approver") }, KEY)).toBe(true);
    expect(personaChangedInStorage({ key: KEY, oldValue: stored("developer"), newValue: stored("developer") }, KEY)).toBe(false);
    expect(personaChangedInStorage({ key: KEY, oldValue: null, newValue: stored("admin") }, KEY)).toBe(false);
    expect(personaChangedInStorage({ key: KEY, oldValue: stored("admin"), newValue: null }, KEY)).toBe(false);
    expect(personaChangedInStorage({ key: "oidc.user:other:client", oldValue: stored("developer"), newValue: stored("approver") }, KEY)).toBe(false);
    expect(personaChangedInStorage({ key: KEY, oldValue: "not json", newValue: stored("approver") }, KEY)).toBe(false);
    expect(personaChangedInStorage({ key: KEY, oldValue: stored("developer"), newValue: JSON.stringify({ profile: {} }) }, KEY)).toBe(false);
  });
});
