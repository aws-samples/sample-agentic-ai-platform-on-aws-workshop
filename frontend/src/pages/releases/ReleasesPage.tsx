import Button from "@cloudscape-design/components/button";
import ContentLayout from "@cloudscape-design/components/content-layout";
import Header from "@cloudscape-design/components/header";
import Link from "@cloudscape-design/components/link";
import Select, { type SelectProps } from "@cloudscape-design/components/select";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Table from "@cloudscape-design/components/table";
import { useMemo } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { useAllReleases, useProjects } from "../../api/hooks";
import type { Release } from "../../api/types";
import { useMe } from "../../auth/MeContext";
import { EmptyState } from "../../components/EmptyState";
import { ErrorAlert } from "../../components/ErrorAlert";
import { ReleaseStatus } from "../../components/StatusBadge";
import { useBreadcrumbs } from "../../layout/breadcrumbs";
import { formatDateTime } from "../../utils/format";
import { CreateReleaseModal } from "./CreateReleaseModal";
import { STATUS_ORDER, describeFailure, evaluationIsPrevious, runProgressLabel, sortNewestFirst } from "./releaseHelpers";
import Badge from "@cloudscape-design/components/badge";

const LIVE_STATUSES: ReadonlyArray<string> = ["EVALUATING", "PROMOTING"];

/** "deploying…" / "evaluating…" while a run is in progress; in DRAFT the evaluation is the previous run's (the last run failed) and says so. */
function evaluationSummary(release: Release): string {
  const progress = runProgressLabel(release);
  if (progress) {
    return progress;
  }
  const evaluation = release.evaluation;
  if (!evaluation) {
    return "not evaluated";
  }
  const names = (evaluation.failures ?? []).map((f) => (typeof f === "string" ? f : f.evaluator ?? "?"));
  if (evaluationIsPrevious(release)) {
    return evaluation.passed ? "previous run: PASSED" : `previous run: BLOCKED (${names.join(", ") || "thresholds missed"})`;
  }
  if (evaluation.passed) {
    return "PASSED: every threshold met";
  }
  return `BLOCKED: ${names.join(", ") || "thresholds missed"}`;
}

export function ReleasesPage() {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const me = useMe();
  const projectId = params.get("projectId") ?? "";
  const status = params.get("status") ?? "";
  const createOpen = params.get("create") === "1";
  useBreadcrumbs([{ text: "Releases", href: "/releases" }]);
  // a run (deploy + dataset, 6 to 7 minutes) or a promotion in progress: refresh the list every 5 s
  const releases = useAllReleases({ refetchInterval: (query) => ((query.state.data ?? []).some((r) => LIVE_STATUSES.includes(r.status)) ? 5000 : false) });
  const projects = useProjects();

  const rows = useMemo(
    () => sortNewestFirst((releases.data ?? []).filter((r) => (!projectId || r.projectId === projectId) && (!status || r.status === status))),
    [releases.data, projectId, status],
  );
  const projectOptions: SelectProps.Option[] = [
    { value: "", label: "All projects" },
    ...[...(projects.data ?? [])]
      .sort((a, b) => a.projectId.localeCompare(b.projectId))
      .map((p) => ({ value: p.projectId, label: p.name || p.projectId, description: p.projectId })),
  ];
  const statusOptions: SelectProps.Option[] = [
    { value: "", label: "All statuses" },
    ...STATUS_ORDER.map((s) => ({ value: s, label: s.replace(/_/g, " ").toLowerCase().replace(/^./, (c) => c.toUpperCase()) })),
  ];
  const setParam = (key: string, value: string | null) => {
    const next = new URLSearchParams(params);
    if (value) {
      next.set(key, value);
    } else {
      next.delete(key);
    }
    setParams(next, { replace: true });
  };
  const canCreate = me.can("releases:create");

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description="Every release candidate of every project, newest first (GET /releases): evaluation verdict, approval and promotion. Open a release to see the scores against the thresholds and to act on it."
          actions={
            canCreate ? (
              <Button variant="primary" onClick={() => setParam("create", "1")} data-testid="create-release">
                Create release
              </Button>
            ) : undefined
          }
        >
          Releases
        </Header>
      }
    >
      <SpaceBetween size="l">
        {releases.error ? <ErrorAlert header="Releases could not be loaded" error={releases.error} onRetry={() => void releases.refetch()} /> : null}
        <Table
          header={<Header variant="h2" counter={releases.data ? `(${rows.length})` : undefined}>Releases</Header>}
          filter={
            <SpaceBetween direction="horizontal" size="xs">
              <Select
                selectedOption={projectOptions.find((o) => o.value === projectId) ?? projectOptions[0]}
                options={projectOptions}
                onChange={({ detail }) => setParam("projectId", detail.selectedOption.value || null)}
                ariaLabel="Filter by project"
                data-testid="releases-project-filter"
              />
              <Select
                selectedOption={statusOptions.find((o) => o.value === status) ?? statusOptions[0]}
                options={statusOptions}
                onChange={({ detail }) => setParam("status", detail.selectedOption.value || null)}
                ariaLabel="Filter by status"
              />
            </SpaceBetween>
          }
          items={rows}
          loading={releases.isLoading}
          loadingText="Loading releases"
          trackBy="releaseId"
          wrapLines
          columnDefinitions={[
            {
              id: "releaseId",
              header: "Release",
              minWidth: 160,
              isRowHeader: true,
              cell: (r) => (
                <Link
                  href={`/releases/${r.releaseId}`}
                  onFollow={(event) => {
                    event.preventDefault();
                    navigate(`/releases/${r.releaseId}`);
                  }}
                >
                  {r.releaseId}
                </Link>
              ),
            },
            { id: "project", header: "Project", minWidth: 140, cell: (r) => r.projectId },
            { id: "version", header: "Blueprint", minWidth: 100, cell: (r) => <Badge color="blue">v{r.blueprintVersion}</Badge> },
            { id: "status", header: "Status", minWidth: 150, cell: (r) => <ReleaseStatus status={r.status} /> },
            {
              id: "evaluation",
              header: "Evaluation",
              minWidth: 220,
              cell: (r) => (
                <span title={(r.evaluation?.failures ?? []).map(describeFailure).join("\n")}>{evaluationSummary(r)}</span>
              ),
            },
            { id: "approval", header: "Decision", minWidth: 220, cell: (r) => (r.approval ? `${r.approval.decision} by ${r.approval.by} (${formatDateTime(r.approval.at)})` : "—") },
            {
              id: "promotion",
              header: "Promotion",
              minWidth: 220,
              cell: (r) =>
                r.status === "PROMOTED" && r.promotion
                  ? `harness v${r.promotion.harnessVersion ?? "?"} on ${r.promotion.endpoint ?? "live"} (${formatDateTime(r.promotion.at)})`
                  : r.status === "PROMOTING"
                    ? "in progress"
                    : r.status === "FAILED"
                      ? r.error?.code ?? "failed"
                      : "—",
            },
            { id: "created", header: "Created", minWidth: 220, cell: (r) => `${formatDateTime(r.createdAt)} by ${r.createdBy}` },
            { id: "notes", header: "Notes", minWidth: 220, cell: (r) => r.notes || "—" },
          ]}
          empty={
            <EmptyState
              title={projectId || status ? "No releases match the filter" : "No releases yet"}
              description="Create the first release from a blueprint version: the platform deploys it to Dev, evaluates it and gates it on the thresholds."
              action={canCreate ? <Button onClick={() => setParam("create", "1")}>Create release</Button> : undefined}
            />
          }
        />
      </SpaceBetween>
      {canCreate ? <CreateReleaseModal visible={createOpen} defaultProjectId={projectId || undefined} onDismiss={() => setParam("create", null)} /> : null}
    </ContentLayout>
  );
}
