import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import SegmentedControl from "@cloudscape-design/components/segmented-control";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Spinner from "@cloudscape-design/components/spinner";
import Table from "@cloudscape-design/components/table";
import { useEffect, useState } from "react";

import { isApiError } from "../../../api/client";
import { useBlueprints, useRenderBlueprint } from "../../../api/hooks";
import type { Blueprint, Env, Project } from "../../../api/types";
import { EmptyState } from "../../../components/EmptyState";
import { ErrorAlert } from "../../../components/ErrorAlert";
import { InfoHeader } from "../../../components/InfoPopover";
import { JsonBlock } from "../../../components/JsonBlock";
import { formatDateTime } from "../../../utils/format";
import { thresholdsText as sharedThresholdsText } from "../../releases/releaseHelpers";

const thresholdsText = (thresholds: Record<string, number> | undefined) => sharedThresholdsText(thresholds, "—");

export function BlueprintsTab({ project }: { project: Project }) {
  const blueprints = useBlueprints(project.projectId);
  const [selected, setSelected] = useState<Blueprint[]>([]);
  const [env, setEnv] = useState<Env>("dev");
  const versions = [...(blueprints.data ?? [])].sort((a, b) => b.version - a.version);
  const current = versions.find((b) => b.version === selected[0]?.version) ?? null;

  useEffect(() => {
    if (!selected.length && versions.length) {
      setSelected([versions[0]]);
    }
  }, [versions, selected.length]);

  return (
    <SpaceBetween size="l">
      {blueprints.error ? <ErrorAlert header="Blueprints could not be loaded" error={blueprints.error} onRetry={() => void blueprints.refetch()} /> : null}
      <Table
        header={
          <InfoHeader
            counter={blueprints.data ? `(${blueprints.data.length})` : undefined}
            help="Immutable versions posted with POST /projects/{id}/blueprints; select one to preview the harness the platform would create."
          >
            Blueprint versions
          </InfoHeader>
        }
        items={versions}
        loading={blueprints.isLoading}
        loadingText="Loading blueprint versions"
        trackBy={(b) => String(b.version)}
        selectionType="single"
        selectedItems={selected}
        onSelectionChange={({ detail }) => setSelected([...detail.selectedItems])}
        ariaLabels={{ selectionGroupLabel: "Blueprint version selection", itemSelectionLabel: (_data, item) => `version ${item.version}` }}
        wrapLines
        columnDefinitions={[
          { id: "version", header: "Version", cell: (b) => `v${b.version}`, isRowHeader: true },
          { id: "model", header: "Model", cell: (b) => b.model },
          { id: "notes", header: "Notes", cell: (b) => b.notes || "—" },
          { id: "thresholds", header: "Evaluation thresholds", cell: (b) => thresholdsText(b.thresholds) },
          { id: "created", header: "Created", cell: (b) => `${formatDateTime(b.createdAt)} by ${b.createdBy}` },
        ]}
        empty={
          <EmptyState
            title="No blueprint versions yet"
            description="POST /projects/{id}/blueprints with a spec and a system prompt creates version 1; the deploy job (POST /projects/{id}/deploy, or a release run) puts it on Dev."
          />
        }
      />
      {current ? <BlueprintDetail project={project} blueprint={current} env={env} setEnv={setEnv} /> : null}
    </SpaceBetween>
  );
}

function BlueprintDetail({ project, blueprint, env, setEnv }: { project: Project; blueprint: Blueprint; env: Env; setEnv: (env: Env) => void }) {
  const [requested, setRequested] = useState(false);
  const render = useRenderBlueprint(project.projectId, blueprint.version, env, requested);
  useEffect(() => {
    setRequested(false);
  }, [blueprint.version, env]);
  const unresolved = isApiError(render.error) && render.error.code === "BLUEPRINT_UNRESOLVED";
  const invalid = isApiError(render.error) && render.error.code === "BLUEPRINT_INVALID";
  const spec = blueprint.spec as Record<string, unknown>;
  return (
    <Container header={<Header variant="h2" description={`Model ${blueprint.model}; ${thresholdsText(blueprint.thresholds)}`}>{`Version ${blueprint.version}`}</Header>}>
      <SpaceBetween size="l">
        <KeyValuePairs
          columns={3}
          items={[
            { label: "Created", value: `${formatDateTime(blueprint.createdAt)} by ${blueprint.createdBy}` },
            { label: "Notes", value: blueprint.notes || "—" },
            { label: "System prompt", value: `${blueprint.systemPrompt.length.toLocaleString("en-US")} characters` },
          ]}
        />
        <Container
          variant="stacked"
          header={
            <Header
              variant="h3"
              description="GET /projects/{id}/blueprints/{version}/render?env= is a dry run: the CreateHarness payload with the environment's gateway, memory, role and key; nothing is created."
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <FormField>
                    <SegmentedControl
                      selectedId={env}
                      options={[
                        { id: "dev", text: "Dev" },
                        { id: "prod", text: "Prod" },
                      ]}
                      onChange={({ detail }) => setEnv(detail.selectedId as Env)}
                    />
                  </FormField>
                  <Button variant="primary" onClick={() => (requested ? void render.refetch() : setRequested(true))} loading={render.isFetching} data-testid="render-preview">
                    Render preview
                  </Button>
                </SpaceBetween>
              }
            >
              Render preview
            </Header>
          }
        >
          {!requested ? (
            <Box color="text-body-secondary">Choose the environment and render to see the harness payload.</Box>
          ) : render.isLoading ? (
            <Box>
              <Spinner /> Rendering for {env}…
            </Box>
          ) : unresolved || invalid ? (
            <Alert type="warning" header={unresolved ? `The blueprint cannot be resolved for ${env} yet` : "The blueprint is invalid"}>
              <SpaceBetween size="xs">
                <Box>{render.error?.message}</Box>
                {unresolved ? (
                  <Box variant="small">
                    Rendering needs the environment's registered gateway and memory (PUT /projects/{"{id}"}/resources) and the model key's credential provider (POST
                    /projects/{"{id}"}/keys, or the deploy job creates it). Register what is missing and render again.
                  </Box>
                ) : null}
                {isApiError(render.error) && render.error.details ? <JsonBlock value={render.error.details} maxHeight={220} label="details" /> : null}
              </SpaceBetween>
            </Alert>
          ) : render.error ? (
            <ErrorAlert header="Render failed" error={render.error} onRetry={() => void render.refetch()} />
          ) : (
            <JsonBlock value={render.data} label="harness payload" />
          )}
        </Container>
        <ExpandableSection headerText="Spec (as stored)" variant="footer">
          <JsonBlock value={spec} label="spec" />
        </ExpandableSection>
        <ExpandableSection headerText="System prompt" variant="footer">
          <JsonBlock value={blueprint.systemPrompt} label="system prompt" maxHeight={360} />
        </ExpandableSection>
      </SpaceBetween>
    </Container>
  );
}
