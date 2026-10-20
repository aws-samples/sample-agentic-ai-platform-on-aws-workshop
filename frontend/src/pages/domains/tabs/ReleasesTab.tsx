import Button from "@cloudscape-design/components/button";
import Link from "@cloudscape-design/components/link";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Table from "@cloudscape-design/components/table";
import { useNavigate } from "react-router-dom";

import { useReleases } from "../../../api/hooks";
import type { Release } from "../../../api/types";
import { useMe } from "../../../auth/MeContext";
import { EmptyState } from "../../../components/EmptyState";
import { ErrorAlert } from "../../../components/ErrorAlert";
import { InfoHeader } from "../../../components/InfoPopover";
import { ReleaseStatus } from "../../../components/StatusBadge";
import { formatDateTime } from "../../../utils/format";
import { evaluationIsPrevious, runProgressLabel } from "../../releases/releaseHelpers";

/** "deploying…" / "evaluating…" while a run is in progress; "previous run: …" in DRAFT, where the evaluation is the last run's verdict. */
function evaluationCell(release: Release): string {
  const progress = runProgressLabel(release);
  if (progress) {
    return progress;
  }
  if (!release.evaluation) {
    return "not evaluated";
  }
  const verdict = release.evaluation.passed ? "PASSED" : `BLOCKED (${release.evaluation.failures?.length ?? 0} failures)`;
  return evaluationIsPrevious(release) ? `previous run: ${verdict}` : verdict;
}

export function ReleasesTab({ projectId }: { projectId: string }) {
  // a run (deploy + dataset) or a promotion in progress: refresh every 5 s, like the Releases screen
  const releases = useReleases(projectId, {
    refetchInterval: (query) => ((query.state.data ?? []).some((r) => r.status === "EVALUATING" || r.status === "PROMOTING") ? 5000 : false),
  });
  const navigate = useNavigate();
  const me = useMe();
  const rows = [...(releases.data ?? [])].sort((a, b) => (a.createdAt < b.createdAt ? 1 : -1));
  const releasesHref = `/releases?projectId=${encodeURIComponent(projectId)}`;
  return (
    <SpaceBetween size="l">
      {releases.error ? <ErrorAlert header="Releases could not be loaded" error={releases.error} onRetry={() => void releases.refetch()} /> : null}
      <Table
        header={
          <InfoHeader
            counter={releases.data ? `(${releases.data.length})` : undefined}
            help="Evaluation verdicts, approvals and promotions happen on the Releases screen."
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button onClick={() => navigate(releasesHref)}>Open Releases</Button>
                {me.can("releases:create") ? (
                  <Button variant="primary" onClick={() => navigate(`${releasesHref}&create=1`)} data-testid="create-release-from-project">
                    Create release
                  </Button>
                ) : null}
              </SpaceBetween>
            }
          >
            Releases
          </InfoHeader>
        }
        items={rows}
        loading={releases.isLoading}
        loadingText="Loading releases"
        trackBy="releaseId"
        columnDefinitions={[
          {
            id: "releaseId",
            header: "Release",
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
          { id: "version", header: "Blueprint version", cell: (r) => `v${r.blueprintVersion}` },
          { id: "notes", header: "Notes", minWidth: 200, cell: (r) => r.notes || "—" },
          { id: "status", header: "Status", cell: (r) => <ReleaseStatus status={r.status} /> },
          {
            id: "verdict",
            header: "Evaluation",
            cell: (r) => evaluationCell(r),
          },
          { id: "approval", header: "Decision", cell: (r) => (r.approval ? `${r.approval.decision} by ${r.approval.by}` : "—") },
          { id: "created", header: "Created", cell: (r) => `${formatDateTime(r.createdAt)} by ${r.createdBy}` },
        ]}
        empty={
          <EmptyState
            title="No releases yet"
            description="Create one from a blueprint version: the platform deploys it to Dev, runs the evaluation dataset and gates it on the thresholds."
          />
        }
      />
    </SpaceBetween>
  );
}
