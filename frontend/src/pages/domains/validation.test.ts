import { describe, expect, it } from "vitest";

import { durationError, idError, nestedErrorPath, numberError } from "./validation";

describe("domain / project form validation", () => {
  it("checks kebab-case ids", () => {
    expect(idError("hr-assistant")).toBeUndefined();
    expect(idError("HR")).toMatch(/kebab-case/);
    expect(idError("")).toBe("id is required");
    expect(idError("1abc")).toMatch(/kebab-case/);
  });

  it("checks numbers and durations", () => {
    expect(numberError("5", { minimum: 0 })).toBeUndefined();
    expect(numberError("-1", { minimum: 0 })).toBe("must be >= 0");
    expect(numberError("1.5", { integer: true })).toBe("must be an integer");
    expect(numberError("", { required: false })).toBeUndefined();
    expect(durationError("7d")).toBeUndefined();
    expect(durationError("weekly")).toMatch(/like 30s/);
  });

  it("parses nested error paths", () => {
    expect(nestedErrorPath("defaultLimits.maxTokens must be a positive integer, got 'x'")).toEqual({
      section: "defaultLimits",
      field: "maxTokens",
    });
    expect(nestedErrorPath("unknown settings keys ['foo']")).toBeNull();
  });
});
