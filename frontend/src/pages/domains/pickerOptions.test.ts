import { describe, expect, it } from "vitest";

import type { KnowledgeBaseOption, ToolOption } from "../../api/types";
import { knowledgeBaseId, knowledgeBasePickerOptions, lambdaFunctionName, sameValues, selectedOptions, toolPickerOptions, valuesOf } from "./pickerOptions";

const HR = "arn:aws:lambda:us-east-1:123456789012:function:agent-platform-hr-tools";
const PAYROLL = "arn:aws:lambda:us-east-1:123456789012:function:payroll-tools:live";
const tools: ToolOption[] = [
  { arn: HR, name: "agent-platform-hr-tools", description: "AnyCompany HR tools (employee profile, leave, holidays)", sources: ["scenario"] },
  { arn: PAYROLL, name: "payroll-tools", description: null, sources: ["gateway x target payroll"] },
];
const kbs: KnowledgeBaseOption[] = [
  { id: "KB123", arn: "arn:aws:bedrock:us-east-1:123456789012:knowledge-base/KB123", name: "agent-platform-policies", description: "HR docs", status: "ACTIVE", sources: ["bedrock"] },
];

describe("pickerOptions", () => {
  it("derives names from ARNs and ids", () => {
    expect(lambdaFunctionName(HR)).toBe("agent-platform-hr-tools");
    expect(lambdaFunctionName(PAYROLL)).toBe("payroll-tools");
    expect(lambdaFunctionName("not-an-arn")).toBe("not-an-arn");
    expect(knowledgeBaseId("arn:aws:bedrock:us-east-1:123456789012:knowledge-base/KB123")).toBe("KB123");
    expect(knowledgeBaseId("KB123")).toBe("KB123");
  });

  it("renders the name as the label and the ARN / id as the description", () => {
    const options = toolPickerOptions(tools, []);
    expect(options.map((o) => [o.label, o.description])).toEqual([
      ["agent-platform-hr-tools", HR],
      ["payroll-tools", PAYROLL],
    ]);
    expect(options[0].tags?.[0]).toContain("AnyCompany HR tools");
    expect(options[1].tags).toBeUndefined();
    expect(knowledgeBasePickerOptions(kbs, []).map((o) => [o.label, o.description, o.tags])).toEqual([["agent-platform-policies", "KB123", ["HR docs"]]]);
  });

  it("keeps the project's current values selectable even when they are not candidates", () => {
    const unknown = "arn:aws:lambda:us-east-1:123456789012:function:legacy-tool";
    const options = toolPickerOptions(tools, [HR, unknown]);
    expect(options).toHaveLength(3);
    expect(options[2]).toEqual({ value: unknown, label: "legacy-tool", description: unknown, tags: ["not in the pick list"] });
    const kbOptions = knowledgeBasePickerOptions(kbs, ["KB123", "KB999"]);
    expect(kbOptions.map((o) => o.value)).toEqual(["KB123", "KB999"]);
    expect(kbOptions[1].label).toBe("KB999");
  });

  it("does not tag current values as unknown before the candidates have loaded", () => {
    expect(toolPickerOptions(undefined, [HR])[0].tags).toBeUndefined();
    expect(knowledgeBasePickerOptions(undefined, ["KB999"])[0].tags).toBeUndefined();
    expect(toolPickerOptions(tools, ["arn:aws:lambda:us-east-1:123456789012:function:legacy-tool"])[2].tags).toEqual(["not in the pick list"]);
  });

  it("treats re-ordered or re-ticked selections as unchanged", () => {
    expect(sameValues([HR, PAYROLL], [PAYROLL, HR])).toBe(true);
    expect(sameValues([HR, HR], [HR])).toBe(true);
    expect(sameValues([HR], [HR, PAYROLL])).toBe(false);
    expect(sameValues([], [])).toBe(true);
    expect(valuesOf([{ value: HR }, { value: HR }, { value: "" }])).toEqual([HR]);
  });

  it("maps values to option objects in order and back", () => {
    const options = toolPickerOptions(tools, []);
    const selected = selectedOptions(options, [PAYROLL, HR]);
    expect(selected.map((o) => o.label)).toEqual(["payroll-tools", "agent-platform-hr-tools"]);
    expect(valuesOf(selected)).toEqual([PAYROLL, HR]);
    // a knowledge base stored as an ARN still matches its id option
    const kbOptions = knowledgeBasePickerOptions(kbs, []);
    expect(selectedOptions(kbOptions, ["arn:aws:bedrock:us-east-1:123456789012:knowledge-base/KB123"]).map((o) => o.value)).toEqual(["KB123"]);
    expect(selectedOptions(options, ["missing"])).toEqual([]);
  });
});
