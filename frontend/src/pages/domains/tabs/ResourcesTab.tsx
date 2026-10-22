import Box from "@cloudscape-design/components/box";
import Container from "@cloudscape-design/components/container";
import Popover from "@cloudscape-design/components/popover";
import SegmentedControl from "@cloudscape-design/components/segmented-control";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Table from "@cloudscape-design/components/table";
import { useState } from "react";

import { useResources } from "../../../api/hooks";
import type { Env, Resource } from "../../../api/types";
import { CopyText } from "../../../components/CopyText";
import { EmptyState } from "../../../components/EmptyState";
import { ErrorAlert } from "../../../components/ErrorAlert";
import { InfoHeader } from "../../../components/InfoPopover";
import { JsonBlock } from "../../../components/JsonBlock";
import { TypeBadge } from "../../../components/StatusBadge";
import { formatDateTime, truncate } from "../../../utils/format";

const TYPE_ORDER = [
  "gateway",
  "gateway_target",
  "policy",
  "memory",
  "knowledge_base",
  "tool",
  "credential_provider",
  "harness",
  "harness_endpoint",
  "online_evaluation",
  "registry_record",
];

const EMPTY_HINTS: Record<Env, string> = {
  dev: "Registered with PUT /projects/{id}/resources: the Dev gateway and its targets, the Cedar policies, the catalogue record, the model key's credential provider and the memory; the deploy job adds the Dev harness.",
  prod: "Registered with PUT /projects/{id}/resources: the Prod gateway. The first promotion adds the Prod harness, its endpoint and the online evaluation config.",
};

function sortResources(rows: Resource[]): Resource[] {
  return [...rows].sort((a, b) => {
    const ta = TYPE_ORDER.indexOf(a.type);
    const tb = TYPE_ORDER.indexOf(b.type);
    return (ta === -1 ? 99 : ta) - (tb === -1 ? 99 : tb) || a.name.localeCompare(b.name);
  });
}

/** Up to three attribute entries inline; the full JSON in a popover. */
function Attributes({ attributes }: { attributes: Record<string, unknown> }) {
  const entries = Object.entries(attributes ?? {});
  if (!entries.length) {
    return <Box color="text-body-secondary">—</Box>;
  }
  const summary = entries
    .slice(0, 3)
    .map(([key, value]) => `${key}: ${truncate(typeof value === "string" ? value : JSON.stringify(value), 32)}`)
    .join(" · ");
  return (
    <Popover
      size="large"
      position="left"
      header="Attributes"
      triggerType="text"
      content={<JsonBlock value={attributes} maxHeight={320} label="attributes" />}
    >
      {summary}
      {entries.length > 3 ? ` · +${entries.length - 3}` : ""}
    </Popover>
  );
}

export function ResourcesTab({ projectId }: { projectId: string }) {
  const resources = useResources(projectId);
  const [envFilter, setEnvFilter] = useState<"all" | Env>("all");
  const envs: Env[] = envFilter === "all" ? ["dev", "prod"] : [envFilter];

  if (resources.error) {
    return <ErrorAlert header="Resources could not be loaded" error={resources.error} onRetry={() => void resources.refetch()} />;
  }
  return (
    <SpaceBetween size="l">
      <Box float="right">
        <SegmentedControl
          selectedId={envFilter}
          options={[
            { id: "all", text: "All" },
            { id: "dev", text: "Dev" },
            { id: "prod", text: "Prod" },
          ]}
          onChange={({ detail }) => setEnvFilter(detail.selectedId as "all" | Env)}
        />
      </Box>
      {envs.map((env) => {
        const rows = sortResources((resources.data ?? []).filter((r) => r.env === env));
        return (
          <Container
            key={env}
            header={
              <InfoHeader
                counter={`(${rows.length})`}
                label={`${env === "dev" ? "Dev" : "Prod"} resources`}
                help="References registered with PUT /projects/{id}/resources and by the deploy / promote jobs"
              >
                {env === "dev" ? "Dev" : "Prod"} resources
              </InfoHeader>
            }
          >
            <Table
              variant="embedded"
              items={rows}
              loading={resources.isLoading}
              loadingText="Loading resources"
              trackBy={(r) => `${r.env}#${r.type}#${r.name}`}
              wrapLines
              columnDefinitions={[
                { id: "type", header: "Type", cell: (r) => <TypeBadge type={r.type} /> },
                { id: "name", header: "Name", minWidth: 200, cell: (r) => r.name, isRowHeader: true },
                { id: "id", header: "Id", minWidth: 200, cell: (r) => <CopyText value={r.id} max={40} /> },
                { id: "arn", header: "ARN / URL", minWidth: 320, cell: (r) => <CopyText value={r.arn ?? r.url} max={64} /> },
                { id: "attributes", header: "Attributes", cell: (r) => <Attributes attributes={r.attributes} /> },
                {
                  id: "registered",
                  header: "Registered",
                  cell: (r) => (
                    <span>
                      {formatDateTime(r.updatedAt ?? r.registeredAt)} by {r.updatedBy ?? r.registeredBy}
                    </span>
                  ),
                },
              ]}
              empty={<EmptyState title={`No ${env} resources registered yet`} description={EMPTY_HINTS[env]} />}
            />
          </Container>
        );
      })}
    </SpaceBetween>
  );
}
