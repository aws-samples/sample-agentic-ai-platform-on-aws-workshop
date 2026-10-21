import Alert from "@cloudscape-design/components/alert";
import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Container from "@cloudscape-design/components/container";
import ContentLayout from "@cloudscape-design/components/content-layout";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Header from "@cloudscape-design/components/header";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import Link from "@cloudscape-design/components/link";
import Modal from "@cloudscape-design/components/modal";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Spinner from "@cloudscape-design/components/spinner";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Steps, { type StepsProps } from "@cloudscape-design/components/steps";
import Table from "@cloudscape-design/components/table";
import Textarea from "@cloudscape-design/components/textarea";
import { useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";

import { describeError, isApiError } from "../../api/client";
import {
  isTerminalJob,
  jobError,
  useApproveRelease,
  useBlueprints,
  useJob,
  usePromoteRelease,
  useRejectRelease,
  useRelease,
  useReleases,
  useResources,
  useRunRelease,
} from "../../api/hooks";
import { qk } from "../../api/queryKeys";
import type { Job, JobError, PromotionRegistryRecord, Release, ReleasePipeline, ReleaseStatus as ReleaseStatusValue } from "../../api/types";
import { useMe } from "../../auth/MeContext";
import { ConfirmModal } from "../../components/ConfirmModal";
import { CopyText } from "../../components/CopyText";
import { EmptyState } from "../../components/EmptyState";
import { ErrorAlert } from "../../components/ErrorAlert";
import { InfoFormField, InfoHeader } from "../../components/InfoPopover";
import { JsonBlock } from "../../components/JsonBlock";
import { GenericStatus, ReleaseStatus } from "../../components/StatusBadge";
import { useBreadcrumbs } from "../../layout/breadcrumbs";
import { formatErrorContent, useNotifications } from "../../layout/notifications";
import { formatDateTime, formatNumber, formatSeconds, parseDate } from "../../utils/format";
import {
  canDecide,
  canPromote,
  canRun,
  describeBlueprintThreshold,
  describeFailure,
  describeThreshold,
  evaluationIsPrevious,
  formatScore,
  nextStep,
  pipelineStuck,
  promotionStuck,
  runFailed,
  scoreRows,
  sortNewestFirst,
} from "./releaseHelpers";

const APPROVER_ONLY = "Approvers only: the API answers 403 to admins and developers (separation of duties).";
const DEVELOPER_ONLY = "Developers and admins only.";
const LIVE_STATUSES: ReadonlyArray<string> = ["EVALUATING", "PROMOTING"];

/**
 * `/releases/:releaseId`. react-router keeps the component instance when only the param changes, so the per-release state
 * (the run or promotion job being followed, an open decision modal, its comment) is scoped with `key={releaseId}`: opening
 * release B from A's version history gets a fresh instance instead of A's finished job rendered on B.
 */
export function ReleaseDetailPage() {
  const { releaseId = "" } = useParams();
  return <ReleaseDetail key={releaseId} releaseId={releaseId} />;
}

function ReleaseDetail({ releaseId }: { releaseId: string }) {
  const navigate = useNavigate();
  const me = useMe();
  const notifications = useNotifications();
  const queryClient = useQueryClient();
  // a run (deploy + dataset) or a promotion in progress: the page refreshes every 5 s on top of the 2 s job polling
  const release = useRelease(releaseId, { refetchInterval: (query) => (LIVE_STATUSES.includes(query.state.data?.status ?? "") ? 5000 : false) });
  const r = release.data;
  useBreadcrumbs([
    { text: "Releases", href: "/releases" },
    { text: releaseId, href: `/releases/${releaseId}` },
  ]);
  const history = useReleases(r?.projectId);
  const blueprints = useBlueprints(r?.projectId);
  const prodResources = useResources(r?.projectId, "prod");
  const approve = useApproveRelease();
  const reject = useRejectRelease();
  const promote = usePromoteRelease();
  const run = useRunRelease();
  const [decision, setDecision] = useState<"APPROVED" | "REJECTED" | null>(null);
  const [comment, setComment] = useState("");
  const [promoteOpen, setPromoteOpen] = useState(false);
  const [runOpen, setRunOpen] = useState(false);
  const [trackedJobId, setTrackedJobId] = useState<string | null>(null);
  const [trackedRunJobId, setTrackedRunJobId] = useState<string | null>(null);

  // The promotion job: the one we just started, or the one the release carries while PROMOTING (another browser, the workshop command).
  const jobId = r?.status === "PROMOTING" ? (r.promotion?.jobId ?? trackedJobId) : trackedJobId;
  const job = useJob(jobId);
  // The run: while EVALUATING the release names the job to poll now (the "release" job during the deploy stage, then the "evaluate"
  // job); the one we just started covers the moment before the release has been refetched.
  const runJobId = r?.status === "EVALUATING" ? (r.pipeline?.jobId ?? trackedRunJobId) : trackedRunJobId;
  const runJob = useJob(runJobId);
  const invalidateAfterJob = () => {
    void queryClient.invalidateQueries({ queryKey: qk.release(releaseId) });
    void queryClient.invalidateQueries({ queryKey: ["releases"] });
    void queryClient.invalidateQueries({ queryKey: ["resources", r?.projectId] });
    void queryClient.invalidateQueries({ queryKey: ["keys", r?.projectId] });
    void queryClient.invalidateQueries({ queryKey: ["spend", r?.projectId] }); // the worker created the Prod key, or the Dev key on a first deploy
  };
  useEffect(() => {
    if (job.isTerminal && jobId) {
      invalidateAfterJob();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- react to the terminal transition only
  }, [job.isTerminal, jobId]);
  useEffect(() => {
    if (runJob.isTerminal && runJobId) {
      invalidateAfterJob(); // the deploy stage done (the release now names the evaluate job), or the dataset scored (BLOCKED / PENDING_APPROVAL)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- react to the terminal transition only
  }, [runJob.isTerminal, runJobId]);
  // The run's outcome: one notification when the release leaves EVALUATING while this page is open (started here, elsewhere or by the CLI).
  const previousStatus = useRef<ReleaseStatusValue | undefined>(undefined);
  useEffect(() => {
    const previous = previousStatus.current;
    previousStatus.current = r?.status;
    if (previous !== "EVALUATING" || !r || r.status === "EVALUATING") {
      return;
    }
    setTrackedRunJobId(null);
    if (r.status === "PENDING_APPROVAL") {
      notifications.success(`Release ${releaseId} evaluated: PENDING_APPROVAL`, "Every threshold met; an approver can decide now.");
    } else if (r.status === "BLOCKED") {
      notifications.warning(`Release ${releaseId} evaluated: BLOCKED`, "At least one threshold was missed; the scores show which.");
    } else if (runFailed(r) && r.error) {
      notifications.error(`Evaluation run of ${releaseId} failed`, new Error(`${r.error.code}: ${r.error.message}`));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- react to the status transition only
  }, [r?.status]);
  useEffect(() => {
    if (job.isTerminal && job.job && jobId === trackedJobId) {
      if (job.job.status === "SUCCEEDED") {
        notifications.success(`Release ${releaseId} promoted`, "The Prod harness runs the approved version; the live endpoint points at it.");
      } else {
        notifications.error(`Promotion of ${releaseId} failed`, new Error(`${jobError(job.job)?.code ?? "FAILED"}: ${jobError(job.job)?.message ?? ""}`));
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- fire once per terminal job
  }, [job.isTerminal]);

  const handleConflict = (error: unknown) => {
    const { status } = describeError(error);
    if (status === 409) {
      notifications.error("The release changed under you: the page was refreshed", error);
      void release.refetch();
      void history.refetch();
    }
  };

  if (release.isLoading) {
    return (
      <ContentLayout header={<Header variant="h1">{releaseId}</Header>}>
        <Spinner /> Loading release…
      </ContentLayout>
    );
  }
  if (release.error || !r) {
    return (
      <ContentLayout header={<Header variant="h1">{releaseId}</Header>}>
        <ErrorAlert header={`Release ${releaseId} could not be loaded`} error={release.error ?? new Error("not found")} onRetry={() => void release.refetch()} />
      </ContentLayout>
    );
  }

  const isApprover = me.can("releases:decide");
  const canOpenProject = me.canAccessScreen("domains"); // approvers have no project detail screen (RequireRole would refuse it)
  const decideReason = !isApprover ? APPROVER_ONLY : !canDecide(r.status) ? `Only PENDING_APPROVAL releases can be approved or rejected (this one is ${r.status}).` : undefined;
  // A PROMOTING release whose job failed, vanished or stalled for 15 minutes may be promoted again (the API's rule; a 409 wins the race).
  const jobMissing = isApiError(job.error) && job.error.status === 404;
  const promoStuck = promotionStuck(r, job.job, jobMissing);
  const promoteReason = !me.can("releases:promote")
    ? APPROVER_ONLY
    : r.status === "PROMOTING"
      ? promoStuck
        ? undefined
        : "A promotion is already running."
      : !canPromote(r.status)
        ? `Only APPROVED releases can be promoted (this one is ${r.status}); a FAILED promotion can be retried.`
        : undefined;
  const retry = r.status === "FAILED" || promoStuck;
  // Same rule for the run: an EVALUATING release whose job failed, vanished or stalled for 15 minutes may be run again.
  const runJobMissing = isApiError(runJob.error) && runJob.error.status === 404;
  const runStuck = pipelineStuck(r, runJob.job, runJobMissing);
  const stuck = promoStuck || runStuck;
  const runReason = !me.can("releases:create")
    ? DEVELOPER_ONLY
    : r.status === "EVALUATING"
      ? runStuck
        ? undefined
        : "A run is in progress."
      : !canRun(r.status)
        ? `Only DRAFT or BLOCKED releases can be run (this one is ${r.status}).`
        : undefined;
  // DRAFT with the error, or BLOCKED whose "Run again" could not start (pipeline FAILED, error set): the run failed, run it again.
  const lastRunFailed = runFailed(r);
  const runAgain = !!r.evaluation || lastRunFailed || runStuck;
  // In DRAFT or EVALUATING the evaluation on the release is the previous run's: the next (or running) run replaces it.
  const previousVerdict = evaluationIsPrevious(r);
  const blueprint = blueprints.data?.find((b) => b.version === r.blueprintVersion);
  const datasetName = r.evaluation?.datasetName ?? datasetOf(blueprint?.spec);
  const rows = scoreRows(r.evaluation);
  const failures = (r.evaluation?.failures ?? []).map(describeFailure);
  const closeDecision = () => {
    setDecision(null);
    approve.reset();
    reject.reset();
  };

  const submitDecision = () => {
    if (!decision) {
      return;
    }
    const mutation = decision === "APPROVED" ? approve : reject;
    mutation.mutate(
      { releaseId, comment: comment.trim() },
      {
        onSuccess: (updated) => {
          notifications.success(`Release ${releaseId} ${updated.status.toLowerCase()} by ${updated.approval?.by ?? me.me?.username ?? "you"}`);
          setDecision(null);
          setComment("");
        },
        onError: handleConflict,
      },
    );
  };
  const startPromotion = () => {
    promote.mutate(releaseId, {
      onSuccess: (accepted) => {
        setTrackedJobId(accepted.jobId);
        setPromoteOpen(false);
        notifications.info(`Promotion job ${accepted.jobId} started`, "Prod key, Prod harness version, live endpoint, online evaluation and the Catalogue's agent record: about 20 seconds.");
      },
      onError: handleConflict,
    });
  };
  const startRun = () => {
    run.mutate(releaseId, {
      onSuccess: (accepted) => {
        setTrackedRunJobId(accepted.jobId);
        setRunOpen(false);
        notifications.info(
          "Evaluation run started",
          `${accepted.stage === "EVALUATING" ? "The Dev harness already runs" : "Deploying"} blueprint v${r.blueprintVersion}${accepted.stage === "EVALUATING" ? "; running" : " to Dev, then running"} the evaluation dataset: about ${accepted.stage === "EVALUATING" ? "5 to 6" : "6 to 7"} minutes. The Evaluation gate below shows the progress.`,
        );
      },
      onError: handleConflict,
    });
  };

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description={`${r.projectId} · blueprint v${r.blueprintVersion}${r.notes ? ` · ${r.notes}` : ""}`}
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              {canOpenProject ? <Button onClick={() => navigate(`/projects/${r.projectId}?tab=releases`)}>Open project</Button> : null}
              {r.status === "PROMOTED" ? <Button onClick={() => navigate(`/playground?project=${encodeURIComponent(r.projectId)}&env=prod`)}>Try it in the Playground (Prod)</Button> : null}
              <Button
                variant={!runReason ? "primary" : "normal"}
                onClick={() => setRunOpen(true)}
                disabled={!!runReason}
                disabledReason={runReason}
                loading={run.isPending}
                data-testid="run-release"
              >
                {runAgain ? "Run again" : "Run evaluation"}
              </Button>
              <Button onClick={() => setDecision("REJECTED")} disabled={!!decideReason} disabledReason={decideReason} data-testid="reject-release">
                Reject
              </Button>
              <Button variant="primary" onClick={() => setDecision("APPROVED")} disabled={!!decideReason} disabledReason={decideReason} data-testid="approve-release">
                Approve
              </Button>
              <Button
                variant={r.status === "APPROVED" || retry ? "primary" : "normal"}
                onClick={() => setPromoteOpen(true)}
                disabled={!!promoteReason}
                disabledReason={promoteReason}
                loading={promote.isPending}
                data-testid="promote-release"
              >
                {retry ? "Promote again" : "Promote to Prod"}
              </Button>
            </SpaceBetween>
          }
        >
          Release {r.releaseId} <ReleaseStatus status={r.status} />
        </Header>
      }
    >
      <SpaceBetween size="l">
        <Alert
          type={r.status === "FAILED" ? "error" : r.status === "BLOCKED" || r.status === "REJECTED" || stuck || lastRunFailed ? "warning" : "info"}
          header={`Status: ${r.status.replace(/_/g, " ")}${stuck ? " (stalled)" : ""}`}
        >
          {nextStep(r, stuck)}
        </Alert>
        {r.status === "FAILED" && r.error ? (
          <Alert type="error" header={`Promotion failed: ${r.error.code}`}>
            <SpaceBetween size="xs">
              <Box>{r.error.message}</Box>
              {r.error.jobId ? <Box variant="small">Job {r.error.jobId}</Box> : null}
              {r.error.details ? <JsonBlock value={r.error.details} maxHeight={200} label="details" /> : null}
            </SpaceBetween>
          </Alert>
        ) : null}
        {lastRunFailed && r.error ? (
          <Alert type="error" header={`Evaluation run failed: ${r.error.code}`}>
            <SpaceBetween size="xs">
              <Box>{r.error.message}</Box>
              {r.status === "BLOCKED" ? <Box>The run could not start, so the release kept its BLOCKED status and the previous verdict.</Box> : null}
              <Box variant="small">
                {(() => {
                  const stage = r.pipeline?.error?.stage ?? r.error.stage;
                  const jobId = r.pipeline?.error?.jobId ?? r.error.jobId;
                  return `${stage ? `Stage ${stage}` : "Stage unknown"}${jobId ? ` · job ${jobId}` : ""}`;
                })()}
              </Box>
              {r.error.details ? <JsonBlock value={r.error.details} maxHeight={200} label="details" /> : null}
            </SpaceBetween>
          </Alert>
        ) : null}

        <Container
          header={
            <InfoHeader help="Dataset evaluation: the mean of every evaluator against the thresholds of the blueprint version. Every built-in scale has 1 = good; the Harmfulness threshold caps the harm rate (1 - mean).">
              Evaluation gate
            </InfoHeader>
          }
        >
          <SpaceBetween size="l">
            {r.pipeline ? (
              <EvaluationRunPanel
                release={r}
                pipeline={r.pipeline}
                job={runJob.job}
                jobTimedOut={runJob.timedOut}
                keepWaiting={runJob.keepWaiting}
                jobLoadError={runJob.error}
                stuck={runStuck}
              />
            ) : null}
            {!r.evaluation ? (
              <SpaceBetween size="m">
                <EmptyState
                  title={r.status === "EVALUATING" ? "Evaluation in progress" : "Not evaluated yet"}
                  description={
                    r.status === "EVALUATING"
                      ? "The scores against the thresholds below appear here once the dataset has been scored."
                      : `Run the evaluation to deploy and score this version: the platform deploys blueprint v${r.blueprintVersion} to Dev if the Dev harness does not run it yet, runs the dataset against it and applies the thresholds below.`
                  }
                />
                {blueprint ? (
                  <KeyValuePairs
                    columns={4}
                    items={Object.entries(blueprint.thresholds ?? {}).map(([name, value]) => ({
                      label: name,
                      value: describeBlueprintThreshold(name, value),
                    }))}
                  />
                ) : null}
              </SpaceBetween>
            ) : (
              <SpaceBetween size="l">
                {previousVerdict ? (
                  <Alert type="info" header="Verdict of the previous run" data-testid="previous-verdict">
                    {r.status === "EVALUATING"
                      ? "Previous run's verdict: the run in progress replaces it once the dataset has been scored."
                      : "Previous run's verdict: the next run replaces it."}
                  </Alert>
                ) : null}
                <KeyValuePairs
                  columns={4}
                  items={[
                    {
                      label: previousVerdict ? "Verdict (previous run)" : "Verdict",
                      value: <StatusIndicator type={r.evaluation.passed ? "success" : "error"}>{r.evaluation.passed ? "PASSED: every threshold met" : `BLOCKED: ${failures.length} failure${failures.length === 1 ? "" : "s"}`}</StatusIndicator>,
                    },
                    { label: "Dataset", value: r.evaluation.datasetName ?? "—" },
                    { label: "Scenarios", value: r.evaluation.scenarioCount ?? "—" },
                    { label: "Run id", value: r.evaluation.runId ? <CopyText value={r.evaluation.runId} max={36} /> : "—" },
                    { label: "Completed", value: formatDateTime(r.evaluation.completedAt) },
                    { label: "Submitted by", value: r.evaluation.submittedBy ?? "—" },
                    { label: "Failed scenarios", value: r.evaluation.failedScenarios?.length ? r.evaluation.failedScenarios.join(", ") : "none" },
                  ]}
                />
                {failures.length ? (
                  <Alert type={previousVerdict ? "warning" : "error"} header={previousVerdict ? "Thresholds missed by the previous run" : "Thresholds missed"}>
                    <ul style={{ margin: 0, paddingInlineStart: 20 }}>
                      {failures.map((text) => (
                        <li key={text}>{text}</li>
                      ))}
                    </ul>
                  </Alert>
                ) : null}
                <Table
                  variant="embedded"
                  header={
                    <Header variant="h3" counter={`(${rows.length})`}>
                      {previousVerdict ? "Scores vs thresholds (previous run)" : "Scores vs thresholds"}
                    </Header>
                  }
                  items={rows}
                  trackBy="key"
                  wrapLines
                  columnDefinitions={[
                    { id: "evaluator", header: "Evaluator", cell: (row) => row.evaluator, isRowHeader: true },
                    { id: "mean", header: "Mean score", cell: (row) => formatScore(row.mean) },
                    { id: "passRate", header: "Pass rate", cell: (row) => (row.passRate === null ? "—" : `${Math.round(row.passRate * 100)}%`) },
                    { id: "count", header: "Scored / errors", cell: (row) => (row.count === null ? "—" : `${formatNumber(row.count)} / ${formatNumber(row.errors ?? 0)}`) },
                    { id: "threshold", header: "Threshold", cell: (row) => describeThreshold(row) },
                    {
                      id: "actual",
                      header: "Compared value",
                      cell: (row) => (row.key === "_scenarios" ? formatNumber(row.actual) : formatScore(row.actual)),
                    },
                    {
                      id: "result",
                      header: "Result",
                      cell: (row) =>
                        row.passed === null ? (
                          <StatusIndicator type="info">not gated</StatusIndicator>
                        ) : row.passed ? (
                          <StatusIndicator type="success">pass</StatusIndicator>
                        ) : (
                          <StatusIndicator type="error">fail</StatusIndicator>
                        ),
                    },
                  ]}
                  empty={<EmptyState title="No scores" />}
                />
                <ScenarioResults release={r} />
              </SpaceBetween>
            )}
          </SpaceBetween>
        </Container>

        <ColumnLayout columns={2}>
          <Container header={<InfoHeader help="POST /releases/{id}/approve | reject by a member of the approvers group">Approval</InfoHeader>}>
            {r.approval ? (
              <KeyValuePairs
                columns={2}
                items={[
                  { label: "Decision", value: <GenericStatus status={r.approval.decision === "APPROVED" ? "SUCCEEDED" : "FAILED"} label={r.approval.decision} /> },
                  { label: "By", value: r.approval.by },
                  { label: "At", value: formatDateTime(r.approval.at) },
                  { label: "Comment", value: r.approval.comment || "—" },
                ]}
              />
            ) : (
              <EmptyState
                title={r.status === "PENDING_APPROVAL" ? "Awaiting an approver" : "No decision yet"}
                description={
                  r.status === "PENDING_APPROVAL"
                    ? isApprover
                      ? "Review the scores above, then Approve or Reject with a comment."
                      : "Only a member of the approvers group can decide (switch persona in the user menu); admins and developers get 403 from the API."
                    : "A release can be approved or rejected once its evaluation passes every threshold."
                }
              />
            )}
          </Container>
          <PromotionPanel release={r} job={job.job} jobTimedOut={job.timedOut} keepWaiting={job.keepWaiting} jobLoadError={job.error} />
        </ColumnLayout>

        <VersionHistory release={r} releases={history.data} loading={history.isLoading} error={history.error} prodResources={prodResources.data} />
      </SpaceBetween>

      <Modal
        visible={!!decision}
        onDismiss={closeDecision}
        header={decision === "APPROVED" ? `Approve release ${r.releaseId}` : `Reject release ${r.releaseId}`}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={closeDecision} disabled={approve.isPending || reject.isPending}>
                Cancel
              </Button>
              <Button variant="primary" onClick={submitDecision} loading={approve.isPending || reject.isPending} data-testid="decision-submit">
                {decision === "APPROVED" ? "Approve" : "Reject"}
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <Box>
            {decision === "APPROVED"
              ? "Approving records your decision on the release; promotion to Prod is a separate step."
              : "Rejecting closes this candidate; the developer creates a new release for another attempt."}
          </Box>
          <InfoFormField label="Comment" help="Stored with the decision" constraintText="Optional">
            <Textarea value={comment} onChange={({ detail }) => setComment(detail.value)} rows={3} data-testid="decision-comment" />
          </InfoFormField>
          {approve.error || reject.error ? (
            <Alert type="error" header="The decision was not recorded">
              {formatErrorContent(approve.error ?? reject.error)}
              {describeError(approve.error ?? reject.error).status === 403 ? " Only the approvers group may decide." : ""}
            </Alert>
          ) : null}
        </SpaceBetween>
      </Modal>

      <ConfirmModal
        visible={promoteOpen}
        header={`Promote release ${r.releaseId} to Prod`}
        confirmText={retry ? "Promote again" : "Promote"}
        loading={promote.isPending}
        error={promote.error}
        onConfirm={startPromotion}
        onDismiss={() => {
          promote.reset();
          setPromoteOpen(false);
        }}
      >
        <SpaceBetween size="xs">
          {promoStuck ? (
            <Alert type="warning" header="The previous promotion stalled">
              Job {r.promotion?.jobId ?? "?"} {jobMissing ? "no longer exists (jobs expire after 24 hours)" : job.job && job.isTerminal ? `ended ${job.job.status} without moving the release` : "has not been updated for 15 minutes (the worker was probably killed)"}.
              Promoting again starts a new job; the API answers 409 if the first one has moved after all.
            </Alert>
          ) : null}
          <Box>
            The platform ensures the Prod key for {blueprint?.model ?? "the blueprint model"}, renders blueprint v{r.blueprintVersion} for Prod (Prod gateway, memory, role, key), creates or updates the Prod
            harness, repoints the <code>live</code> endpoint to the new version and enables online evaluation. About 20 seconds; the job progress shows below.
          </Box>
          <Box variant="small">POST /releases/{"{id}"}/promote → 202 with a job id; the release is PROMOTING until the worker finishes.</Box>
        </SpaceBetween>
      </ConfirmModal>

      <ConfirmModal
        visible={runOpen}
        header={runAgain ? `Run the evaluation of release ${r.releaseId} again` : `Run the evaluation of release ${r.releaseId}`}
        confirmText={runAgain ? "Run again" : "Run evaluation"}
        loading={run.isPending}
        error={run.error}
        onConfirm={startRun}
        onDismiss={() => {
          run.reset();
          setRunOpen(false);
        }}
      >
        <SpaceBetween size="xs">
          {runStuck ? (
            <Alert type="warning" header="The previous run stalled">
              Job {r.pipeline?.jobId ?? "?"} {runJobMissing ? "no longer exists (jobs expire after 24 hours)" : runJob.job && runJob.isTerminal ? `ended ${runJob.job.status} without moving the release` : "has not been updated for 15 minutes (the worker was probably killed)"}.
              Running again starts a new job; the API answers 409 if the first one has moved after all.
            </Alert>
          ) : null}
          <Box>
            The platform deploys blueprint v{r.blueprintVersion} to Dev if the Dev harness does not run it yet (about a minute; four to five for a project&apos;s first Dev
            harness), runs the evaluation dataset{datasetName ? ` ${datasetName}` : ""}
            {r.evaluation?.scenarioCount ? ` (${r.evaluation.scenarioCount} scenarios)` : ""} against the Dev harness (the model calls count against the project&apos;s Dev key
            cap) and applies the thresholds of v{r.blueprintVersion}: the release ends BLOCKED or PENDING_APPROVAL. About 6 to 7 minutes; the Evaluation gate shows the
            progress.
          </Box>
          <Box variant="small">POST /releases/{"{id}"}/run → 202 with a job id; the release is EVALUATING until the dataset has been scored.</Box>
        </SpaceBetween>
      </ConfirmModal>
    </ContentLayout>
  );
}

/** The dataset a blueprint version names in `spec.evaluation.dataset` (platform_sdk.blueprint), if any. */
function datasetOf(spec: Record<string, unknown> | undefined): string | null {
  const evaluation = spec?.evaluation;
  if (typeof evaluation !== "object" || evaluation === null) {
    return null;
  }
  const dataset = (evaluation as Record<string, unknown>).dataset;
  return typeof dataset === "string" && dataset ? dataset : null;
}

/**
 * The run of a release as four steps (requested, Dev deployment, dataset evaluation, verdict), derived from `release.pipeline`
 * plus the live status of the job being polled; the elapsed timer ticks while the release is EVALUATING.
 */
function EvaluationRunPanel({
  release,
  pipeline,
  job,
  jobTimedOut,
  keepWaiting,
  jobLoadError,
  stuck,
}: {
  release: Release;
  pipeline: ReleasePipeline;
  job: Job<Record<string, unknown>> | undefined;
  jobTimedOut: boolean;
  keepWaiting: () => void;
  jobLoadError: Error | null;
  stuck: boolean;
}) {
  const running = release.status === "EVALUATING";
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) {
      return;
    }
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [running]);
  const started = parseDate(pipeline.startedAt);
  const ended = running
    ? new Date(now)
    : (parseDate(pipeline.finishedAt) ?? parseDate(release.evaluation?.completedAt) ?? (job && isTerminalJob(job) ? parseDate(job.updatedAt) : null));
  const elapsed = started && ended ? Math.max(0, Math.round((ended.getTime() - started.getTime()) / 1000)) : null;
  const version = `v${release.blueprintVersion}`;

  // The run failed: the pipeline says so (stage FAILED), the release went back to DRAFT with the error, or a BLOCKED release's
  // "Run again" could not start (WORKER_UNAVAILABLE: start_run returned it to BLOCKED with pipeline FAILED and the error).
  const releaseError = runFailed(release) ? release.error : null;
  const runError: JobError | null = pipeline.error ?? releaseError ?? (pipeline.stage === "FAILED" ? jobError(job) : null);
  const failed = pipeline.stage === "FAILED" || !!releaseError;
  // The stage that failed comes from the error itself; guessing from the queued jobs is the fallback for rows written by older builds.
  const failedStage = failed ? (pipeline.error?.stage ?? releaseError?.stage ?? (pipeline.evaluationJobId ? "EVALUATING" : "DEPLOYING")) : null;
  const deployFailed = failed && failedStage === "DEPLOYING";
  const evalFailed = failed && !deployFailed;
  // The release left EVALUATING by another path while the pipeline still says DEPLOYING / EVALUATING (for example a manual
  // POST /releases/{id}/evaluation on a stalled run): the run was superseded, never show it as in progress.
  const superseded = !running && !failed && (pipeline.stage === "DEPLOYING" || pipeline.stage === "EVALUATING");
  const supersededText =
    release.evaluation?.source === "api" ? "superseded: the evaluation was submitted through the API" : "superseded: the release moved on before this run finished";
  const errorText = runError ? `${runError.code}: ${runError.message}` : "the run failed";
  const jobText = job ? ` · job ${job.status}` : "";
  const elapsedText = elapsed !== null ? ` · ${formatSeconds(elapsed)}` : "";
  const liveStatus: StepsProps.Status = stuck ? "warning" : job?.status === "RUNNING" ? "in-progress" : "loading";

  let deployStatus: StepsProps.Status;
  let deployDetails: string;
  if (pipeline.deploySkipped) {
    deployStatus = "success";
    deployDetails = `skipped: the Dev harness already runs blueprint ${version}${pipeline.harnessVersion ? ` (harness version ${pipeline.harnessVersion})` : ""}`;
  } else if (deployFailed) {
    deployStatus = "error";
    deployDetails = errorText;
  } else if (pipeline.stage === "DEPLOYING" && running) {
    deployStatus = liveStatus;
    deployDetails = `${stuck ? "stalled" : "creating or updating the Dev harness"}${jobText}${elapsedText}`;
  } else if (pipeline.stage === "DEPLOYING") {
    deployStatus = "stopped";
    deployDetails = supersededText;
  } else {
    deployStatus = "success";
    deployDetails = pipeline.harnessVersion ? `Dev harness version ${pipeline.harnessVersion}` : "Dev harness updated";
  }

  let evalStatus: StepsProps.Status;
  let evalDetails: string | undefined;
  if (evalFailed) {
    evalStatus = "error";
    evalDetails = errorText;
  } else if (deployFailed) {
    evalStatus = "stopped";
    evalDetails = "not started: the deployment failed";
  } else if (pipeline.stage === "DEPLOYING") {
    evalStatus = running ? "pending" : "stopped";
    evalDetails = running ? "starts after the deployment" : `not started: ${supersededText}`;
  } else if (pipeline.stage === "EVALUATING" && running) {
    evalStatus = liveStatus;
    evalDetails = `${stuck ? "stalled" : "running the dataset against the Dev harness, then scoring (about five to six minutes)"}${jobText}${elapsedText}`;
  } else if (pipeline.stage === "EVALUATING") {
    evalStatus = "stopped";
    evalDetails = supersededText;
  } else {
    evalStatus = "success";
    const runId = pipeline.runId ?? release.evaluation?.runId;
    evalDetails = `${runId ? `run ${runId}` : "scored"}${pipeline.seconds ? ` · ${formatSeconds(pipeline.seconds)}` : ""}`;
  }

  let verdictStatus: StepsProps.Status = "pending";
  let verdictDetails: string | undefined;
  if (failed) {
    verdictStatus = "error";
    verdictDetails = `run failed${failedStage ? ` while ${String(failedStage).toLowerCase()}` : ""}: no verdict`;
  } else if (superseded) {
    verdictStatus = "stopped";
    verdictDetails = release.evaluation?.source === "api" ? "no verdict from this run: the verdict below was submitted through the API" : "no verdict from this run";
  } else if (!running && pipeline.stage === "DONE" && release.evaluation) {
    const missed = release.evaluation.failures?.length ?? 0;
    verdictStatus = release.evaluation.passed ? "success" : "error";
    verdictDetails = release.evaluation.passed ? "PENDING_APPROVAL: every threshold met" : `BLOCKED: ${missed} threshold${missed === 1 ? "" : "s"} missed`;
  } else if (pipeline.stage === "EVALUATING") {
    verdictDetails = "the thresholds decide BLOCKED or PENDING_APPROVAL";
  }

  const requestJobId = pipeline.deployJobId ?? (pipeline.stage === "DEPLOYING" ? pipeline.jobId : null);
  const steps: StepsProps.Step[] = [
    {
      status: "success",
      header: "Run requested",
      details: `${pipeline.by} · ${formatDateTime(pipeline.startedAt)}${requestJobId ? ` · job ${requestJobId}` : ""}`,
    },
    { status: deployStatus, header: "Dev deployment", details: deployDetails },
    { status: evalStatus, header: "Dataset evaluation", details: evalDetails },
    { status: verdictStatus, header: "Verdict", details: verdictDetails },
  ];

  return (
    <SpaceBetween size="m">
      <Header variant="h3" description="POST /releases/{id}/run by a developer or admin; the worker jobs are polled every 2 s (GET /jobs/{id})">
        Evaluation run
      </Header>
      <Steps steps={steps} ariaLabel="Evaluation run progress" />
      {jobTimedOut && running ? (
        <Alert type="warning" header="Still running after 10 minutes" action={<Button onClick={keepWaiting}>Keep waiting</Button>}>
          The run has not finished yet (a project&apos;s first Dev harness plus the dataset can take about ten minutes). The release page refreshes every 5 seconds while
          EVALUATING.
        </Alert>
      ) : null}
      {jobLoadError && running ? <ErrorAlert header="The run job could not be read" error={jobLoadError} /> : null}
      {pipeline.harnessArn || pipeline.logGroup ? (
        <KeyValuePairs
          columns={3}
          items={[
            { label: "Dev harness", value: pipeline.harnessArn ? <CopyText value={pipeline.harnessArn} max={60} /> : "—" },
            { label: "Log group", value: pipeline.logGroup ? <CopyText value={pipeline.logGroup} max={60} /> : "—" },
            { label: "Evaluate job", value: pipeline.evaluationJobId ? <CopyText value={pipeline.evaluationJobId} max={32} /> : "not queued yet" },
          ]}
        />
      ) : null}
    </SpaceBetween>
  );
}

function ScenarioResults({ release }: { release: Release }) {
  const results = release.evaluation?.results ?? [];
  if (!results.length) {
    return null;
  }
  const evaluators = Array.from(new Set(results.flatMap((row) => Object.keys((row.scores as Record<string, unknown>) ?? {}))));
  return (
    <ExpandableSection headerText={`Per-scenario results (${results.length})`} variant="footer">
      <Table
        variant="embedded"
        items={results}
        trackBy={(row) => String(row.scenarioId ?? Math.random())}
        wrapLines
        columnDefinitions={[
          { id: "scenario", header: "Scenario", cell: (row) => String(row.scenarioId ?? "—"), isRowHeader: true },
          { id: "status", header: "Status", cell: (row) => <GenericStatus status={String(row.status ?? "COMPLETED") === "COMPLETED" ? "SUCCEEDED" : String(row.status)} label={String(row.status ?? "COMPLETED")} /> },
          ...evaluators.map((name) => ({
            id: `score-${name}`,
            header: name,
            cell: (row: Record<string, unknown>) => {
              const value = ((row.scores as Record<string, unknown>) ?? {})[name];
              const label = ((row.labels as Record<string, unknown>) ?? {})[name];
              return value === undefined || value === null ? "—" : `${formatScore(Number(value), 2)}${label ? ` (${String(label)})` : ""}`;
            },
          })),
          { id: "error", header: "Error", cell: (row) => (row.error ? String(row.error) : "—") },
        ]}
      />
    </ExpandableSection>
  );
}

/** The promote job's Catalogue step: the project's AGENT record (a link to the Agents tab), or why there is none. */
function CatalogueRecordCell({ entry }: { entry: PromotionRegistryRecord | undefined }) {
  const navigate = useNavigate();
  if (!entry) {
    return <span>— (promoted before the catalogue step existed)</span>;
  }
  if (entry.recordId) {
    const notes = [entry.action, entry.warning, entry.resourceError ? `resource row not written: ${entry.resourceError}` : null].filter(Boolean).join(" · ");
    return (
      <SpaceBetween size="xxs">
        <Link
          href="/catalogue?tab=agent"
          onFollow={(event) => {
            event.preventDefault();
            navigate("/catalogue?tab=agent");
          }}
        >
          {entry.name}
        </Link>
        <Box variant="small" color={entry.warning ? "text-status-warning" : "text-body-secondary"}>
          {entry.status ?? "—"}
          {notes ? ` · ${notes}` : ""}
        </Box>
      </SpaceBetween>
    );
  }
  if (entry.skipped) {
    return <StatusIndicator type="stopped">Skipped: {entry.skipped}</StatusIndicator>;
  }
  return <StatusIndicator type="warning">Not registered: {entry.error ?? "unknown error"}</StatusIndicator>;
}

function PromotionPanel({
  release,
  job,
  jobTimedOut,
  keepWaiting,
  jobLoadError,
}: {
  release: Release;
  job: Job<Record<string, unknown>> | undefined;
  jobTimedOut: boolean;
  keepWaiting: () => void;
  jobLoadError: Error | null;
}) {
  const [now, setNow] = useState(() => Date.now());
  const running = release.status === "PROMOTING" || (job && (job.status === "QUEUED" || job.status === "RUNNING"));
  useEffect(() => {
    if (!running) {
      return;
    }
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [running]);
  const started = parseDate(release.promotion?.startedAt ?? job?.createdAt);
  const ended = job && (job.status === "SUCCEEDED" || job.status === "FAILED") ? parseDate(job.updatedAt) : null;
  const elapsed = started ? Math.max(0, Math.round(((ended ?? new Date(now)).getTime() - started.getTime()) / 1000)) : null;
  const error = jobError(job);

  const steps: StepsProps.Step[] = useMemo(() => {
    const jobStatus = job?.status ?? (release.status === "PROMOTING" ? "QUEUED" : null);
    const workStatus: StepsProps.Status =
      jobStatus === "SUCCEEDED" ? "success" : jobStatus === "FAILED" ? "error" : jobStatus === "RUNNING" ? "in-progress" : jobStatus === "QUEUED" ? "loading" : "pending";
    const finalStatus: StepsProps.Status = release.status === "PROMOTED" ? "success" : release.status === "FAILED" || jobStatus === "FAILED" ? "error" : "pending";
    return [
      {
        status: "success",
        header: "Promotion requested",
        details: `${release.promotion?.by ?? job?.createdBy ?? "approver"} · ${formatDateTime(release.promotion?.startedAt ?? job?.createdAt)}${job ? ` · job ${job.jobId}` : ""}`,
      },
      {
        status: workStatus,
        header: "Prod key, Prod harness version, live endpoint, online evaluation, catalogue record",
        details: jobStatus === "FAILED" && error ? `${error.code}: ${error.message}` : jobStatus ? `job ${jobStatus}${elapsed !== null ? ` · ${formatSeconds(elapsed)}` : ""}` : undefined,
      },
      {
        status: finalStatus,
        header: release.status === "PROMOTED" ? "Release PROMOTED" : release.status === "FAILED" ? "Release FAILED" : "Release PROMOTED",
        details:
          release.status === "PROMOTED" && release.promotion
            ? `harness version ${release.promotion.harnessVersion ?? "?"} on endpoint ${release.promotion.endpoint ?? "live"} · ${formatDateTime(release.promotion.at)}`
            : undefined,
      },
    ];
  }, [job, release, elapsed, error]);

  const showSteps = release.status === "PROMOTING" || !!job || release.status === "FAILED";
  return (
    <Container
      header={
        <InfoHeader help="POST /releases/{id}/promote by an approver; the worker job is polled every 2 s (GET /jobs/{id})">
          Promotion
        </InfoHeader>
      }
    >
      <SpaceBetween size="m">
        {showSteps ? <Steps steps={steps} ariaLabel="Promotion progress" /> : null}
        {jobTimedOut ? (
          <Alert type="warning" header="Still running after 10 minutes" action={<Button onClick={keepWaiting}>Keep waiting</Button>}>
            The promotion job has not finished yet (a first Prod harness can take a few minutes). The release page refreshes every 5 seconds while PROMOTING.
          </Alert>
        ) : null}
        {jobLoadError && release.status === "PROMOTING" ? <ErrorAlert header="The promotion job could not be read" error={jobLoadError} /> : null}
        {release.status === "PROMOTED" && release.promotion ? (
          <KeyValuePairs
            columns={2}
            items={[
              { label: "Harness version", value: String(release.promotion.harnessVersion ?? "—") },
              { label: "Endpoint", value: <Badge color="blue">{release.promotion.endpoint ?? "live"}</Badge> },
              { label: "Promoted at", value: formatDateTime(release.promotion.at) },
              { label: "By", value: release.promotion.by ?? "—" },
              { label: "Harness ARN", value: <CopyText value={release.promotion.harnessArn} max={60} /> },
              { label: "Online evaluation", value: release.promotion.onlineEvaluationConfigId ? <CopyText value={release.promotion.onlineEvaluationConfigId} max={48} /> : "—" },
              { label: "Job", value: <CopyText value={release.promotion.jobId} max={32} /> },
              { label: "Catalogue record", value: <CatalogueRecordCell entry={release.promotion.registryRecord} /> },
            ]}
          />
        ) : !showSteps ? (
          <EmptyState
            title="Not promoted"
            description={
              release.status === "APPROVED"
                ? "An approver can promote this release now."
                : "Promotion becomes available once an approver has approved the release."
            }
          />
        ) : null}
      </SpaceBetween>
    </Container>
  );
}

function VersionHistory({
  release,
  releases,
  loading,
  error,
  prodResources,
}: {
  release: Release;
  releases: Release[] | undefined;
  loading: boolean;
  error: Error | null;
  prodResources: ReturnType<typeof useResources>["data"];
}) {
  const navigate = useNavigate();
  const rows = sortNewestFirst(releases ?? []);
  const harness = prodResources?.find((res) => res.type === "harness");
  const live = prodResources?.find((res) => res.type === "harness_endpoint" && res.name === "live");
  const onlineEval = prodResources?.find((res) => res.type === "online_evaluation");
  const attr = (res: typeof harness, key: string) => (res?.attributes?.[key] === undefined || res?.attributes?.[key] === null ? "—" : String(res.attributes[key]));
  return (
    <Container
      header={
        <InfoHeader help={`Every release of ${release.projectId} and what Prod runs now (registered harness and live endpoint resources).`}>
          Version history
        </InfoHeader>
      }
    >
      <SpaceBetween size="l">
        <KeyValuePairs
          columns={4}
          items={[
            { label: "Prod harness", value: harness ? <CopyText value={harness.name} max={40} /> : "none yet (first promotion creates it)" },
            { label: "Prod harness version", value: harness ? `${attr(harness, "version")} (blueprint v${attr(harness, "blueprintVersion")})` : "—" },
            { label: "Live endpoint", value: live ? `version ${attr(live, "version")} · ${attr(live, "status")}` : "—" },
            { label: "Online evaluation", value: onlineEval ? <CopyText value={onlineEval.name} max={40} /> : "—" },
          ]}
        />
        {error ? <ErrorAlert header="Release history could not be loaded" error={error} /> : null}
        <Table
          variant="embedded"
          items={rows}
          loading={loading}
          loadingText="Loading releases"
          trackBy="releaseId"
          wrapLines
          columnDefinitions={[
            {
              id: "release",
              header: "Release",
              isRowHeader: true,
              minWidth: 260,
              cell: (row) =>
                row.releaseId === release.releaseId ? (
                  <Box variant="strong">{row.releaseId} (this release)</Box>
                ) : (
                  <Link
                    href={`/releases/${row.releaseId}`}
                    onFollow={(event) => {
                      event.preventDefault();
                      navigate(`/releases/${row.releaseId}`);
                    }}
                  >
                    {row.releaseId}
                  </Link>
                ),
            },
            { id: "version", header: "Blueprint", cell: (row) => `v${row.blueprintVersion}` },
            { id: "notes", header: "Notes", minWidth: 200, cell: (row) => row.notes || "—" },
            { id: "status", header: "Status", minWidth: 150, cell: (row) => <ReleaseStatus status={row.status} /> },
            {
              id: "evaluation",
              header: "Evaluation",
              cell: (row) =>
                row.evaluation
                  ? `${evaluationIsPrevious(row) ? "previous run: " : ""}${row.evaluation.passed ? "PASSED" : `BLOCKED (${row.evaluation.failures?.length ?? 0})`}`
                  : "—",
            },
            { id: "decision", header: "Decision", cell: (row) => (row.approval ? `${row.approval.decision} by ${row.approval.by}` : "—") },
            {
              id: "promotion",
              header: "Promoted harness version",
              cell: (row) => (row.status === "PROMOTED" && row.promotion ? `v${row.promotion.harnessVersion ?? "?"} on ${row.promotion.endpoint ?? "live"} · ${formatDateTime(row.promotion.at)}` : "—"),
            },
            { id: "created", header: "Created", cell: (row) => `${formatDateTime(row.createdAt)} by ${row.createdBy}` },
          ]}
          empty={<EmptyState title="No other releases" />}
        />
      </SpaceBetween>
    </Container>
  );
}
