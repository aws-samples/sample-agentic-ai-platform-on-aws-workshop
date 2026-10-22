import { describe, expect, it } from "vitest";

import type { Resource } from "../../api/types";
import { budgetDetails, expectedSeconds, harnessByEnv, isValidSessionId, newSessionId, usageSummary } from "./playgroundHelpers";

const resource = (over: Partial<Resource>): Resource => ({
  projectId: "lt-hr-assistant",
  env: "dev",
  type: "harness",
  name: "lt_hr_assistant_dev",
  arn: "arn:aws:bedrock-agentcore:us-east-1:990851549965:harness/lt_hr_assistant_dev-abc",
  id: "lt_hr_assistant_dev-abc",
  url: null,
  attributes: { version: "2", blueprintVersion: 2, model: "claude-sonnet-5", endpoint: "DEFAULT" },
  registeredAt: "2026-10-09T03:00:00Z",
  registeredBy: "developer",
  ...over,
});

describe("session ids", () => {
  it("generates ids that satisfy the InvokeHarness rule", () => {
    const id = newSessionId();
    expect(id.endsWith("-playground")).toBe(true);
    expect(id.length).toBe(47);
    expect(isValidSessionId(id)).toBe(true);
    expect(newSessionId()).not.toBe(id);
  });

  it("rejects ids the API would refuse", () => {
    expect(isValidSessionId("short")).toBe(false);
    expect(isValidSessionId("-" + "a".repeat(40))).toBe(false);
    expect(isValidSessionId("a".repeat(33))).toBe(true);
    expect(isValidSessionId("a".repeat(101))).toBe(false);
    expect(isValidSessionId("my session with spaces 123456789012345")).toBe(false);
  });
});

describe("harnessByEnv", () => {
  it("finds the registered harness per environment and names the endpoint the playground reaches", () => {
    const rows = [resource({}), resource({ env: "prod", name: "lt_hr_assistant_prod", attributes: { version: "1", blueprintVersion: 2, endpoint: "DEFAULT" } }), resource({ type: "gateway", name: "gw" })];
    const by = harnessByEnv(rows);
    expect(by.dev).toMatchObject({ name: "lt_hr_assistant_dev", version: "2", blueprintVersion: "2", model: "claude-sonnet-5", qualifier: "DEFAULT" });
    expect(by.prod).toMatchObject({ name: "lt_hr_assistant_prod", version: "1", qualifier: "DEFAULT", model: null });
  });

  it("ignores harness rows without an ARN and copes with no resources", () => {
    expect(harnessByEnv([resource({ arn: null })]).dev).toBeNull();
    expect(harnessByEnv(undefined)).toEqual({ dev: null, prod: null });
  });
});

describe("usage and budget parsing", () => {
  it("summarises token usage in either naming", () => {
    expect(usageSummary({ inputTokens: 1200, outputTokens: 300, totalTokens: 1500 })).toEqual({ input: 1200, output: 300, total: 1500 });
    expect(usageSummary({ input_tokens: 10, output_tokens: 5 })).toEqual({ input: 10, output: 5, total: 15 });
    expect(usageSummary(null)).toEqual({ input: null, output: null, total: null });
  });

  it("extracts cap, spend and reset time from a BUDGET_EXCEEDED error only", () => {
    const details = budgetDetails({
      code: "BUDGET_EXCEEDED",
      message: "Budget has been exceeded! Key=lt-hr-assistant-dev-claude-sonnet-5 Current cost: 0.0446, Max budget: 0.01",
      details: { alias: "lt-hr-assistant-dev-claude-sonnet-5", maxBudget: 0.01, spend: 0.0446512, budgetResetAt: "2026-10-12T00:00:00Z", currentCost: 0.0446512 },
    });
    expect(details).toMatchObject({ alias: "lt-hr-assistant-dev-claude-sonnet-5", maxBudget: 0.01, spend: 0.0446512, budgetResetAt: "2026-10-12T00:00:00Z" });
    expect(budgetDetails({ code: "INVOKE_FAILED", message: "boom" })).toBeNull();
    expect(budgetDetails(null)).toBeNull();
  });

  it("tells the user how long a turn takes", () => {
    expect(expectedSeconds(0)).toContain("25-35 s");
    expect(expectedSeconds(1)).toContain("4 s");
  });
});
