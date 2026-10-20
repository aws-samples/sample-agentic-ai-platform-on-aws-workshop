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
import Textarea from "@cloudscape-design/components/textarea";
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { isApiError } from "../../api/client";
import { useCatalogue, useCatalogueRecord, useCatalogueSearch, useSetCatalogueStatus } from "../../api/hooks";
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
  const [queryText, setQueryText] = useState("");
  const [submittedQuery, setSubmittedQuery] = useState("");
  const [status, setStatus] = useState("");
  const [selected, setSelected] = useState<CatalogueRecord[]>([]);
  const [decision, setDecision] = useState<DecisionStatus | null>(null);
  const canDecide = me.can("catalogue:decide");

  const searching = submittedQuery.trim().length > 0;
  const list = useCatalogue(status || undefined, { enabled: !searching });
  const search = useCatalogueSearch(submittedQuery);
  const active = searching ? search : list;
  const rows = useMemo(() => {
    const data = (active.data ?? []) as CatalogueRecord[];
    return [...data].sort((a, b) => (a.updatedAt ?? "") < (b.updatedAt ?? "") ? 1 : -1);
  }, [active.data]);
  const selectedRecord = rows.find((r) => r.recordId === selected[0]?.recordId) ?? null;
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
    if (selected.length && !rows.some((r) => r.recordId === selected[0].recordId)) {
      setSelected([]);
    }
  }, [rows, selected]);

  const submitSearch = () => setSubmittedQuery(queryText.trim().slice(0, SEARCH_MAX));
  const decisionItems = [
    { id: "APPROVED", text: "Approve", disabled: selectedRecord?.status === "APPROVED" },
    { id: "REJECTED", text: "Reject", disabled: selectedRecord?.status === "REJECTED" },
    { id: "DEPRECATED", text: "Deprecate", disabled: selectedRecord?.status === "DEPRECATED" },
  ];

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description="Shared Agent Registry: MCP tool servers, knowledge bases and approved models that projects may use. Search covers APPROVED (discoverable) records only; the status filter lists every record."
        >
          Catalogue
        </Header>
      }
    >
      <SpaceBetween size="l">
        {active.error && !notConfigured ? <ErrorAlert header="Catalogue could not be loaded" error={active.error} onRetry={() => void active.refetch()} /> : null}
        <Table
          header={
            <Header
              variant="h2"
              counter={active.data ? `(${rows.length})` : undefined}
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button onClick={() => void active.refetch()} iconName="refresh" ariaLabel="Refresh" />
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
          items={rows}
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
            ) : searching ? (
              <EmptyState title="No approved records match" description="Search covers discoverable (APPROVED) records only; clear the search to list every record by status." />
            ) : (
              <EmptyState title="No records" description={status ? `No records with status ${status}.` : "Records are created in the Agent Registry; POST /catalogue/{id}/status approves, rejects or deprecates them."} />
            )
          }
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
