import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Form from "@cloudscape-design/components/form";
import FormField from "@cloudscape-design/components/form-field";
import Modal from "@cloudscape-design/components/modal";
import Select, { type SelectProps } from "@cloudscape-design/components/select";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Textarea from "@cloudscape-design/components/textarea";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import { useBlueprints, useCreateRelease, useProjects } from "../../api/hooks";
import { InfoFormField } from "../../components/InfoPopover";
import { formatErrorContent, useNotifications } from "../../layout/notifications";
import { formatDateTime } from "../../utils/format";
import { thresholdsText } from "./releaseHelpers";

/**
 * POST /projects/{id}/releases {blueprintVersion, notes, autoRun: true} (developer or admin): the platform deploys the version to Dev if the
 * Dev harness does not run it yet, runs the evaluation dataset and applies the thresholds; the release page follows the run.
 */
export function CreateReleaseModal({ visible, defaultProjectId, onDismiss }: { visible: boolean; defaultProjectId?: string; onDismiss: () => void }) {
  const navigate = useNavigate();
  const notifications = useNotifications();
  const projects = useProjects();
  const create = useCreateRelease();
  const [projectId, setProjectId] = useState(defaultProjectId ?? "");
  const [version, setVersion] = useState<number | null>(null);
  const [notes, setNotes] = useState("");
  const [touched, setTouched] = useState(false);
  const blueprints = useBlueprints(projectId || undefined);
  const versions = [...(blueprints.data ?? [])].sort((a, b) => b.version - a.version);

  useEffect(() => {
    if (visible) {
      setProjectId((current) => current || defaultProjectId || "");
    }
  }, [visible, defaultProjectId]);

  // Default to the latest blueprint version of the chosen project.
  useEffect(() => {
    if (versions.length && (version === null || !versions.some((b) => b.version === version))) {
      setVersion(versions[0].version);
    }
  }, [versions, version]);

  const projectOptions: SelectProps.Option[] = [...(projects.data ?? [])]
    .sort((a, b) => a.projectId.localeCompare(b.projectId))
    .map((p) => ({ value: p.projectId, label: p.name || p.projectId, description: p.projectId }));
  const versionOptions: SelectProps.Option[] = versions.map((b) => ({
    value: String(b.version),
    label: `v${b.version} · ${b.model}`,
    description: `${formatDateTime(b.createdAt)} by ${b.createdBy}${b.notes ? ` · ${b.notes}` : ""}`,
    tags: [thresholdsText(b.thresholds)],
  }));
  const errors = {
    projectId: projectId ? undefined : "choose a project",
    version:
      version === null
        ? blueprints.data && !versions.length
          ? "this project has no blueprint version yet; post one with POST /projects/{id}/blueprints"
          : "choose a blueprint version"
        : undefined,
  };
  const hasErrors = Object.values(errors).some(Boolean);

  const close = () => {
    setTouched(false);
    setNotes("");
    setProjectId(""); // the next open starts from the list's current project filter, not the one chosen last time
    setVersion(null);
    create.reset();
    onDismiss();
  };
  const submit = () => {
    setTouched(true);
    if (hasErrors || version === null) {
      return;
    }
    create.mutate(
      { projectId, blueprintVersion: version, notes: notes.trim() },
      {
        onSuccess: (release) => {
          if (release.status === "DRAFT" && release.error) {
            // The draft exists even when CodePipeline could not start; it can be retried from its page.
            notifications.warning(
              `Release ${release.releaseId} created, but the run did not start`,
              `${release.error.code}: ${release.error.message} The release page offers to run it again.`,
            );
          } else {
            notifications.success(
              `Release ${release.releaseId} created`,
              `CodePipeline will deploy and evaluate blueprint v${release.blueprintVersion}. Follow execution progress on the release page.`,
            );
          }
          close();
          navigate(`/releases/${release.releaseId}`);
        },
      },
    );
  };

  return (
    <Modal
      visible={visible}
      onDismiss={close}
      header="Create release"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={close} disabled={create.isPending}>
              Cancel
            </Button>
            <Button variant="primary" onClick={submit} loading={create.isPending} data-testid="create-release-submit">
              Create release
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <Form errorText={create.error ? formatErrorContent(create.error) : undefined}>
        <SpaceBetween size="m">
          <Alert type="info">
            A release is one blueprint version. CodePipeline deploys it to Dev, evaluates the dataset, and applies the version&apos;s thresholds,
            producing BLOCKED or PENDING_APPROVAL. Approve and promote through the separate manual gates on the release page.
            Executions queue behind earlier releases, including those waiting for approval.
          </Alert>
          <FormField label="Project" errorText={touched ? errors.projectId : undefined}>
            <Select
              selectedOption={projectOptions.find((o) => o.value === projectId) ?? null}
              options={projectOptions}
              placeholder={projects.isLoading ? "Loading projects…" : "Choose a project"}
              statusType={projects.isLoading ? "loading" : projects.error ? "error" : "finished"}
              onChange={({ detail }) => {
                setProjectId(detail.selectedOption.value ?? "");
                setVersion(null);
              }}
              data-testid="release-project"
            />
          </FormField>
          <InfoFormField
            label="Blueprint version"
            help="Newest first; the thresholds of the chosen version gate the release"
            errorText={touched ? errors.version : undefined}
          >
            <Select
              selectedOption={versionOptions.find((o) => o.value === String(version)) ?? null}
              options={versionOptions}
              placeholder={!projectId ? "Choose a project first" : blueprints.isLoading ? "Loading blueprint versions…" : "Choose a version"}
              statusType={blueprints.isLoading ? "loading" : blueprints.error ? "error" : "finished"}
              empty="No blueprint versions yet: post one with POST /projects/{id}/blueprints"
              onChange={({ detail }) => setVersion(detail.selectedOption.value ? Number(detail.selectedOption.value) : null)}
              data-testid="release-version"
            />
          </InfoFormField>
          <InfoFormField label="Notes" help="What changed in this candidate" constraintText="Optional">
            <Textarea value={notes} onChange={({ detail }) => setNotes(detail.value)} rows={2} placeholder="prompt v2: grounded policy answers" data-testid="release-notes" />
          </InfoFormField>
        </SpaceBetween>
      </Form>
    </Modal>
  );
}
