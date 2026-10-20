import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Form from "@cloudscape-design/components/form";
import FormField from "@cloudscape-design/components/form-field";
import Input from "@cloudscape-design/components/input";
import Modal from "@cloudscape-design/components/modal";
import Select, { type SelectProps } from "@cloudscape-design/components/select";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Table from "@cloudscape-design/components/table";
import { useEffect, useState } from "react";

import { useCreateKey, useKeys, useSettings, useSpend, useUpdateKey } from "../../../api/hooks";
import type { AgentKey, Env, Project, SpendKey } from "../../../api/types";
import { useMe } from "../../../auth/MeContext";
import { ConfirmModal } from "../../../components/ConfirmModal";
import { CopyText } from "../../../components/CopyText";
import { EmptyState } from "../../../components/EmptyState";
import { ErrorAlert } from "../../../components/ErrorAlert";
import { INFO_SPACER, InfoFormField, InfoHeader } from "../../../components/InfoPopover";
import { SpendBar } from "../../../components/SpendBar";
import { EnvBadge, KeyStatus } from "../../../components/StatusBadge";
import { formatErrorContent, useNotifications } from "../../../layout/notifications";
import { formatBudgetDuration, formatDateTime, formatRelative, formatUsd } from "../../../utils/format";
import { durationError, numberError } from "../validation";

interface KeyRow extends AgentKey {
  spendInfo?: SpendKey;
}

export function KeysTab({ project }: { project: Project }) {
  const me = useMe();
  const keys = useKeys(project.projectId);
  const spend = useSpend(project.projectId);
  const [selected, setSelected] = useState<KeyRow[]>([]);
  const [createOpen, setCreateOpen] = useState(false);
  const [raiseCapKey, setRaiseCapKey] = useState<KeyRow | null>(null);
  const [statusChange, setStatusChange] = useState<{ key: KeyRow; status: "active" | "suspended" } | null>(null);
  const update = useUpdateKey();
  const notifications = useNotifications();

  const rows: KeyRow[] = (keys.data ?? []).map((key) => ({
    ...key,
    spendInfo: spend.data?.keys.find((s) => s.alias === key.alias),
  }));
  const selectedKey = rows.find((r) => r.alias === selected[0]?.alias) ?? null;
  const canCreate = me.can("keys:create");
  const canManage = me.can("keys:manage");

  const applyStatus = () => {
    if (!statusChange) {
      return;
    }
    const { key, status } = statusChange;
    update.mutate(
      { projectId: project.projectId, env: key.env, model: key.model, update: { status } },
      {
        onSuccess: () => {
          notifications.success(`Key ${key.alias} ${status === "suspended" ? "suspended (blocked in LiteLLM)" : "resumed"}`);
          setStatusChange(null);
        },
      },
    );
  };

  return (
    <SpaceBetween size="l">
      {keys.error ? <ErrorAlert header="Keys could not be loaded" error={keys.error} onRetry={() => void keys.refetch()} /> : null}
      {spend.error ? <ErrorAlert header="Spend could not be loaded (the key list still shows the configured caps)" error={spend.error} /> : null}
      <Table
        header={
          <InfoHeader
            counter={keys.data ? `(${keys.data.length})` : undefined}
            help="One LiteLLM virtual key per agent, environment and model; its raw value lives only in AgentCore Identity (the credential provider)."
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  iconName="refresh"
                  ariaLabel="Refresh keys and spend"
                  onClick={() => {
                    void keys.refetch();
                    void spend.refetch();
                  }}
                  loading={keys.isFetching || spend.isFetching}
                  data-testid="keys-refresh"
                />
                {canManage ? (
                  <Button disabled={!selectedKey} onClick={() => selectedKey && setRaiseCapKey(selectedKey)} data-testid="raise-cap">
                    Set cap
                  </Button>
                ) : null}
                {canManage ? (
                  <Button
                    disabled={!selectedKey || selectedKey.status === "suspended"}
                    onClick={() => selectedKey && setStatusChange({ key: selectedKey, status: "suspended" })}
                  >
                    Suspend
                  </Button>
                ) : null}
                {canManage ? (
                  <Button
                    disabled={!selectedKey || selectedKey.status !== "suspended"}
                    onClick={() => selectedKey && setStatusChange({ key: selectedKey, status: "active" })}
                  >
                    Resume
                  </Button>
                ) : null}
                {canCreate ? (
                  <Button variant="primary" onClick={() => setCreateOpen(true)} data-testid="create-key">
                    Create key
                  </Button>
                ) : null}
              </SpaceBetween>
            }
          >
            Agent keys
          </InfoHeader>
        }
        items={rows}
        loading={keys.isLoading}
        loadingText="Loading keys"
        trackBy="alias"
        selectionType={canManage ? "single" : undefined}
        selectedItems={selected}
        onSelectionChange={({ detail }) => setSelected([...detail.selectedItems])}
        ariaLabels={{ selectionGroupLabel: "Key selection", itemSelectionLabel: (_data, item) => item.alias }}
        wrapLines
        columnDefinitions={[
          { id: "alias", header: "Alias", minWidth: 280, cell: (k) => <CopyText value={k.alias} max={48} />, isRowHeader: true },
          { id: "env", header: "Env", cell: (k) => <EnvBadge env={k.env} /> },
          { id: "model", header: "Model", minWidth: 150, cell: (k) => k.model },
          {
            id: "spend",
            header: "Spend vs cap",
            minWidth: 220,
            cell: (k) => (
              <SpendBar
                spend={k.spendInfo?.spend ?? (spend.isLoading ? null : 0)}
                cap={k.spendInfo?.maxBudget ?? k.maxBudget}
                blocked={k.spendInfo?.blocked}
                resetAt={k.spendInfo?.budgetResetAt ?? undefined}
              />
            ),
          },
          { id: "status", header: "Status", minWidth: 130, cell: (k) => <KeyStatus status={k.status} blocked={k.spendInfo?.blocked} /> },
          {
            id: "reset",
            header: "Reset",
            minWidth: 170,
            cell: (k) =>
              k.spendInfo?.budgetResetAt ? (
                <span title={formatDateTime(k.spendInfo.budgetResetAt)}>
                  {formatRelative(k.spendInfo.budgetResetAt)} (every {formatBudgetDuration(k.spendInfo.budgetDuration ?? k.budgetDuration)})
                </span>
              ) : (
                `every ${formatBudgetDuration(k.budgetDuration)}`
              ),
          },
          { id: "adopted", header: "Origin", minWidth: 150, cell: (k) => (k.adopted ? "Adopted (POST /projects/{id}/keys with adopt)" : "Created by the platform") },
          { id: "provider", header: "Credential provider", minWidth: 260, cell: (k) => <CopyText value={k.credentialProviderName ?? k.credentialProviderArn} max={40} /> },
          { id: "created", header: "Created", minWidth: 200, cell: (k) => `${formatDateTime(k.createdAt)} by ${k.createdBy}` },
        ]}
        empty={
          <EmptyState
            title="No keys yet"
            description="Keys are created with POST /projects/{id}/keys (or adopted through its adopt field); the deploy job creates the Dev key when it is missing, and the first promotion creates the Prod key."
            action={canCreate ? <Button onClick={() => setCreateOpen(true)}>Create key</Button> : undefined}
          />
        }
      />
      <CreateKeyModal project={project} visible={createOpen} existing={rows} onDismiss={() => setCreateOpen(false)} />
      {raiseCapKey ? <RaiseCapModal project={project} keyRow={raiseCapKey} onDismiss={() => setRaiseCapKey(null)} /> : null}
      <ConfirmModal
        visible={!!statusChange}
        header={statusChange?.status === "suspended" ? `Suspend key ${statusChange.key.alias}` : `Resume key ${statusChange?.key.alias ?? ""}`}
        confirmText={statusChange?.status === "suspended" ? "Suspend" : "Resume"}
        loading={update.isPending}
        error={update.error}
        onConfirm={applyStatus}
        onDismiss={() => {
          update.reset();
          setStatusChange(null);
        }}
      >
        {statusChange?.status === "suspended" ? (
          <Box>LiteLLM blocks the key immediately: the agent's next model call fails until the key is resumed.</Box>
        ) : (
          <Box>LiteLLM unblocks the key; the agent can call the model again.</Box>
        )}
      </ConfirmModal>
    </SpaceBetween>
  );
}

function CreateKeyModal({ project, visible, existing, onDismiss }: { project: Project; visible: boolean; existing: KeyRow[]; onDismiss: () => void }) {
  const settings = useSettings();
  const create = useCreateKey();
  const notifications = useNotifications();
  const [env, setEnv] = useState<Env>("dev");
  const [model, setModel] = useState<string>(project.litellm?.models?.[0] ?? "");
  const [maxBudget, setMaxBudget] = useState("");
  const [budgetDuration, setBudgetDuration] = useState("");
  const [touched, setTouched] = useState(false);

  useEffect(() => {
    if (visible && settings.data) {
      setMaxBudget((v) => v || String(settings.data.defaultKeyCap.maxBudget));
      setBudgetDuration((v) => v || settings.data.defaultKeyCap.budgetDuration);
    }
  }, [visible, settings.data]);

  const modelOptions: SelectProps.Option[] = (project.litellm?.models ?? []).map((m) => ({ value: m, label: m }));
  const envOptions: SelectProps.Option[] = [
    { value: "dev", label: "dev" },
    { value: "prod", label: "prod" },
  ];
  const duplicate = existing.some((k) => k.env === env && k.model === model);
  const errors = {
    model: model ? undefined : "choose a model",
    maxBudget: numberError(maxBudget, { minimum: 0 }),
    budgetDuration: durationError(budgetDuration),
    duplicate: duplicate ? `a key for ${env}/${model} already exists (PATCH it instead)` : undefined,
  };
  const hasErrors = Object.values(errors).some(Boolean);
  const close = () => {
    setTouched(false);
    create.reset();
    onDismiss();
  };
  const submit = () => {
    setTouched(true);
    if (hasErrors) {
      return;
    }
    create.mutate(
      { projectId: project.projectId, key: { env, model, maxBudget: Number(maxBudget), budgetDuration: budgetDuration.trim() } },
      {
        onSuccess: (key) => {
          notifications.success(`Key ${key.alias} created`, `LiteLLM key and credential provider ${key.credentialProviderName} are in place.`);
          close();
        },
      },
    );
  };
  return (
    <Modal
      visible={visible}
      onDismiss={close}
      header="Create agent key"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={close} disabled={create.isPending}>
              Cancel
            </Button>
            <Button variant="primary" onClick={submit} loading={create.isPending} data-testid="create-key-submit">
              Create key
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <Form errorText={create.error ? formatErrorContent(create.error) : undefined}>
        <SpaceBetween size="m">
          <Alert type="info">
            Creates the LiteLLM service-account key <code>{`${project.projectId}-${env}-${model || "<model>"}`}</code> (restricted to the model, with
            the cap below) and stores it in AgentCore Identity as credential provider <code>{`litellm-${project.projectId}-${env}-${model || "<model>"}`}</code>.
          </Alert>
          <ColumnLayout columns={2}>
            {/* the plain fields of this grid keep the same description-slot height as their InfoFormField neighbours */}
            <FormField label="Environment" description={INFO_SPACER}>
              <Select selectedOption={envOptions.find((o) => o.value === env) ?? null} options={envOptions} onChange={({ detail }) => setEnv(detail.selectedOption.value as Env)} />
            </FormField>
            <InfoFormField label="Model" help="One of the project's allowed models" errorText={touched ? errors.model ?? errors.duplicate : errors.duplicate}>
              <Select selectedOption={modelOptions.find((o) => o.value === model) ?? null} options={modelOptions} onChange={({ detail }) => setModel(detail.selectedOption.value ?? "")} />
            </InfoFormField>
            <InfoFormField label="Key cap (USD)" help="LiteLLM max_budget; default from the platform settings" errorText={touched ? errors.maxBudget : undefined}>
              <Input type="number" inputMode="decimal" value={maxBudget} onChange={({ detail }) => setMaxBudget(detail.value)} />
            </InfoFormField>
            <FormField label="Budget period" description={INFO_SPACER} errorText={touched ? errors.budgetDuration : undefined}>
              <Input value={budgetDuration} onChange={({ detail }) => setBudgetDuration(detail.value)} />
            </FormField>
          </ColumnLayout>
        </SpaceBetween>
      </Form>
    </Modal>
  );
}

function RaiseCapModal({ project, keyRow, onDismiss }: { project: Project; keyRow: KeyRow; onDismiss: () => void }) {
  const update = useUpdateKey();
  const notifications = useNotifications();
  const current = keyRow.spendInfo?.maxBudget ?? keyRow.maxBudget;
  const [value, setValue] = useState(String(current ?? ""));
  const error = numberError(value, { minimum: 0 });
  const submit = () => {
    if (error) {
      return;
    }
    update.mutate(
      { projectId: project.projectId, env: keyRow.env, model: keyRow.model, update: { maxBudget: Number(value) } },
      {
        onSuccess: (key) => {
          notifications.success(`Cap of ${key.alias} set to ${formatUsd(key.maxBudget)}`);
          onDismiss();
        },
      },
    );
  };
  return (
    <Modal
      visible
      onDismiss={onDismiss}
      header={`Set cap of ${keyRow.alias}`}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={update.isPending}>
              Cancel
            </Button>
            <Button variant="primary" onClick={submit} loading={update.isPending} disabled={!!error} data-testid="raise-cap-submit">
              Set cap
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <Form errorText={update.error ? formatErrorContent(update.error) : undefined}>
        <SpaceBetween size="m">
          <Box>
            Spend so far: {formatUsd(keyRow.spendInfo?.spend ?? 0)} of {formatUsd(current)} per {formatBudgetDuration(keyRow.budgetDuration)}
            {keyRow.spendInfo?.blocked ? " (the key is blocked: LiteLLM refuses model calls with budget_exceeded)" : ""}.
          </Box>
          <InfoFormField
            label="New cap (USD)"
            help="PATCH maxBudget raises or lowers the LiteLLM max_budget. A cap below the current spend blocks the key with budget_exceeded (the Playground shows it) until it is raised again; a cap above the spend unblocks it at once."
            errorText={error}
          >
            <Input type="number" inputMode="decimal" value={value} onChange={({ detail }) => setValue(detail.value)} autoFocus />
          </InfoFormField>
        </SpaceBetween>
      </Form>
    </Modal>
  );
}
