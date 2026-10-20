import { describe, expect, it } from "vitest";

import { ConfigError, cognitoAuthority, cognitoLogoutUrl, loadRuntimeConfig, parseRuntimeConfig } from "./runtimeConfig";

const VALID = {
  apiUrl: "https://api.example.com/",
  region: "us-east-1",
  userPoolId: "us-east-1_abc",
  clientId: "client123",
  authDomain: "https://auth.example.com",
  modelGatewayUrl: "https://gw.example.com",
  uiUrl: "https://ui.example.com",
};

describe("parseRuntimeConfig", () => {
  it("parses the seven keys and strips trailing slashes", () => {
    const config = parseRuntimeConfig(JSON.stringify(VALID));
    expect(config.apiUrl).toBe("https://api.example.com");
    expect(config.userPoolId).toBe("us-east-1_abc");
    expect(config.uiUrl).toBe("https://ui.example.com");
  });

  it("treats the SPA fallback HTML as not configured", () => {
    expect(() => parseRuntimeConfig("<!doctype html><html><body>placeholder</body></html>")).toThrowError(
      expect.objectContaining({ kind: "not-configured" }),
    );
  });

  it("rejects invalid JSON and missing keys", () => {
    expect(() => parseRuntimeConfig("{ nope")).toThrowError(expect.objectContaining({ kind: "invalid" }));
    const { clientId: _dropped, ...withoutClient } = VALID;
    expect(() => parseRuntimeConfig(JSON.stringify(withoutClient))).toThrowError(/missing clientId/);
    expect(() => parseRuntimeConfig("[1, 2]")).toThrowError(ConfigError);
  });
});

describe("loadRuntimeConfig", () => {
  it("maps a failed fetch to unreachable", async () => {
    const failing = (() => Promise.reject(new TypeError("Failed to fetch"))) as unknown as typeof fetch;
    await expect(loadRuntimeConfig(failing)).rejects.toMatchObject({ kind: "unreachable" });
  });

  it("maps a non-2xx answer to unreachable and a JSON body to a config", async () => {
    const notFound = (() => Promise.resolve(new Response("nope", { status: 404 }))) as unknown as typeof fetch;
    await expect(loadRuntimeConfig(notFound)).rejects.toMatchObject({ kind: "unreachable" });
    const ok = (() => Promise.resolve(new Response(JSON.stringify(VALID), { status: 200 }))) as unknown as typeof fetch;
    await expect(loadRuntimeConfig(ok)).resolves.toMatchObject({ region: "us-east-1" });
  });
});

describe("cognito helpers", () => {
  it("builds the issuer and the logout URL", () => {
    expect(cognitoAuthority({ region: "us-east-1", userPoolId: "us-east-1_abc" })).toBe(
      "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_abc",
    );
    expect(cognitoLogoutUrl({ authDomain: "https://auth.example.com", clientId: "c1" }, "http://localhost:5173")).toBe(
      "https://auth.example.com/logout?client_id=c1&logout_uri=http%3A%2F%2Flocalhost%3A5173%2F",
    );
  });
});
