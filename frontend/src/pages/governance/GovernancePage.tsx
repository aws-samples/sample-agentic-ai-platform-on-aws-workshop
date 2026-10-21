import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Container from "@cloudscape-design/components/container";
import ContentLayout from "@cloudscape-design/components/content-layout";
import Grid from "@cloudscape-design/components/grid";
import Header from "@cloudscape-design/components/header";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Table from "@cloudscape-design/components/table";
import Toggle from "@cloudscape-design/components/toggle";
import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { useGovernance, useModelGatewayCredentials, useProjectOptions, useSetGatewayMode, useSettings } from "../../api/hooks";
import type { GatewayMode, GovernanceGateway } from "../../api/types";
import { useMe } from "../../auth/MeContext";
import { ConfirmModal } from "../../components/ConfirmModal";
import { CopyText } from "../../components/CopyText";
import { EmptyState } from "../../components/EmptyState";
import { ErrorAlert } from "../../components/ErrorAlert";
import { InfoHeader } from "../../components/InfoPopover";
import { SecretField } from "../../components/SecretField";
import { GenericStatus, ModeBadge } from "../../components/StatusBadge";
import { useConfig } from "../../config/ConfigContext";
import { useBreadcrumbs } from "../../layout/breadcrumbs";
import { useNotifications } from "../../layout/notifications";
import { ApprovedTabs } from "./ApprovedTabs";
import { SettingsForm } from "./SettingsForm";
import { type ApprovedTabId, isApprovedTab } from "./tabIds";

export function GovernancePage() {
  useBreadcrumbs([{ text: "Governance", href: "/governance" }]);
  const config = useConfig();
  const me = useMe();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const governance = useGovernance();
  const settings = useSettings();
  const setMode = useSetGatewayMode();
  const notifications = useNotifications();
  const [pending, setPending] = useState<{ gateway: GovernanceGateway; mode: GatewayMode } | null>(null);
  const canWrite = me.can("governance:write");
  // the LiteLLM admin sign-in is an admin-only route; the password stays masked until the eye icon is clicked
  const credentials = useModelGatewayCredentials(canWrite);
  // the approved inventory's active tab lives in the URL (?tab=), like the project detail page's tabs
  const requestedTab = params.get("tab");
  const activeTab: ApprovedTabId = isApprovedTab(requestedTab) ? requestedTab : "models";
  const setTab = (tab: ApprovedTabId) => {
    const next = new URLSearchParams(params);
    next.set("tab", tab);
    setParams(next, { replace: true });
  };
  // the knowledge bases and tools of the approved tabs: the same admin request that feeds the project form's pick lists.
  // It fans out to several AgentCore and Bedrock calls, so it starts with the first visit of one of those two tabs.
  const [inventoryWanted, setInventoryWanted] = useState(activeTab !== "models");
  if (activeTab !== "models" && !inventoryWanted) {
    setInventoryWanted(true);
  }
  const options = useProjectOptions(inventoryWanted);
  const data = governance.data;
  // the model gateway URL comes from the API (SSM at run time); config.json is the fallback of older deployments
  const gatewayUrl = data?.modelGateway?.url ?? config.modelGatewayUrl ?? "";
  const gatewayDeployed = data ? data.modelGateway?.deployed !== false : true;
  // private landing zone: the proxy's load balancer is internal, so its admin UI cannot be opened from a browser
  const gatewayPrivate = data?.modelGateway?.private === true;

  const applyMode = () => {
    if (!pending?.gateway.gatewayId) {
      return;
    }
    const { gateway, mode } = pending;
    setMode.mutate(
      { gatewayId: gateway.gatewayId as string, mode },
      {
        onSuccess: (result) => {
          notifications.success(`Gateway ${result.name ?? gateway.gatewayId} is now ${result.mode}`, `Policy engine mode updated (status ${result.status ?? "READY"}).`);
          setPending(null);
        },
      },
    );
  };

  return (
    <ContentLayout
      header={
        <Header variant="h1" description="Approved models, knowledge bases and tools, the baseline guardrail, the shared policy engine and its gateways, the catalogue registry, and the platform defaults.">
          Governance
        </Header>
      }
    >
      <SpaceBetween size="l">
        {governance.error ? <ErrorAlert header="Governance data could not be loaded" error={governance.error} onRetry={() => void governance.refetch()} /> : null}

        <ApprovedTabs
          activeTab={activeTab}
          onTabChange={setTab}
          models={data?.models}
          modelsLoading={governance.isLoading}
          modelsEmptyDescription={gatewayDeployed ? "The ModelGateway stack publishes the alias list in SSM and LiteLLM's config." : "The model gateway is not deployed yet (landing zone)."}
          options={options.data}
          optionsLoading={options.isLoading}
          optionsError={options.error}
          onRetryOptions={() => void options.refetch()}
        />

        <ColumnLayout columns={2}>
          <Container
            header={
              <InfoHeader help="Baseline Bedrock guardrail attached to every alias in the model gateway (SharedServices stack)">
                Guardrail
              </InfoHeader>
            }
          >
            {data?.guardrail ? (
              <SpaceBetween size="s">
                <KeyValuePairs
                  columns={2}
                  items={[
                    { label: "Name", value: data.guardrail.name ?? "—" },
                    { label: "Status", value: <GenericStatus status={data.guardrail.status ?? (data.guardrail.summary?.error ? "ERROR" : "UNKNOWN")} /> },
                    { label: "Id", value: <CopyText value={data.guardrail.id} /> },
                    { label: "Version", value: data.guardrail.version ?? "—" },
                  ]}
                />
                {data.guardrail.summary?.error ? (
                  <Box color="text-status-error">GetGuardrail failed: {data.guardrail.summary.error}</Box>
                ) : (
                  <KeyValuePairs
                    columns={1}
                    items={[
                      { label: "Denied topics", value: data.guardrail.summary?.topics?.join(", ") || "none" },
                      {
                        label: "Content filters",
                        value:
                          data.guardrail.summary?.contentFilters?.map((f) => `${f.type} (input ${f.input}, output ${f.output})`).join("; ") || "none",
                      },
                      { label: "Sensitive information", value: data.guardrail.summary?.piiEntities?.map((p) => `${p.type}: ${p.action}`).join("; ") || "none" },
                      { label: "Blocked input message", value: data.guardrail.summary?.blockedInputMessaging ?? "—" },
                      { label: "Blocked output message", value: data.guardrail.summary?.blockedOutputsMessaging ?? "—" },
                    ]}
                  />
                )}
              </SpaceBetween>
            ) : (
              <Box color="text-body-secondary">{governance.isLoading ? "Loading…" : "No guardrail information"}</Box>
            )}
          </Container>
          <SpaceBetween size="l">
            <Container
              header={
                <InfoHeader
                  help="Shared Agent Registry behind the Catalogue screen (its id is stored in the settings with PUT /settings)"
                  actions={<Button onClick={() => navigate("/catalogue")}>Open Catalogue</Button>}
                >
                  Registry
                </InfoHeader>
              }
            >
              {data?.registry?.id ? (
                <KeyValuePairs
                  columns={1}
                  items={[
                    { label: "Registry id", value: <CopyText value={data.registry.id} /> },
                    { label: "ARN", value: <CopyText value={data.registry.arn} max={80} /> },
                  ]}
                />
              ) : (
                <EmptyState title="No registry configured" description="PUT /settings {registryId} points the Catalogue at an Agent Registry." />
              )}
            </Container>
            <Container
              header={
                <InfoHeader
                  help="LiteLLM proxy: teams per project, virtual keys per agent, spend and budgets. The LiteLLM admin UI button in this card's header opens the proxy's console; on the private landing zone it opens only from inside the VPC."
                  actions={
                    // kept on purpose while the proxy is private: the button says where the admin UI is, its tooltip why it will not open from here
                    <Button
                      href={gatewayUrl ? `${gatewayUrl}/ui` : undefined}
                      external
                      loading={governance.isLoading && !gatewayUrl}
                      disabled={!gatewayUrl || gatewayPrivate}
                      disabledReason={
                        !gatewayDeployed
                          ? "The model gateway is not deployed yet (landing zone)."
                          : !gatewayUrl
                            ? "The model gateway URL is not published."
                            : "The proxy is private: this URL opens only from inside the VPC. Copy it from the Admin UI field of this card."
                      }
                    >
                      LiteLLM admin UI
                    </Button>
                  }
                >
                  Model gateway
                </InfoHeader>
              }
            >
              <SpaceBetween size="l">
                <KeyValuePairs
                  columns={1}
                  items={[
                    {
                      label: "Reachability",
                      value: gatewayPrivate ? (
                        <StatusIndicator type="info">Private · reachable only from inside the VPC</StatusIndicator>
                      ) : data?.modelGateway?.private === false && gatewayDeployed ? (
                        "Public (internet-facing load balancer)"
                      ) : (
                        "—"
                      ),
                    },
                    { label: "OpenAI-compatible base URL", value: <CopyText value={gatewayUrl ? `${gatewayUrl}/v1` : null} max={80} /> },
                    {
                      // the header button is the one opener of this URL; the field is for copying it (from inside the VPC when private)
                      label: "Admin UI",
                      value: gatewayUrl ? (
                        <SpaceBetween size="xxs">
                          <CopyText value={`${gatewayUrl}/ui`} max={80} />
                          {gatewayPrivate ? (
                            <Box variant="small" color="text-body-secondary">
                              Opens only {data?.modelGateway?.reachableFrom ?? "from inside the VPC"}; the platform reaches it through the relay and the
                              credentials below stay valid.
                            </Box>
                          ) : null}
                        </SpaceBetween>
                      ) : gatewayDeployed ? (
                        "—"
                      ) : (
                        "not deployed yet (landing zone)"
                      ),
                    },
                  ]}
                />
                <Grid gridDefinition={[{ colspan: { default: 12, xs: 3 } }, { colspan: { default: 12, xs: 9 } }]}>
                  <KeyValuePairs columns={1} items={[{ label: "Username", value: <CopyText value="admin" /> }]} />
                  <SpaceBetween size="xs">
                    <KeyValuePairs
                      columns={1}
                      items={[
                        {
                          label: "Password",
                          value: canWrite ? (
                            <SecretField
                              value={credentials.data?.password}
                              label="LiteLLM admin password"
                              loading={credentials.isLoading}
                              error={credentials.error ? `Could not load the password: ${credentials.error.message}` : null}
                            />
                          ) : (
                            "visible to platform admins"
                          ),
                        },
                      ]}
                    />
                    <Box variant="small" color="text-body-secondary">
                      The password is the LiteLLM master key. You can also find it in Secrets Manager (agent-platform/litellm-master-key).
                    </Box>
                  </SpaceBetween>
                </Grid>
              </SpaceBetween>
            </Container>
          </SpaceBetween>
        </ColumnLayout>

        <Table
          header={
            <InfoHeader
              counter={data ? `(${data.gateways.length})` : undefined}
              help="Cedar policies evaluated by every project gateway. LOG_ONLY records decisions, ENFORCE denies. PUT /settings {policyEngineId} names the policy engine."
              description={
                data
                  ? data.policyEngine?.id
                    ? `Policy engine ${data.policyEngine.id}`
                    : "No policy engine configured yet"
                  : undefined
              }
            >
              Policy engine and gateways
            </InfoHeader>
          }
          items={data?.gateways ?? []}
          loading={governance.isLoading}
          loadingText="Loading gateways"
          trackBy={(g) => `${g.projectId}#${g.env}#${g.gatewayId ?? g.name}`}
          wrapLines
          columnDefinitions={[
            { id: "project", header: "Project", minWidth: 150, cell: (g) => g.projectId, isRowHeader: true },
            { id: "env", header: "Env", cell: (g) => g.env },
            { id: "gateway", header: "Gateway", minWidth: 280, cell: (g) => <CopyText value={g.gatewayId ?? g.name} max={40} /> },
            { id: "status", header: "Status", minWidth: 120, cell: (g) => <GenericStatus status={g.status ?? "UNKNOWN"} label={g.error ? `${g.status}: ${g.error}` : undefined} /> },
            { id: "mode", header: "Mode", minWidth: 120, cell: (g) => <ModeBadge mode={g.mode} /> },
            {
              id: "toggle",
              header: "Enforce",
              minWidth: 150,
              cell: (g) => (
                <Toggle
                  checked={g.mode === "ENFORCE"}
                  disabled={!canWrite || !g.gatewayId || g.status !== "READY"}
                  onChange={({ detail }) => setPending({ gateway: g, mode: detail.checked ? "ENFORCE" : "LOG_ONLY" })}
                  ariaLabel={`Enforce policies on ${g.name ?? g.gatewayId}`}
                  data-testid={`toggle-${g.gatewayId}`}
                >
                  {g.mode === "ENFORCE" ? "ENFORCE" : "LOG_ONLY"}
                </Toggle>
              ),
            },
            { id: "url", header: "MCP URL", cell: (g) => <CopyText value={g.url} max={56} /> },
          ]}
          empty={<EmptyState title="No gateways registered" description="Project gateways appear here once registered with PUT /projects/{id}/resources (type gateway)." />}
        />

        <SettingsForm />

        {settings.data ? (
          <Container header={<InfoHeader help="Ids stored with PUT /settings (read-only here)">Shared resources in the settings</InfoHeader>}>
            <KeyValuePairs
              columns={2}
              items={[
                { label: "Registry id", value: settings.data.registryId ?? "not set (PUT /settings)" },
                { label: "Policy engine id", value: settings.data.policyEngineId ?? "not set (PUT /settings)" },
                { label: "Registry ARN", value: <CopyText value={settings.data.registryArn} max={80} /> },
                { label: "Policy engine ARN", value: <CopyText value={settings.data.policyEngineArn} max={80} /> },
              ]}
            />
          </Container>
        ) : null}
      </SpaceBetween>

      <ConfirmModal
        visible={!!pending}
        header={pending ? `Switch to ${pending.mode} mode?` : ""}
        confirmText={pending ? `Set ${pending.mode}` : "Confirm"}
        loading={setMode.isPending}
        error={setMode.error}
        onConfirm={applyMode}
        onDismiss={() => {
          setMode.reset();
          setPending(null);
        }}
      >
        {pending?.mode === "ENFORCE" ? (
          <Box>
            Tool calls that no Cedar policy permits are denied. UpdateGateway takes about 10 seconds; the gateway stays
            reachable.
          </Box>
        ) : (
          <Box>Decisions are still evaluated and recorded in CloudWatch, but nothing is denied. UpdateGateway takes about 10 seconds.</Box>
        )}
      </ConfirmModal>
    </ContentLayout>
  );
}
