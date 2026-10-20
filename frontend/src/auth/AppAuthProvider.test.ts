import { describe, expect, it } from "vitest";

import { currentReturnTo, safeReturnTo } from "./AppAuthProvider";

const ORIGIN = "https://ui.example.test";

describe("safeReturnTo", () => {
  it("accepts same-origin paths only", () => {
    expect(safeReturnTo({ returnTo: "/governance?x=1" }, ORIGIN)).toBe("/governance?x=1");
    expect(safeReturnTo({ returnTo: "/releases/01ABC#history" }, ORIGIN)).toBe("/releases/01ABC#history");
    expect(safeReturnTo({ returnTo: "//evil.example.com" }, ORIGIN)).toBe("/");
    expect(safeReturnTo({ returnTo: "/\\evil.example.com" }, ORIGIN)).toBe("/");
    expect(safeReturnTo({ returnTo: "/\\/evil.example.com/x" }, ORIGIN)).toBe("/");
    expect(safeReturnTo({ returnTo: "https://evil.example.com" }, ORIGIN)).toBe("/");
    expect(safeReturnTo({ returnTo: "javascript:alert(1)" }, ORIGIN)).toBe("/");
    expect(safeReturnTo(undefined, ORIGIN)).toBe("/");
    expect(safeReturnTo("string state", ORIGIN)).toBe("/");
  });

  it("strips stale OIDC callback parameters so a failed callback does not re-arm itself", () => {
    expect(safeReturnTo({ returnTo: "/?code=OLD&state=OLD" }, ORIGIN)).toBe("/");
    expect(safeReturnTo({ returnTo: "/releases?projectId=hr-assistant&code=x&state=y&error=access_denied&error_description=no" }, ORIGIN)).toBe(
      "/releases?projectId=hr-assistant",
    );
    expect(safeReturnTo({ returnTo: "/?project=hr-assistant&iss=https%3A%2F%2Fcognito&env=prod" }, ORIGIN)).toBe("/?project=hr-assistant&env=prod");
  });

  it("captures the current page without callback parameters", () => {
    window.history.replaceState({}, "", "/playground?project=hr-assistant&code=abc&state=def#x");
    expect(currentReturnTo()).toBe("/playground?project=hr-assistant#x");
    window.history.replaceState({}, "", "/");
  });
});
