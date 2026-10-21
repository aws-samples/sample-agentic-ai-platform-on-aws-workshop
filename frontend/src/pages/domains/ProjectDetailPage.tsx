import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import ContentLayout from "@cloudscape-design/components/content-layout";
import Header from "@cloudscape-design/components/header";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Spinner from "@cloudscape-design/components/spinner";
import Tabs from "@cloudscape-design/components/tabs";
import { useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";

import { useDomains, useProject } from "../../api/hooks";
import { useMe } from "../../auth/MeContext";
import { CopyText } from "../../components/CopyText";
import { ErrorAlert } from "../../components/ErrorAlert";
import { GenericStatus } from "../../components/StatusBadge";
import { useBreadcrumbs } from "../../layout/breadcrumbs";
import { formatBudgetDuration, formatDateTime, formatNumber, formatUsd } from "../../utils/format";
import { DeleteProjectModal, EditProjectModal } from "./ProjectModals";
import { lambdaFunctionName } from "./pickerOptions";
import { BlueprintsTab } from "./tabs/BlueprintsTab";
import { KeysTab } from "./tabs/KeysTab";
import { ReleasesTab } from "./tabs/ReleasesTab";
import { ResourcesTab } from "./tabs/ResourcesTab";

const TAB_IDS = ["resources", "keys", "blueprints", "releases"] as const;

export function ProjectDetailPage() {
  const { projectId = "" } = useParams();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const me = useMe();
  const project = useProject(projectId);
  const domains = useDomains();
  const [editOpen, setEditOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  useBreadcrumbs([
    { text: "Domains and projects", href: "/domains" },
    { text: project.data?.name || projectId, href: `/projects/${projectId}` },
  ]);

  const requestedTab = params.get("tab");
  const activeTab = (TAB_IDS as ReadonlyArray<string>).includes(requestedTab ?? "") ? (requestedTab as string) : "resources";
  const setTab = (tab: string) => {
    const next = new URLSearchParams(params);
    next.set("tab", tab);
    setParams(next, { replace: true });
  };

  if (project.isLoading) {
    return (
      <ContentLayout header={<Header variant="h1">{projectId}</Header>}>
        <Spinner /> Loading project…
      </ContentLayout>
    );
  }
  if (project.error || !project.data) {
    return (
      <ContentLayout header={<Header variant="h1">{projectId}</Header>}>
        <ErrorAlert header={`Project ${projectId} could not be loaded`} error={project.error ?? new Error("not found")} onRetry={() => void project.refetch()} />
      </ContentLayout>
    );
  }
  const p = project.data;
  const domainName = domains.data?.find((d) => d.domainId === p.domainId)?.name ?? p.domainId;
  const canWrite = me.can("projects:write");

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description={p.description || "No description"}
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={() => navigate(`/?project=${encodeURIComponent(p.projectId)}`)}>View in Overview</Button>
              {canWrite ? (
                <Button onClick={() => setEditOpen(true)}>Edit</Button>
              ) : null}
              {canWrite ? (
                <Button onClick={() => setDeleteOpen(true)}>Delete</Button>
              ) : null}
            </SpaceBetween>
          }
        >
          {p.name} <GenericStatus status={p.status} />
        </Header>
      }
    >
      <SpaceBetween size="l">
        <Container header={<Header variant="h2">Project</Header>}>
          <KeyValuePairs
            columns={4}
            items={[
              { label: "Project id", value: <CopyText value={p.projectId} /> },
              { label: "Domain", value: domainName },
              { label: "Allowed models", value: (p.litellm?.models ?? []).join(", ") || "—" },
              {
                label: "Project cap",
                value: p.litellm ? `${formatUsd(p.litellm.maxBudget)} per ${formatBudgetDuration(p.litellm.budgetDuration)}` : "—",
              },
              { label: "Rate limits", value: p.litellm ? `${formatNumber(p.litellm.tpmLimit)} tokens/min, ${formatNumber(p.litellm.rpmLimit)} requests/min` : "—" },
              { label: "LiteLLM team", value: p.litellm?.teamId ?? "—" },
              { label: "Created", value: `${formatDateTime(p.createdAt)} by ${p.createdBy}` },
              { label: "Updated", value: p.updatedAt ? `${formatDateTime(p.updatedAt)} by ${p.updatedBy ?? "—"}` : "—" },
              {
                label: "Allowed tools",
                value: p.allowedToolArns?.length ? (
                  <SpaceBetween size="xs">
                    {p.allowedToolArns.map((arn) => (
                      <div key={arn}>
                        <Box variant="strong" display="block">
                          {lambdaFunctionName(arn)}
                        </Box>
                        <CopyText value={arn} max={72} />
                      </div>
                    ))}
                  </SpaceBetween>
                ) : (
                  "none"
                ),
              },
              {
                label: "Knowledge bases",
                value: p.knowledgeBaseIds?.length ? (
                  <SpaceBetween size="xxs">
                    {p.knowledgeBaseIds.map((id) => (
                      <CopyText key={id} value={id} />
                    ))}
                  </SpaceBetween>
                ) : (
                  "none"
                ),
              },
              { label: "Dev roles", value: <RoleArns roles={p.roles?.dev} /> },
              { label: "Prod roles", value: <RoleArns roles={p.roles?.prod} /> },
            ]}
          />
        </Container>
        <Tabs
          activeTabId={activeTab}
          onChange={({ detail }) => setTab(detail.activeTabId)}
          tabs={[
            { id: "resources", label: "Resources", content: <ResourcesTab projectId={p.projectId} /> },
            { id: "keys", label: "Agents", content: <KeysTab project={p} /> },
            { id: "blueprints", label: "Blueprints", content: <BlueprintsTab project={p} /> },
            { id: "releases", label: "Releases", content: <ReleasesTab projectId={p.projectId} /> },
          ]}
        />
      </SpaceBetween>
      <EditProjectModal project={p} visible={editOpen} onDismiss={() => setEditOpen(false)} />
      <DeleteProjectModal project={p} visible={deleteOpen} onDismiss={() => setDeleteOpen(false)} onDeleted={() => navigate("/domains")} />
    </ContentLayout>
  );
}

function RoleArns({ roles }: { roles: { agentRoleArn: string; gatewayRoleArn: string } | undefined }) {
  if (!roles) {
    return <>—</>;
  }
  return (
    <SpaceBetween size="xxs">
      <CopyText value={roles.agentRoleArn} max={72} />
      <CopyText value={roles.gatewayRoleArn} max={72} />
    </SpaceBetween>
  );
}
