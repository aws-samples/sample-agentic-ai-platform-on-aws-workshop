import { describe, expect, it } from "vitest";

import type { CatalogueRecord } from "../../api/types";
import { CATALOGUE_TABS, categoryLabel, categoryOf, emptyDescription, emptyTitle, isCatalogueTab, rowsForTab, tabLabel } from "./catalogueTabs";

function record(recordId: string, category: CatalogueRecord["category"]): CatalogueRecord {
  return {
    recordId,
    recordArn: null,
    name: recordId,
    displayName: null,
    description: null,
    recordType: category === "agent" ? "AGENT" : category === "tool" ? "MCP" : "CUSTOM",
    recordVersion: null,
    status: "APPROVED",
    statusReason: null,
    createdAt: null,
    updatedAt: null,
    createdBy: null,
    category,
  };
}

const ROWS = [
  record("hr-assistant-tools", "tool"),
  record("kb-hr-policies", "knowledge-base"),
  record("model-claude-sonnet-5", "model"),
  record("hr-assistant-agent", "agent"),
  record("mystery", undefined), // an older backend, or a CUSTOM record without a kind
];

describe("catalogueTabs", () => {
  it("has the five tabs in the requested order and recognises their ids", () => {
    expect(CATALOGUE_TABS.map((t) => t.label)).toEqual(["All", "Models", "Knowledge bases", "Tools", "Agents"]);
    expect(isCatalogueTab("agent") && isCatalogueTab("all")).toBe(true);
    expect(isCatalogueTab("keys") || isCatalogueTab(null) || isCatalogueTab(undefined)).toBe(false);
  });

  it("puts every record under All and each category under its own tab", () => {
    expect(rowsForTab(ROWS, "all")).toHaveLength(5);
    expect(rowsForTab(ROWS, "tool").map((r) => r.recordId)).toEqual(["hr-assistant-tools"]);
    expect(rowsForTab(ROWS, "knowledge-base").map((r) => r.recordId)).toEqual(["kb-hr-policies"]);
    expect(rowsForTab(ROWS, "model").map((r) => r.recordId)).toEqual(["model-claude-sonnet-5"]);
    expect(rowsForTab(ROWS, "agent").map((r) => r.recordId)).toEqual(["hr-assistant-agent"]);
    expect(categoryOf(ROWS[4])).toBe("other"); // only under All
  });

  it("falls back on the record type when an older backend sends no category", () => {
    expect(categoryOf({ category: undefined, recordType: "AGENT" })).toBe("agent");
    expect(categoryOf({ category: undefined, recordType: "MCP" })).toBe("tool");
    expect(categoryOf({ category: undefined, recordType: "GATEWAY" })).toBe("tool");
    expect(categoryOf({ category: undefined, recordType: "CUSTOM" })).toBe("other"); // the kind lives in the descriptor
    expect(categoryOf({ category: "knowledge-base", recordType: "CUSTOM" })).toBe("knowledge-base");
  });

  it("labels tabs with their counts once the rows are known, and categories for the detail panel", () => {
    expect(tabLabel(CATALOGUE_TABS[0], ROWS)).toBe("All (5)");
    expect(tabLabel(CATALOGUE_TABS[4], ROWS)).toBe("Agents (1)");
    expect(tabLabel(CATALOGUE_TABS[1], undefined)).toBe("Models");
    expect(categoryLabel("knowledge-base")).toBe("Knowledge base");
    expect(categoryLabel("agent")).toBe("Agent");
    expect(categoryLabel(undefined)).toBe("—");
  });

  it("explains an empty tab", () => {
    expect(emptyDescription("agent", "", false)).toMatch(/promote/);
    expect(emptyDescription("model", "DRAFT", false)).toBe("No model records with status DRAFT.");
    expect(emptyDescription("all", "DRAFT", false)).toBe("No records with status DRAFT.");
    expect(emptyDescription("tool", "", false)).toMatch(/MCP records as tools/);
    expect(emptyDescription("all", "", true)).toMatch(/APPROVED/);
    expect(emptyDescription("model", "", true)).toMatch(/The All tab shows every hit/);
    expect(emptyDescription("all", "", true)).not.toMatch(/All tab/);
  });

  it("titles an empty tab (the e2e expects exactly 'No records' on the All tab)", () => {
    expect(emptyTitle("all", false)).toBe("No records");
    expect(emptyTitle("knowledge-base", false)).toBe("No knowledge bases");
    expect(emptyTitle("agent", false)).toBe("No agents");
    expect(emptyTitle("all", true)).toBe("No approved records match");
    expect(emptyTitle("model", true)).toBe("No approved models match");
  });
});
