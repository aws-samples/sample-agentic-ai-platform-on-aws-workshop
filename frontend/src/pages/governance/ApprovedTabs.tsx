import Alert from "@cloudscape-design/components/alert";
import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Table from "@cloudscape-design/components/table";
import Tabs from "@cloudscape-design/components/tabs";
import type { ReactNode } from "react";

import type { GovernanceModel, KnowledgeBaseOption, ProjectOptions, ToolOption } from "../../api/types";
import { CopyText } from "../../components/CopyText";
import { EmptyState } from "../../components/EmptyState";
import { ErrorAlert } from "../../components/ErrorAlert";
import { InfoHeader } from "../../components/InfoPopover";
import { LiteLLMLogo } from "../../components/LiteLLMLogo";
import { VendorIcon, VendorIconSpacer } from "../../components/VendorIcon";
import { GenericStatus } from "../../components/StatusBadge";
import { vendorForModel } from "../../config/modelVendors";
import { formatPricePerMillion } from "../../utils/format";
import { type ApprovedTabId, isApprovedTab } from "./tabIds";

export interface ApprovedTabsProps {
  /** The page keeps the active tab in the URL (?tab=) like the project detail page, so a refresh or a shared link lands on it. */
  activeTab: ApprovedTabId;
  onTabChange: (tab: ApprovedTabId) => void;
  models: GovernanceModel[] | undefined;
  modelsLoading: boolean;
  /** The empty-state text of the models tab: it depends on whether the model gateway exists yet. */
  modelsEmptyDescription: string;
  /** GET /governance/project-options: the tool Lambdas and knowledge bases the platform knows, with the projects allowing them. */
  options: ProjectOptions | undefined;
  optionsLoading: boolean;
  optionsError: Error | null;
  onRetryOptions: () => void;
}

/** Where a tool or knowledge base was found (scenario stack, live gateway targets, registered resources), one line each. */
function Sources({ sources }: { sources: string[] }) {
  if (!sources.length) {
    return <Box color="text-body-secondary">—</Box>;
  }
  return (
    <SpaceBetween size="xxs">
      {sources.map((source) => (
        <Box key={source}>{source}</Box>
      ))}
    </SpaceBetween>
  );
}

/** The projects whose allow-list names the item: the platform-level "approved for" of a tool or knowledge base. */
function Projects({ projects }: { projects: string[] | undefined }) {
  return projects?.length ? <span>{projects.join(", ")}</span> : <Box color="text-body-secondary">—</Box>;
}


/** The alias with its vendor's mark in front (OpenAI / Anthropic / AWS, see config/modelVendors.ts); unknown vendors keep the column aligned with a blank. */
function ModelAlias({ model }: { model: GovernanceModel }) {
  const vendor = vendorForModel(model.alias, model.modelId, model.litellmModel);
  return (
    <SpaceBetween direction="horizontal" size="xxs" alignItems="center">
      {vendor ? <VendorIcon vendor={vendor.vendor} title={vendor.label} /> : <VendorIconSpacer />}
      <CopyText value={model.alias} />
    </SpaceBetween>
  );
}

function ModelsTable({ models, loading, emptyDescription }: { models: GovernanceModel[] | undefined; loading: boolean; emptyDescription: string }) {
  return (
    <Table
      header={
        <InfoHeader
          counter={models ? `(${models.length})` : undefined}
          help="Model aliases exposed by the model gateway (LiteLLM /model/info, falling back to the SSM list); agents reach models only through these aliases."
        >
          Approved models
        </InfoHeader>
      }
      items={models ?? []}
      loading={loading}
      loadingText="Loading models"
      trackBy="alias"
      wrapLines
      columnDefinitions={[
        { id: "alias", header: "Alias", minWidth: 160, cell: (m) => <ModelAlias model={m} />, isRowHeader: true },
        { id: "modelId", header: "Model id", minWidth: 260, cell: (m) => <CopyText value={m.modelId ?? m.litellmModel} max={56} /> },
        { id: "tier", header: "Tier", cell: (m) => (m.tier ? <Badge color={m.tier === "premium" ? "blue" : "grey"}>{m.tier}</Badge> : "—") },
        { id: "input", header: "Input price", minWidth: 150, cell: (m) => formatPricePerMillion(m.inputCostPerToken) },
        { id: "output", header: "Output price", minWidth: 150, cell: (m) => formatPricePerMillion(m.outputCostPerToken) },
        {
          id: "guardrail",
          header: "Guardrail",
          minWidth: 160,
          // the guardrail's name (resolved by the backend) reads better than its id; the id stays in the tooltip
          cell: (m) =>
            m.guardrail?.id ? (
              <span title={`${m.guardrail.id} v${m.guardrail.version}`}>
                <Badge color="green">{`${m.guardrail.name ?? m.guardrail.id} v${m.guardrail.version}`}</Badge>
              </span>
            ) : (
              <Badge color="red">none</Badge>
            ),
        },
        // { id: "description", header: "Description", cell: (m) => m.description ?? "—" },
        {
          id: "source",
          header: "Source",
          cell: (m) =>
            m.source === "litellm" ? (
              <SpaceBetween direction="horizontal" size="xxs" alignItems="center">
                <LiteLLMLogo />
                <span>LiteLLM</span>
              </SpaceBetween>
            ) : (
              "SSM list (gateway unavailable)"
            ),
        },
      ]}
      empty={<EmptyState title="No approved models" description={emptyDescription} />}
    />
  );
}

/** The empty slot of the two inventory tables: a failed request is not an empty inventory. */
function inventoryEmpty(what: string, failed: boolean, description: string) {
  return failed ? (
    <EmptyState title={`${what} could not be loaded`} description="Retry with the button in the alert above." />
  ) : (
    <EmptyState title={`No ${what.toLowerCase()}`} description={description} />
  );
}

function KnowledgeBasesTable({ rows, loading, failed }: { rows: KnowledgeBaseOption[] | undefined; loading: boolean; failed: boolean }) {
  return (
    <Table
      header={
        <InfoHeader
          counter={rows ? `(${rows.length})` : undefined}
          help="Bedrock knowledge bases the platform knows: the scenario knowledge base (SSM), those listed in the account and the targets registered by projects. A project picks from this list (knowledgeBaseIds) and its agents reach them through the project gateway."
        >
          Approved knowledge bases
        </InfoHeader>
      }
      items={rows ?? []}
      loading={loading}
      loadingText="Loading knowledge bases"
      trackBy="id"
      wrapLines
      columnDefinitions={[
        { id: "name", header: "Name", minWidth: 200, cell: (kb) => kb.name, isRowHeader: true },
        { id: "id", header: "Knowledge base id", minWidth: 180, cell: (kb) => <CopyText value={kb.id} /> },
        { id: "status", header: "Status", minWidth: 120, cell: (kb) => <GenericStatus status={kb.status ?? "UNKNOWN"} /> },
        { id: "description", header: "Description", cell: (kb) => kb.description ?? "—" },
        { id: "projects", header: "Allowed in projects", minWidth: 160, cell: (kb) => <Projects projects={kb.projects} /> },
        { id: "sources", header: "Found in", minWidth: 220, cell: (kb) => <Sources sources={kb.sources} /> },
        { id: "arn", header: "ARN", minWidth: 260, cell: (kb) => <CopyText value={kb.arn} max={56} /> },
      ]}
      empty={inventoryEmpty(
        "Knowledge bases",
        failed,
        "The scenario stack publishes its knowledge base id in SSM; projects register knowledge base targets with PUT /projects/{id}/resources.",
      )}
    />
  );
}

function ToolsTable({ rows, loading, failed }: { rows: ToolOption[] | undefined; loading: boolean; failed: boolean }) {
  return (
    <Table
      header={
        <InfoHeader
          counter={rows ? `(${rows.length})` : undefined}
          help="Tool Lambdas the platform knows: the scenario tools (SSM), the Lambda targets of every AgentCore gateway and the targets registered by projects. A project picks from this list (allowedToolArns); its gateway role may invoke only these."
        >
          Approved tools
        </InfoHeader>
      }
      items={rows ?? []}
      loading={loading}
      loadingText="Loading tools"
      trackBy="arn"
      wrapLines
      columnDefinitions={[
        { id: "name", header: "Name", minWidth: 200, cell: (t) => t.name, isRowHeader: true },
        { id: "arn", header: "Lambda ARN", minWidth: 300, cell: (t) => <CopyText value={t.arn} max={60} /> },
        { id: "description", header: "Description", cell: (t) => t.description ?? "—" },
        { id: "projects", header: "Allowed in projects", minWidth: 160, cell: (t) => <Projects projects={t.projects} /> },
        { id: "sources", header: "Found in", minWidth: 220, cell: (t) => <Sources sources={t.sources} /> },
      ]}
      empty={inventoryEmpty(
        "Tools",
        failed,
        "The scenario stack publishes its tool Lambdas in SSM; gateway Lambda targets and registered gateway_target resources appear here too.",
      )}
    />
  );
}

/**
 * The approved inventory of the platform in three tabs: the model aliases of the model gateway, the knowledge bases and the
 * tool Lambdas a project may be allowed to use. The models table is the one the screen always had; the two others follow its
 * layout and share one request (GET /governance/project-options), whose listing problems are shown above both.
 */
export function ApprovedTabs({ activeTab, onTabChange, models, modelsLoading, modelsEmptyDescription, options, optionsLoading, optionsError, onRetryOptions }: ApprovedTabsProps) {
  const warnings = options?.warnings ?? [];
  // an array rather than a fragment, so SpaceBetween receives the alerts as separate children (an empty array adds no gap)
  const notes: ReactNode[] = [];
  if (optionsError) {
    notes.push(<ErrorAlert key="error" header="The knowledge bases and tools could not be loaded" error={optionsError} onRetry={onRetryOptions} />);
  }
  if (warnings.length) {
    notes.push(
      <Alert key="warnings" type="warning" header="Some sources could not be listed">
        <SpaceBetween size="xxs">
          {warnings.map((warning) => (
            <Box key={warning}>{warning}</Box>
          ))}
        </SpaceBetween>
      </Alert>,
    );
  }
  const failed = !!optionsError;
  return (
    <Tabs
      activeTabId={activeTab}
      onChange={({ detail }) => onTabChange(isApprovedTab(detail.activeTabId) ? detail.activeTabId : "models")}
      ariaLabel="Approved models, knowledge bases and tools"
      tabs={[
        { id: "models", label: "Models", content: <ModelsTable models={models} loading={modelsLoading} emptyDescription={modelsEmptyDescription} /> },
        {
          id: "knowledge-bases",
          label: "Knowledge bases",
          content: (
            <SpaceBetween size="m">
              {notes}
              <KnowledgeBasesTable rows={options?.knowledgeBases} loading={optionsLoading} failed={failed} />
            </SpaceBetween>
          ),
        },
        {
          id: "tools",
          label: "Tools",
          content: (
            <SpaceBetween size="m">
              {notes}
              <ToolsTable rows={options?.tools} loading={optionsLoading} failed={failed} />
            </SpaceBetween>
          ),
        },
      ]}
    />
  );
}
