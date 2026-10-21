import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it } from "vitest";

import type { GovernanceModel, ProjectOptions } from "../../api/types";
import { ApprovedTabs, type ApprovedTabsProps } from "./ApprovedTabs";
import { type ApprovedTabId, isApprovedTab } from "./tabIds";

afterEach(cleanup);

const MODELS: GovernanceModel[] = [
  {
    alias: "claude-sonnet-5",
    modelId: "us.anthropic.claude-sonnet-5",
    provider: "bedrock",
    tier: "premium",
    description: "Anthropic Claude Sonnet 5",
    inputCostPerToken: 0.000003,
    outputCostPerToken: 0.000015,
    guardrail: { id: "g-1", version: "1", name: "hr-baseline" },
    source: "litellm",
  },
];

const OPTIONS: ProjectOptions = {
  tools: [
    {
      arn: "arn:aws:lambda:us-east-1:123456789012:function:agent-platform-hr-tools",
      name: "agent-platform-hr-tools",
      description: "AnyCompany HR tools (employee profile, leave, holidays)",
      sources: ["scenario", "gateway hr-assistant-dev-gw target hrtools"],
      projects: ["hr-assistant"],
      inventoried: true,
    },
    {
      arn: "arn:aws:lambda:us-east-1:123456789012:function:payroll-tools",
      name: "payroll-tools",
      description: null,
      sources: ["gateway invoice-helper-dev-gw target payroll"],
      projects: [],
      inventoried: true,
    },
    // what an older backend sends: no projects, no inventoried flag
    { arn: "arn:aws:lambda:us-east-1:123456789012:function:legacy-tool", name: "legacy-tool", description: null, sources: ["project allow-list only"] },
  ],
  knowledgeBases: [
    {
      id: "KB123",
      arn: "arn:aws:bedrock:us-east-1:123456789012:knowledge-base/KB123",
      name: "agent-platform-policies",
      description: "HR docs",
      status: "ACTIVE",
      sources: ["bedrock", "scenario"],
      projects: ["hr-assistant", "payroll-helper"],
      inventoried: true,
    },
  ],
  warnings: ["AgentCore gateway targets could not be listed (not authorized)"],
};

/** The page's part of the contract: the active tab is state outside the component (the URL on the real page). */
function Harness({ initialTab = "models", ...overrides }: Partial<Omit<ApprovedTabsProps, "activeTab" | "onTabChange">> & { initialTab?: ApprovedTabId }) {
  const [tab, setTab] = useState<ApprovedTabId>(initialTab);
  return (
    <ApprovedTabs
      activeTab={tab}
      onTabChange={setTab}
      models={MODELS}
      modelsLoading={false}
      modelsEmptyDescription="The ModelGateway stack publishes the alias list."
      options={OPTIONS}
      optionsLoading={false}
      optionsError={null}
      onRetryOptions={() => undefined}
      {...overrides}
    />
  );
}

describe("ApprovedTabs", () => {
  it("opens on the approved models table and switches to the knowledge bases and the tools", () => {
    render(<Harness />);
    expect(screen.getByRole("tab", { name: "Models" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("heading", { level: 2, name: /Approved models/ })).toBeInTheDocument();
    expect(screen.getAllByText("claude-sonnet-5").length).toBeGreaterThan(0);
    expect(screen.getByText("hr-baseline v1")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Tools" }));
    expect(screen.getByRole("heading", { level: 2, name: /Approved tools/ })).toBeInTheDocument();
    expect(screen.getByText("agent-platform-hr-tools")).toBeInTheDocument();
    expect(screen.getByText("gateway hr-assistant-dev-gw target hrtools")).toBeInTheDocument();
    expect(screen.getByText("hr-assistant")).toBeInTheDocument(); // the project allow-listing the tool
    expect(screen.getByText("project allow-list only")).toBeInTheDocument(); // a stale allow-list entry is still shown here
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(2); // payroll-tools and legacy-tool: allowed nowhere
    expect(screen.getByText("Some sources could not be listed")).toBeInTheDocument(); // the backend's listing warnings
    expect(screen.getByText("AgentCore gateway targets could not be listed (not authorized)")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Knowledge bases" }));
    expect(screen.getByRole("heading", { level: 2, name: /Approved knowledge bases/ })).toBeInTheDocument();
    expect(screen.getByText("agent-platform-policies")).toBeInTheDocument();
    expect(screen.getByText("hr-assistant, payroll-helper")).toBeInTheDocument();
    expect(screen.getAllByText("KB123").length).toBeGreaterThan(0);
    expect(screen.queryByRole("heading", { level: 2, name: /Approved models/ })).not.toBeInTheDocument();
  });

  it("shows the loading, empty and error states of the shared request on the new tabs", () => {
    render(<Harness options={undefined} optionsLoading initialTab="tools" />);
    expect(screen.getByText("Loading tools")).toBeInTheDocument();
    cleanup();
    render(<Harness options={{ tools: [], knowledgeBases: [], warnings: [] }} initialTab="tools" />);
    expect(screen.getByText("No tools")).toBeInTheDocument();
    cleanup();
    render(<Harness options={undefined} optionsError={new Error("boom")} initialTab="knowledge-bases" />);
    expect(screen.getByText("The knowledge bases and tools could not be loaded")).toBeInTheDocument();
    expect(screen.getByText("Knowledge bases could not be loaded")).toBeInTheDocument(); // not "No knowledge bases"
    expect(screen.queryByText("No knowledge bases")).not.toBeInTheDocument();
  });

  it("keeps the models tab independent of the pick-list request", () => {
    render(<Harness models={[]} options={undefined} optionsLoading optionsError={new Error("boom")} />);
    expect(screen.getByText("No approved models")).toBeInTheDocument();
    expect(screen.getByText("The ModelGateway stack publishes the alias list.")).toBeInTheDocument();
    expect(screen.queryByText("The knowledge bases and tools could not be loaded")).not.toBeInTheDocument();
    expect(screen.queryByText(/could not be listed/)).not.toBeInTheDocument();
  });

  it("accepts only its three tab ids from the URL", () => {
    expect(isApprovedTab("tools") && isApprovedTab("knowledge-bases") && isApprovedTab("models")).toBe(true);
    expect(isApprovedTab("keys") || isApprovedTab(null) || isApprovedTab(undefined)).toBe(false);
  });
});
