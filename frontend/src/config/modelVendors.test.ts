import { describe, expect, it } from "vitest";

import { vendorForModel } from "./modelVendors";

describe("vendorForModel", () => {
  it("recognises the three families from the alias, case-insensitively", () => {
    expect(vendorForModel("GPT-4o")?.vendor).toBe("openai");
    expect(vendorForModel("claude-sonnet")?.vendor).toBe("anthropic");
    expect(vendorForModel("Nova-Pro")?.vendor).toBe("aws");
  });

  it("falls back to the model id when the alias is opaque", () => {
    expect(vendorForModel("fast-chat", "anthropic.claude-haiku-4-5-v1:0")?.vendor).toBe("anthropic");
    expect(vendorForModel("summariser", "amazon.nova-lite-v1:0")?.label).toBe("AWS");
  });

  it("returns null for unknown families and empty input", () => {
    expect(vendorForModel("llama-3-70b", "meta.llama3-70b-instruct-v1:0")).toBeNull();
    expect(vendorForModel(null, undefined)).toBeNull();
    expect(vendorForModel("")).toBeNull();
  });
});
