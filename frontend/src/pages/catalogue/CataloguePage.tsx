import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import ButtonDropdown from "@cloudscape-design/components/button-dropdown";
import ContentLayout from "@cloudscape-design/components/content-layout";
import Form from "@cloudscape-design/components/form";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import Input from "@cloudscape-design/components/input";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import Modal from "@cloudscape-design/components/modal";
import Select, { type SelectProps } from "@cloudscape-design/components/select";
import SpaceBetween from "@cloudscape-design/components/space-between";
import SplitPanel from "@cloudscape-design/components/split-panel";
import Spinner from "@cloudscape-design/components/spinner";
import Table from "@cloudscape-design/components/table";
import Tabs from "@cloudscape-design/components/tabs";
import Textarea from "@cloudscape-design/components/textarea";
import { keepPreviousData } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { isApiError } from "../../api/client";
import { useCatalogue, useCatalogueRecord, useCatalogueSearch, useSetCatalogueStatus, useSyncCatalogueAgents } from "../../api/hooks";
import type { CatalogueRecord, DecisionStatus } from "../../api/types";
import { useMe } from "../../auth/MeContext";
import { CopyText } from "../../components/CopyText";
import { EmptyState } from "../../components/EmptyState";
import { ErrorAlert } from "../../components/ErrorAlert";
import { InfoFormField } from "../../components/InfoPopover";
import { JsonBlock } from "../../components/JsonBlock";
import { RecordStatus } from "../../components/StatusBadge";
import { useBreadcrumbs } from "../../layout/breadcrumbs";
import { formatErrorContent, useNotifications } from "../../layout/notifications";
import { usePageSplitPanel } from "../../layout/splitPanel";
import { formatDateTime } from "../../utils/format";
import { CATALOGUE_TABS, type CatalogueTabId, categoryLabel, emptyDescription, emptyTitle, isCatalogueTab, rowsForTab, tabLabel } from "./catalogueTabs";

const SEARCH_MAX = 256; // SearchDiscoverableRegistryRecords.searchQuery limit (the API answers 400 above it)
const REASON_MAX = 255; // UpdateRegistryRecordStatus.statusReason limit

const STATUS_OPTIONS: SelectProps.Option[] = [
  { value: "", label: "All statuses" },
  { value: "APPROVED", label: "Approved" },
  { value: "PENDING_APPROVAL", label: "Pending approval" },
  { value: "DRAFT", label: "Draft" },
  { value: "REJECTED", label: "Rejected" },
  { value: "DEPRECATED", label: "Deprecated" },
];

/** Descriptor payloads arrive as JSON strings inside the record; pretty-print them where possible. */
export function expandDescriptors(descriptors: unknown): unknown {
  if (Array.isArray(descriptors)) {
    return descriptors.map(expandDescriptors);
  }
  if (typeof descriptors === "object" && descriptors !== null) {
    return Object.fromEntries(Object.entries(descriptors as Record<string, unknown>).map(([key, value]) => [key, expandDescriptors(value)]));
  }
  if (typeof descriptors === "string") {
    const trimmed = descriptors.trim();
    if ((trimmed.startsWith("{") && trimmed.endsWith("}")) || (trimmed.startsWith("[") && trimmed.endsWith("]"))) {
      try {
        return expandDescriptors(JSON.parse(trimmed));
      } catch {
        return descriptors;
      }
    }
  }
  return descriptors;
}

export function CataloguePage() {
  useBreadcrumbs([{ text: "Catalogue", href: "/catalogue" }]);
  const me = useMe();
  const navigate = useNavigate();
  const notifications = useNotifications();
  const [params, setParams] = useSearchParams();
  const [queryText, setQueryText] = useState("");
  const [submittedQuery, setSubmittedQuery] = useState("");
  const [status, setStatus] = useState("");
  const [selected, setSelected] = useState<CatalogueRecord[]>([]);
  const [decision, setDecision] = useState<DecisionStatus | null>(null);
  const canDecide = me.can("catalogue:decide");
  const syncAgents = useSyncCatalogueAgents();
  const registerAgents = () =>
    syncAgents.mutate(undefined, {
      onSuccess: (result) => {
        const registered = result.agents.filter((a) => a.recordId);
        const failed = result.agents.filter((a) => a.error);
        const skipped = result.agents.filter((a) => a.skipped);
        const lines = result.agents.map((a) => `${a.projectId}: ${a.recordId ? `${a.name} ${a.status ?? ""} (${a.action ?? "kept"})` : (a.error ?? a.skipped ?? "—")}`);
        if (failed.length) {
          notifications.error(`${failed.length} agent(s) could not be registered`, new Error(lines.join("; ")));
        } else {
          notifications.success(`${registered.length} agent record(s) registered${skipped.length ? `, ${skipped.length} project(s) skipped` : ""}`, lines.join("; "));
        }
      },
      onError: (error) => notifications.error("Registering the promoted agents failed", error),
    });
  // the Records tab (All / Models / Knowledge bases / Tools / Agents) lives in the URL like the other tabbed screens
  const requestedTab = params.get("tab");
  const tab: CatalogueTabId = isCatalogueTab(requestedTab) ? requestedTab : "all";
  const setTab = (next: CatalogueTabId) => {
    const nextParams = new URLSearchParams(params);
    nextParams.set("tab", next);
    setParams(nextParams, { replace: true });
  };

  const searching = submittedQuery.trim().length > 0;
  // the previous rows stay while a new status filter or search loads, so the tab counts do not flicker to nothing
  const list = useCatalogue(status || undefined, { enabled: !searching, placeholderData: keepPreviousData });
  const search = useCatalogueSearch(submittedQuery, { placeholderData: keepPreviousData });
  const active = searching ? search : list;
  const rows = useMemo(() => {
    const data = (active.data ?? []) as CatalogueRecord[];
    return [...data].sort((a, b) => (a.updatedAt ?? "") < (b.updatedAt ?? "") ? 1 : -1);
  }, [active.data]);
  const tabRows = useMemo(() => rowsForTab(rows, tab), [rows, tab]);
  const selectedRecord = tabRows.find((r) => r.recordId === selected[0]?.recordId) ?? null;
  const detail = useCatalogueRecord(selectedRecord?.recordId);

  const notConfigured = isApiError(active.error) && active.error.code === "REGISTRY_NOT_CONFIGURED";

  const panel = useMemo(
    () =>
      selectedRecord ? (
        <SplitPanel header={selectedRecord.displayName ?? selectedRecord.name} hidePreferencesButton closeBehavior="collapse">
          <SpaceBetween size="m">
            <KeyValuePairs
              columns={2}
              items={[
                { label: "Name", value: selectedRecord.name },
                { label: "Type", value: selectedRecord.recordType },
                { label: "Category", value: categoryLabel(detail.data?.category ?? selectedRecord.category) },
                { label: "Status", value: <RecordStatus status={detail.data?.status ?? selectedRecord.status} /> },
                { label: "Version", value: selectedRecord.recordVersion ?? "—" },
                { label: "Record id", value: <CopyText value={selectedRecord.recordId} /> },
                { label: "ARN", value: <CopyText value={selectedRecord.recordArn} max={60} /> },
                { label: "Created", value: `${formatDateTime(selectedRecord.createdAt)} by ${selectedRecord.createdBy ?? "—"}` },
                { label: "Updated", value: formatDateTime(detail.data?.updatedAt ?? selectedRecord.updatedAt) },
                { label: "Status reason", value: detail.data?.statusReason ?? selectedRecord.statusReason ?? "—" },
                { label: "Description", value: selectedRecord.description ?? "—" },
              ]}
            />
            <Header variant="h3" description="GET /catalogue/{recordId}: the descriptors (MCP server, tools, custom metadata) also for DRAFT and PENDING_APPROVAL records">
              Descriptors
            </Header>
            {detail.isLoading ? (
              <Box>
                <Spinner /> Loading descriptors…
              </Box>
            ) : detail.error ? (
              <ErrorAlert header="Record could not be loaded" error={detail.error} onRetry={() => void detail.refetch()} />
            ) : (
              <JsonBlock value={expandDescriptors({ descriptors: detail.data?.descriptors ?? {}, customMetadata: detail.data?.customMetadata ?? {} })} label="descriptors" maxHeight={520} />
            )}
          </SpaceBetween>
        </SplitPanel>
      ) : null,
    [selectedRecord, detail.data, detail.isLoading, detail.error, detail.refetch],
  );
  usePageSplitPanel(panel, !!selectedRecord);

  useEffect(() => {
    if (selected.length && !tabRows.some((r) => r.recordId === selected[0].recordId)) {
      setSelected([]); // the record left the list, or the tab changed to one it does not belong to
    }
  }, [tabRows, selected]);

  const submitSearch = () => setSubmittedQuery(queryText.trim().slice(0, SEARCH_MAX));
  const decisionItems = [
    { id: "APPROVED", text: "Approve", disabled: selectedRecord?.status === "APPROVED" },
    { id: "REJECTED", text: "Reject", disabled: selectedRecord?.status === "REJECTED" },
    { id: "DEPRECATED", text: "Deprecate", disabled: selectedRecord?.status === "DEPRECATED" },
  ];
  const recordsTable = renderRecordsTable();

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description="Shared Agent Registry: MCP tool servers, knowledge bases, approved models and the agents promoted to Prod, one tab each. Search covers APPROVED (discoverable) records only; the status filter lists every record."
        >
          Catalogue
        </Header>
      }
    >
      <SpaceBetween size="l">
        {active.error && !notConfigured ? <ErrorAlert header="Catalogue could not be loaded" error={active.error} onRetry={() => void active.refetch()} /> : null}
        <Tabs
          activeTabId={tab}
          onChange={({ detail }) => setTab(isCatalogueTab(detail.activeTabId) ? detail.activeTabId : "all")}
          ariaLabel="Records by category"
          // the same table under every tab (only the active one is mounted): the tab decides which rows it shows
          tabs={CATALOGUE_TABS.map((t) => ({ id: t.id, label: tabLabel(t, active.data ? rows : undefined), content: recordsTable }))}
        />
      </SpaceBetween>
      {selectedRecord && decision ? (
        <DecisionModal
          record={selectedRecord}
          decision={decision}
          onDismiss={() => setDecision(null)}
          onDone={(result) => {
            notifications.success(`${selectedRecord.name} is now ${result.status}`);
            setDecision(null);
            void detail.refetch();
          }}
        />
      ) : null}
    </ContentLayout>
  );

  function renderRecordsTable() {
    return (
      <Table
        header={
          <Header
            variant="h2"
            counter={active.data ? `(${tabRows.length})` : undefined}
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button onClick={() => void active.refetch()} iconName="refresh" ariaLabel="Refresh" />
                {canDecide && tab === "agent" ? (
                  // the promote job's catalogue step for every project at once: agents promoted before it existed, or a failed step
                  <Button onClick={registerAgents} loading={syncAgents.isPending} data-testid="register-agents">
                    Register promoted agents
                  </Button>
                ) : null}
                {canDecide ? (
                  <ButtonDropdown
                    items={decisionItems}
                    disabled={!selectedRecord}
                    onItemClick={({ detail }) => setDecision(detail.id as DecisionStatus)}
                    data-testid="set-status"
                  >
                    Set status
                  </ButtonDropdown>
                ) : null}
              </SpaceBetween>
            }
          >
            Records
          </Header>
        }
        filter={
          <SpaceBetween direction="horizontal" size="s">
            <FormField constraintText={`${queryText.length}/${SEARCH_MAX}`}>
              <Input
                type="search"
                value={queryText}
                placeholder="Search approved records (GET /catalogue/search?q=)"
                onChange={({ detail }) => setQueryText(detail.value.slice(0, SEARCH_MAX))}
                onKeyDown={({ detail }) => {
                  if (detail.key === "Enter") {
                    submitSearch();
                  }
                }}
                clearAriaLabel="Clear search"
                data-testid="catalogue-search"
              />
            </FormField>
            <Button onClick={submitSearch} disabled={!queryText.trim()}>
              Search
            </Button>
            {searching ? (
              <Button
                variant="link"
                onClick={() => {
                  setQueryText("");
                  setSubmittedQuery("");
                }}
              >
                Clear search
              </Button>
            ) : null}
            <Select
              selectedOption={STATUS_OPTIONS.find((o) => o.value === status) ?? STATUS_OPTIONS[0]}
              options={STATUS_OPTIONS}
              disabled={searching}
              onChange={({ detail }) => setStatus(detail.selectedOption.value ?? "")}
            />
          </SpaceBetween>
        }
        items={tabRows}
        loading={active.isLoading}
        loadingText={searching ? "Searching the registry" : "Loading records"}
        trackBy="recordId"
        selectionType="single"
        selectedItems={selected}
        onSelectionChange={({ detail }) => setSelected([...detail.selectedItems])}
        ariaLabels={{ selectionGroupLabel: "Record selection", itemSelectionLabel: (_data, item) => item.name }}
        wrapLines
        columnDefinitions={[
          {
            id: "name",
            header: "Name",
            minWidth: 220,
            isRowHeader: true,
            cell: (r) => (
              <div>
                <Box variant="strong" display="block">
                  {r.displayName ?? r.name}
                </Box>
                <Box variant="small" color="text-body-secondary" display="block">
                  {r.name}
                </Box>
              </div>
            ),
          },
          { id: "type", header: "Type", minWidth: 100, cell: (r) => <Badge color={r.recordType === "MCP" ? "blue" : "grey"}>{r.recordType}</Badge> },
          // 200px keeps "Pending approval" (icon + 16 characters + cell padding) on one line; Description gives the room back
          { id: "status", header: "Status", width: 200, minWidth: 200, cell: (r) => <RecordStatus status={r.status} /> },
          { id: "description", header: "Description", minWidth: 220, cell: (r) => r.description ?? "—" },
          { id: "version", header: "Version", cell: (r) => r.recordVersion ?? "—" },
          { id: "updated", header: "Updated", cell: (r) => formatDateTime(r.updatedAt) },
        ]}
        empty={
          notConfigured ? (
            <EmptyState
              title="No Agent Registry configured"
              description="PUT /settings {registryId} points the Catalogue at an Agent Registry; until then the catalogue is empty."
              action={<Button onClick={() => navigate("/governance")}>Open Governance</Button>}
            />
          ) : active.error ? (
            // a failed load is not an empty registry: the alert above says what happened
            <EmptyState title="Records could not be loaded" description="Retry with the button in the alert above." />
          ) : (
            <EmptyState title={emptyTitle(tab, searching)} description={emptyDescription(tab, status, searching)} />
          )
        }
      />
    );
  }
}

function DecisionModal({
  record,
  decision,
  onDismiss,
  onDone,
}: {
  record: CatalogueRecord;
  decision: DecisionStatus;
  onDismiss: () => void;
  onDone: (result: { status: string }) => void;
}) {
  const mutation = useSetCatalogueStatus();
  const [reason, setReason] = useState("");
  const labels: Record<DecisionStatus, string> = { APPROVED: "Approve", REJECTED: "Reject", DEPRECATED: "Deprecate" };
  return (
    <Modal
      visible
      onDismiss={onDismiss}
      header={`${labels[decision]} ${record.displayName ?? record.name}`}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={mutation.isPending}>
              Cancel
            </Button>
            <Button
              variant="primary"
              loading={mutation.isPending}
              onClick={() => mutation.mutate({ recordId: record.recordId, status: decision, reason: reason.trim() }, { onSuccess: onDone })}
              data-testid="decision-submit"
            >
              {labels[decision]}
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <Form errorText={mutation.error ? formatErrorContent(mutation.error) : undefined}>
        <SpaceBetween size="m">
          <Box>
            POST /catalogue/{record.recordId}/status with status {decision}. Current status: {record.status}.
            {decision === "APPROVED" ? " Approved records become discoverable to search and usable by projects." : ""}
            {decision === "DEPRECATED" ? " Deprecated records stay listed but should not be used by new projects." : ""}
          </Box>
          <InfoFormField label="Reason" help="Stored as the record's status reason" constraintText={`${reason.length}/${REASON_MAX}`}>
            <Textarea value={reason} onChange={({ detail }) => setReason(detail.value.slice(0, REASON_MAX))} rows={3} />
          </InfoFormField>
        </SpaceBetween>
      </Form>
    </Modal>
  );
}
