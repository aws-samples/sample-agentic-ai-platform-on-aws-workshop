import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Checkbox from "@cloudscape-design/components/checkbox";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Form from "@cloudscape-design/components/form";
import FormField from "@cloudscape-design/components/form-field";
import Input from "@cloudscape-design/components/input";
import Modal from "@cloudscape-design/components/modal";
import Multiselect, { type MultiselectProps } from "@cloudscape-design/components/multiselect";
import Select, { type SelectProps } from "@cloudscape-design/components/select";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { useEffect, useMemo, useState } from "react";

import { isApiError } from "../../api/client";
import { useCreateProject, useDeleteProject, useGovernance, useProjectOptions, useSettings, useUpdateProject } from "../../api/hooks";
import type { Domain, Project, ProjectCreate, ProjectUpdate } from "../../api/types";
import { INFO_SPACER, InfoFormField } from "../../components/InfoPopover";
import { formatErrorContent, useNotifications } from "../../layout/notifications";
import { knowledgeBasePickerOptions, sameValues, selectedOptions, toolPickerOptions, valuesOf } from "./pickerOptions";
import { durationError, idError, numberError } from "./validation";

interface CapsState {
  maxBudget: string;
  budgetDuration: string;
  tpmLimit: string;
  rpmLimit: string;
}

function capsErrors(caps: CapsState) {
  return {
    maxBudget: numberError(caps.maxBudget, { minimum: 0 }),
    budgetDuration: durationError(caps.budgetDuration),
    tpmLimit: numberError(caps.tpmLimit, { minimum: 1, integer: true }),
    rpmLimit: numberError(caps.rpmLimit, { minimum: 1, integer: true }),
  };
}

function useModelOptions(): { options: MultiselectProps.Option[]; loading: boolean } {
  const governance = useGovernance();
  const options = useMemo<MultiselectProps.Option[]>(
    () =>
      (governance.data?.models ?? []).map((m) => ({
        value: m.alias,
        label: m.alias,
        description: m.description ?? m.modelId ?? undefined,
        tags: m.tier ? [m.tier] : undefined,
      })),
    [governance.data],
  );
  return { options, loading: governance.isLoading };
}

/**
 * The two pick lists of the project form: the Lambdas the gateway role may invoke and the knowledge bases it may retrieve
 * from. Candidates come from GET /governance/project-options (scenario tools, the Lambda targets of every AgentCore
 * gateway, registered targets; Bedrock knowledge bases) and show the name on the first line with the ARN / id as the muted
 * second line, so an admin picks instead of copying identifiers from the AgentCore and Bedrock consoles. The state holds the
 * values (ARNs / ids); the option objects are derived, so labels refresh when the candidates arrive.
 */
function ResourcePickers({
  visible,
  toolValues,
  kbValues,
  onToolValues,
  onKbValues,
  toolDescription,
  kbDescription,
}: {
  visible: boolean;
  toolValues: string[];
  kbValues: string[];
  onToolValues: (values: string[]) => void;
  onKbValues: (values: string[]) => void;
  toolDescription: string;
  kbDescription: string;
}) {
  const options = useProjectOptions(visible);
  const toolOptions = useMemo(() => toolPickerOptions(options.data?.tools, toolValues), [options.data, toolValues]);
  const kbOptions = useMemo(() => knowledgeBasePickerOptions(options.data?.knowledgeBases, kbValues), [options.data, kbValues]);
  const statusType = options.isLoading ? "loading" : options.error ? "error" : "finished";
  const errorText = options.error ? `Candidates could not be loaded: ${formatErrorContent(options.error)}` : undefined;
  // the dropdown footer shows the error only once opened; the field itself says that the current values are kept
  const warningText = options.error ? "The candidate list could not be loaded; the project's current values are kept and stay selectable." : undefined;
  const retry = () => {
    if (options.error) {
      void options.refetch();
    }
  };
  const warnings = options.data?.warnings ?? [];
  return (
    <SpaceBetween size="m">
      {warnings.length ? (
        <Alert type="warning" header="Some candidates could not be listed">
          {warnings.join(" ")}
        </Alert>
      ) : null}
      <ColumnLayout columns={2}>
        <InfoFormField label="Allowed tools" help={toolDescription} warningText={warningText}>
          <Multiselect
            selectedOptions={selectedOptions(toolOptions, toolValues)}
            options={toolOptions}
            onChange={({ detail }) => onToolValues(valuesOf(detail.selectedOptions))}
            statusType={statusType}
            errorText={errorText}
            recoveryText="Retry"
            onLoadItems={retry}
            loadingText="Loading tool Lambdas and gateway targets"
            filteringType="auto"
            placeholder="Choose tool Lambdas"
            empty="No tool Lambdas found"
            tokenLimit={3}
            data-testid="allowed-tools"
          />
        </InfoFormField>
        <InfoFormField label="Knowledge bases" help={kbDescription} warningText={warningText}>
          <Multiselect
            selectedOptions={selectedOptions(kbOptions, kbValues)}
            options={kbOptions}
            onChange={({ detail }) => onKbValues(valuesOf(detail.selectedOptions))}
            statusType={statusType}
            errorText={errorText}
            recoveryText="Retry"
            onLoadItems={retry}
            loadingText="Loading knowledge bases"
            filteringType="auto"
            placeholder="Choose knowledge bases"
            empty="No knowledge bases found"
            tokenLimit={3}
            data-testid="knowledge-bases"
          />
        </InfoFormField>
      </ColumnLayout>
    </SpaceBetween>
  );
}

export function CreateProjectModal({
  visible,
  domains,
  defaultDomainId,
  onDismiss,
  onCreated,
}: {
  visible: boolean;
  domains: Domain[];
  defaultDomainId?: string;
  onDismiss: () => void;
  onCreated: (project: Project) => void;
}) {
  const settings = useSettings();
  const models = useModelOptions();
  const create = useCreateProject();
  const notifications = useNotifications();
  const [projectId, setProjectId] = useState("");
  const [domainId, setDomainId] = useState(defaultDomainId ?? "");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [selectedModels, setSelectedModels] = useState<MultiselectProps.Option[]>([]);
  const [caps, setCaps] = useState<CapsState>({ maxBudget: "", budgetDuration: "", tpmLimit: "", rpmLimit: "" });
  const [toolValues, setToolValues] = useState<string[]>([]);
  const [kbValues, setKbValues] = useState<string[]>([]);
  const [touched, setTouched] = useState(false);

  // Prefill the caps with the platform defaults (GET /settings) once they are known.
  useEffect(() => {
    const defaults = settings.data?.defaultProjectCap;
    if (defaults && visible) {
      setCaps((current) => ({
        maxBudget: current.maxBudget || String(defaults.maxBudget),
        budgetDuration: current.budgetDuration || defaults.budgetDuration,
        tpmLimit: current.tpmLimit || String(defaults.tpmLimit),
        rpmLimit: current.rpmLimit || String(defaults.rpmLimit),
      }));
    }
  }, [settings.data, visible]);

  useEffect(() => {
    if (visible) {
      setDomainId((current) => current || defaultDomainId || domains[0]?.domainId || "");
    }
  }, [visible, defaultDomainId, domains]);

  const errors = {
    projectId: idError(projectId, "projectId"),
    domainId: domainId ? undefined : "choose a domain",
    name: name.trim() ? undefined : "name is required",
    models: selectedModels.length ? undefined : "choose at least one approved model",
    ...capsErrors(caps),
  };
  const hasErrors = Object.values(errors).some(Boolean);
  const domainOptions: SelectProps.Option[] = domains.map((d) => ({ value: d.domainId, label: d.name || d.domainId, description: d.domainId }));

  const reset = () => {
    setProjectId("");
    setName("");
    setDescription("");
    setSelectedModels([]);
    setToolValues([]);
    setKbValues([]);
    setTouched(false);
    setCaps({ maxBudget: "", budgetDuration: "", tpmLimit: "", rpmLimit: "" });
    create.reset();
  };

  const submit = () => {
    setTouched(true);
    if (hasErrors) {
      return;
    }
    const body: ProjectCreate = {
      projectId: projectId.trim(),
      domainId,
      name: name.trim(),
      description: description.trim(),
      models: selectedModels.map((o) => o.value as string),
      maxBudget: Number(caps.maxBudget),
      budgetDuration: caps.budgetDuration.trim(),
      tpmLimit: Number(caps.tpmLimit),
      rpmLimit: Number(caps.rpmLimit),
      allowedToolArns: toolValues,
      knowledgeBaseIds: kbValues,
    };
    create.mutate(body, {
      onSuccess: (project) => {
        notifications.success(`Project ${project.projectId} created`, "LiteLLM team and the four project roles are in place.");
        reset();
        onCreated(project);
      },
    });
  };

  return (
    <Modal
      visible={visible}
      size="large"
      onDismiss={() => {
        reset();
        onDismiss();
      }}
      header="Create project"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button
              variant="link"
              onClick={() => {
                reset();
                onDismiss();
              }}
              disabled={create.isPending}
            >
              Cancel
            </Button>
            <Button variant="primary" onClick={submit} loading={create.isPending} data-testid="create-project-submit">
              Create project
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <Form errorText={create.error ? formatErrorContent(create.error) : undefined}>
        <SpaceBetween size="m">
          <Alert type="info">
            Creating a project provisions a LiteLLM team (caps, allowed models) and four IAM roles (agent and gateway role per
            environment). Takes about two seconds.
          </Alert>
          <ColumnLayout columns={2}>
            <InfoFormField label="Project id" help="Becomes the LiteLLM team id" constraintText="kebab-case" errorText={touched ? errors.projectId : undefined}>
              <Input value={projectId} onChange={({ detail }) => setProjectId(detail.value)} placeholder="hr-assistant" autoFocus />
            </InfoFormField>
            {/* the plain fields of this row keep the same description-slot height as their InfoFormField neighbours */}
            <FormField label="Domain" description={INFO_SPACER} errorText={touched ? errors.domainId : undefined}>
              <Select
                selectedOption={domainOptions.find((o) => o.value === domainId) ?? null}
                options={domainOptions}
                onChange={({ detail }) => setDomainId(detail.selectedOption.value ?? "")}
                placeholder="Choose a domain"
                empty="Create a domain first"
              />
            </FormField>
            <FormField label="Name" description={INFO_SPACER} errorText={touched ? errors.name : undefined}>
              <Input value={name} onChange={({ detail }) => setName(detail.value)} placeholder="HR Assistant" />
            </FormField>
            <FormField label="Description" description={INFO_SPACER}>
              <Input value={description} onChange={({ detail }) => setDescription(detail.value)} />
            </FormField>
          </ColumnLayout>
          <InfoFormField
            label="Allowed models"
            help="Approved aliases of the model gateway (GET /governance); the blueprint's model must be one of them"
            errorText={touched ? errors.models : undefined}
          >
            <Multiselect
              selectedOptions={selectedModels}
              options={models.options}
              onChange={({ detail }) => setSelectedModels([...detail.selectedOptions])}
              statusType={models.loading ? "loading" : "finished"}
              placeholder="Choose approved models"
              tokenLimit={4}
            />
          </InfoFormField>
          <ColumnLayout columns={4}>
            <InfoFormField label="Project cap (USD)" help="LiteLLM team max_budget" errorText={touched ? errors.maxBudget : undefined}>
              <Input type="number" inputMode="decimal" value={caps.maxBudget} onChange={({ detail }) => setCaps({ ...caps, maxBudget: detail.value })} />
            </InfoFormField>
            <InfoFormField label="Budget period" help="7d avoids a UTC-midnight reset" errorText={touched ? errors.budgetDuration : undefined}>
              <Input value={caps.budgetDuration} onChange={({ detail }) => setCaps({ ...caps, budgetDuration: detail.value })} />
            </InfoFormField>
            <FormField label="Tokens per minute" description={INFO_SPACER} errorText={touched ? errors.tpmLimit : undefined}>
              <Input type="number" inputMode="numeric" value={caps.tpmLimit} onChange={({ detail }) => setCaps({ ...caps, tpmLimit: detail.value })} />
            </FormField>
            <FormField label="Requests per minute" description={INFO_SPACER} errorText={touched ? errors.rpmLimit : undefined}>
              <Input type="number" inputMode="numeric" value={caps.rpmLimit} onChange={({ detail }) => setCaps({ ...caps, rpmLimit: detail.value })} />
            </FormField>
          </ColumnLayout>
          <ResourcePickers
            visible={visible}
            toolValues={toolValues}
            kbValues={kbValues}
            onToolValues={setToolValues}
            onKbValues={setKbValues}
            toolDescription="Lambda functions the gateway role may invoke (the candidates come from the registered gateway targets); the name is shown, the ARN below it"
            kbDescription="Bedrock knowledge bases the gateway role may retrieve from; the name is shown, the id below it"
          />
        </SpaceBetween>
      </Form>
    </Modal>
  );
}

export function EditProjectModal({ project, visible, onDismiss }: { project: Project; visible: boolean; onDismiss: () => void }) {
  const models = useModelOptions();
  const update = useUpdateProject();
  const notifications = useNotifications();
  const [name, setName] = useState(project.name);
  const [description, setDescription] = useState(project.description ?? "");
  const [selectedModels, setSelectedModels] = useState<MultiselectProps.Option[]>([]);
  const [caps, setCaps] = useState<CapsState>({
    maxBudget: String(project.litellm?.maxBudget ?? ""),
    budgetDuration: project.litellm?.budgetDuration ?? "",
    tpmLimit: String(project.litellm?.tpmLimit ?? ""),
    rpmLimit: String(project.litellm?.rpmLimit ?? ""),
  });
  const [toolValues, setToolValues] = useState<string[]>(project.allowedToolArns ?? []);
  const [kbValues, setKbValues] = useState<string[]>(project.knowledgeBaseIds ?? []);
  const [touched, setTouched] = useState(false);

  useEffect(() => {
    if (visible) {
      setName(project.name);
      setDescription(project.description ?? "");
      setCaps({
        maxBudget: String(project.litellm?.maxBudget ?? ""),
        budgetDuration: project.litellm?.budgetDuration ?? "",
        tpmLimit: String(project.litellm?.tpmLimit ?? ""),
        rpmLimit: String(project.litellm?.rpmLimit ?? ""),
      });
      setToolValues(project.allowedToolArns ?? []);
      setKbValues(project.knowledgeBaseIds ?? []);
      setTouched(false);
      update.reset();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reset the form when the modal opens
  }, [visible, project]);

  useEffect(() => {
    if (visible && models.options.length) {
      setSelectedModels(models.options.filter((o) => (project.litellm?.models ?? []).includes(o.value as string)));
    }
  }, [visible, models.options, project]);

  const errors = { name: name.trim() ? undefined : "name is required", models: selectedModels.length ? undefined : "choose at least one model", ...capsErrors(caps) };
  const hasErrors = Object.values(errors).some(Boolean);

  const submit = () => {
    setTouched(true);
    if (hasErrors) {
      return;
    }
    const fields: ProjectUpdate = {};
    if (name.trim() !== project.name) fields.name = name.trim();
    if (description.trim() !== (project.description ?? "")) fields.description = description.trim();
    const chosen = selectedModels.map((o) => o.value as string);
    if (JSON.stringify(chosen) !== JSON.stringify(project.litellm?.models ?? [])) fields.models = chosen;
    if (Number(caps.maxBudget) !== Number(project.litellm?.maxBudget)) fields.maxBudget = Number(caps.maxBudget);
    if (caps.budgetDuration.trim() !== project.litellm?.budgetDuration) fields.budgetDuration = caps.budgetDuration.trim();
    if (Number(caps.tpmLimit) !== Number(project.litellm?.tpmLimit)) fields.tpmLimit = Number(caps.tpmLimit);
    if (Number(caps.rpmLimit) !== Number(project.litellm?.rpmLimit)) fields.rpmLimit = Number(caps.rpmLimit);
    // set comparison: unticking and re-ticking an entry must not re-stamp the gateway roles
    if (!sameValues(toolValues, project.allowedToolArns ?? [])) fields.allowedToolArns = Array.from(new Set(toolValues));
    if (!sameValues(kbValues, project.knowledgeBaseIds ?? [])) fields.knowledgeBaseIds = Array.from(new Set(kbValues));
    if (!Object.keys(fields).length) {
      onDismiss();
      return;
    }
    update.mutate(
      { projectId: project.projectId, fields },
      {
        onSuccess: () => {
          notifications.success(`Project ${project.projectId} updated`, `Changed: ${Object.keys(fields).join(", ")}`);
          onDismiss();
        },
      },
    );
  };

  return (
    <Modal
      visible={visible}
      size="large"
      onDismiss={onDismiss}
      header={`Edit project ${project.projectId}`}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={update.isPending}>
              Cancel
            </Button>
            <Button variant="primary" onClick={submit} loading={update.isPending}>
              Save changes
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <Form errorText={update.error ? formatErrorContent(update.error) : undefined}>
        <SpaceBetween size="m">
          <ColumnLayout columns={2}>
            <FormField label="Name" errorText={touched ? errors.name : undefined}>
              <Input value={name} onChange={({ detail }) => setName(detail.value)} />
            </FormField>
            <FormField label="Description">
              <Input value={description} onChange={({ detail }) => setDescription(detail.value)} />
            </FormField>
          </ColumnLayout>
          <InfoFormField label="Allowed models" help="Updates the LiteLLM team" errorText={touched ? errors.models : undefined}>
            <Multiselect
              selectedOptions={selectedModels}
              options={models.options}
              onChange={({ detail }) => setSelectedModels([...detail.selectedOptions])}
              statusType={models.loading ? "loading" : "finished"}
              tokenLimit={4}
            />
          </InfoFormField>
          <ColumnLayout columns={4}>
            <FormField label="Project cap (USD)" errorText={touched ? errors.maxBudget : undefined}>
              <Input type="number" inputMode="decimal" value={caps.maxBudget} onChange={({ detail }) => setCaps({ ...caps, maxBudget: detail.value })} />
            </FormField>
            <FormField label="Budget period" errorText={touched ? errors.budgetDuration : undefined}>
              <Input value={caps.budgetDuration} onChange={({ detail }) => setCaps({ ...caps, budgetDuration: detail.value })} />
            </FormField>
            <FormField label="Tokens per minute" errorText={touched ? errors.tpmLimit : undefined}>
              <Input type="number" inputMode="numeric" value={caps.tpmLimit} onChange={({ detail }) => setCaps({ ...caps, tpmLimit: detail.value })} />
            </FormField>
            <FormField label="Requests per minute" errorText={touched ? errors.rpmLimit : undefined}>
              <Input type="number" inputMode="numeric" value={caps.rpmLimit} onChange={({ detail }) => setCaps({ ...caps, rpmLimit: detail.value })} />
            </FormField>
          </ColumnLayout>
          <ResourcePickers
            visible={visible}
            toolValues={toolValues}
            kbValues={kbValues}
            onToolValues={setToolValues}
            onKbValues={setKbValues}
            toolDescription="Lambda functions the gateway role may invoke; a change re-stamps the gateway roles"
            kbDescription="Bedrock knowledge bases the gateway role may retrieve from; a change re-stamps the gateway roles"
          />
        </SpaceBetween>
      </Form>
    </Modal>
  );
}

export function DeleteProjectModal({
  project,
  visible,
  onDismiss,
  onDeleted,
}: {
  project: Project | null;
  visible: boolean;
  onDismiss: () => void;
  onDeleted: () => void;
}) {
  const remove = useDeleteProject();
  const notifications = useNotifications();
  const [confirmation, setConfirmation] = useState("");
  const [force, setForce] = useState(false);
  useEffect(() => {
    if (visible) {
      setConfirmation("");
      setForce(false);
      remove.reset();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reset when opened
  }, [visible]);
  if (!project) {
    return null;
  }
  const promotedBlock = isApiError(remove.error) && remove.error.status === 409;
  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header={`Delete project ${project.projectId}`}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={remove.isPending}>
              Cancel
            </Button>
            <Button
              variant="primary"
              disabled={confirmation !== project.projectId}
              loading={remove.isPending}
              onClick={() =>
                remove.mutate(
                  { projectId: project.projectId, force },
                  {
                    onSuccess: (summary) => {
                      notifications.success(`Project ${project.projectId} deleted`, `${summary.items} items, ${summary.keys.length} keys, team and roles removed.`);
                      onDeleted();
                    },
                  },
                )
              }
            >
              Delete
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        <Alert type="warning">
          Deleting removes the LiteLLM team and keys, the credential providers, the four IAM roles and every platform record of the
          project. The project's AgentCore resources (gateway, memory, harness) stay and must be deleted with the workshop cleanup command.
        </Alert>
        <FormField label={`Type ${project.projectId} to confirm`}>
          <Input value={confirmation} onChange={({ detail }) => setConfirmation(detail.value)} />
        </FormField>
        <Checkbox checked={force} onChange={({ detail }) => setForce(detail.checked)}>
          Force: delete even though a PROMOTED release exists (the Prod harness keeps running until the workshop cleanup command removes it)
        </Checkbox>
        {remove.error ? (
          <Alert type="error" header={promotedBlock ? "A PROMOTED release blocks the deletion" : "Deletion failed"}>
            {formatErrorContent(remove.error)}
            {promotedBlock ? " Tick Force to delete anyway." : ""}
          </Alert>
        ) : null}
      </SpaceBetween>
    </Modal>
  );
}
