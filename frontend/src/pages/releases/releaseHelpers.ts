/**
 * Pure helpers of the Releases screens: score rows against thresholds, failure wording and the state machine of
 * build contract section 3 (DRAFT -> EVALUATING -> BLOCKED | PENDING_APPROVAL -> APPROVED | REJECTED -> PROMOTING -> PROMOTED | FAILED).
 * The verdict itself always comes from the backend (platform_sdk.evaluation.apply_thresholds); the UI only presents it.
 */
import type { EvaluationDetail, FailureLike, Job, Release, ReleaseEvaluation, ReleaseStatus } from "../../api/types";
import { TERMINAL_JOB_STATES } from "../../api/types";
import { parseDate, toNumber } from "../../utils/format";

export type { FailureLike } from "../../api/types";

export const SCENARIOS_KEY = "_scenarios";
export const BUILTIN_PREFIX = "Builtin.";

/** `Builtin.Correctness` and `Correctness` both name the evaluator `Correctness` (platform_sdk.evaluation.evaluator_key): the SDK
 * accepts threshold keys in either form and stores `details` / `scores` under the stripped name. */
export function evaluatorKey(name: string): string {
  return name.startsWith(BUILTIN_PREFIX) ? name.slice(BUILTIN_PREFIX.length) : name;
}

export interface ScoreRow {
  /** Threshold / score key ("Correctness", "_scenarios"). */
  key: string;
  /** Display name ("Failed scenarios" for `_scenarios`). */
  evaluator: string;
  /** Mean score as reported by the run (1 = good on every built-in scale). */
  mean: number | null;
  /** The value compared with the threshold: the mean, the harm rate (1 - mean) or the number of failed scenarios. */
  actual: number | null;
  threshold: number | null;
  op: string | null;
  /** null when no threshold applies to this evaluator (informational score). */
  passed: boolean | null;
  count: number | null;
  errors: number | null;
  passRate: number | null;
  gated: boolean;
}

/** Harmfulness thresholds cap the harm rate (1 - mean); every other threshold is a floor on the mean. */
export function isUpperBound(evaluator: string): boolean {
  return evaluatorKey(evaluator).toLowerCase().includes("harmfulness");
}

function detailsByEvaluator(details: ReleaseEvaluation["details"] | undefined | null): Record<string, EvaluationDetail> {
  if (!details) {
    return {};
  }
  if (Array.isArray(details)) {
    return Object.fromEntries(details.filter((d) => d && typeof d.evaluator === "string").map((d) => [d.evaluator as string, d]));
  }
  return details;
}

/** Rows for the "scores vs thresholds" table: gated evaluators first (in threshold order), extra scores after, failed scenarios last. */
export function scoreRows(evaluation: ReleaseEvaluation | null | undefined): ScoreRow[] {
  if (!evaluation) {
    return [];
  }
  const details = detailsByEvaluator(evaluation.details);
  const scores = evaluation.scores ?? {};
  const thresholds = evaluation.thresholds ?? {};
  const rows: ScoreRow[] = [];
  const seen = new Set<string>();
  for (const [name, threshold] of Object.entries(thresholds)) {
    const key = evaluatorKey(name);
    const detail = details[key] ?? details[name] ?? {};
    const score = scores[key] ?? scores[name] ?? {};
    const mean = toNumber(score.mean);
    const upper = isUpperBound(name);
    const actual = toNumber(detail.actual) ?? (mean === null ? null : upper ? Math.round((1 - mean) * 10_000) / 10_000 : mean);
    const limit = toNumber(detail.threshold) ?? toNumber(threshold);
    const passed =
      typeof detail.passed === "boolean" ? detail.passed : actual !== null && limit !== null && (upper ? actual <= limit : actual >= limit);
    rows.push({
      key,
      evaluator: key,
      mean,
      actual,
      threshold: limit,
      op: detail.op ?? (upper ? "harmRate<=" : "mean>="),
      passed,
      count: toNumber(score.count) ?? toNumber(detail.count),
      errors: toNumber(score.errors) ?? toNumber(detail.errors),
      passRate: toNumber(score.passRate),
      gated: true,
    });
    seen.add(key);
  }
  for (const [name, score] of Object.entries(scores)) {
    const key = evaluatorKey(name);
    if (seen.has(key)) {
      continue;
    }
    rows.push({
      key,
      evaluator: key,
      mean: toNumber(score.mean),
      actual: toNumber(score.mean),
      threshold: null,
      op: null,
      passed: null,
      count: toNumber(score.count),
      errors: toNumber(score.errors),
      passRate: toNumber(score.passRate),
      gated: false,
    });
    seen.add(key);
  }
  const scenarios = details[SCENARIOS_KEY];
  if (scenarios) {
    rows.push({
      key: SCENARIOS_KEY,
      evaluator: "Failed scenarios",
      mean: null,
      actual: toNumber(scenarios.actual),
      threshold: toNumber(scenarios.threshold) ?? 0,
      op: scenarios.op ?? "failed<=",
      passed: scenarios.passed ?? false,
      count: null,
      errors: null,
      passRate: null,
      gated: true,
    });
  }
  return rows;
}

export function formatScore(value: number | null | undefined, digits = 3): string {
  const n = toNumber(value);
  return n === null ? "—" : n.toFixed(digits);
}

/** "mean ≥ 0.8", "harm rate ≤ 0.0", "failed scenarios ≤ 0". */
export function describeThreshold(row: Pick<ScoreRow, "op" | "threshold" | "key">): string {
  if (row.threshold === null || row.op === null) {
    return "no threshold";
  }
  if (row.key === SCENARIOS_KEY || row.op.startsWith("failed")) {
    return `failed scenarios ≤ ${row.threshold}`;
  }
  if (row.op.startsWith("harmRate")) {
    return `harm rate ≤ ${row.threshold}`;
  }
  return `mean ≥ ${row.threshold}`;
}

/** "Correctness mean ≥ 0.8, Harmfulness harm rate ≤ 0" for a blueprint version's thresholds (before any evaluation exists). */
export function describeBlueprintThreshold(name: string, value: number): string {
  return describeThreshold({ key: evaluatorKey(name), op: isUpperBound(name) ? "harmRate<=" : "mean>=", threshold: toNumber(value) });
}

export function thresholdsText(thresholds: Record<string, number> | undefined | null, empty = "no thresholds"): string {
  const entries = Object.entries(thresholds ?? {});
  return entries.length ? entries.map(([name, value]) => `${evaluatorKey(name)} ${describeBlueprintThreshold(name, value)}`).join(", ") : empty;
}

/** One sentence per failure, as stored by apply_thresholds: {evaluator, actual, threshold, op}. */
export function describeFailure(failure: FailureLike | string): string {
  if (typeof failure === "string") {
    return failure;
  }
  const { evaluator = "?", actual, threshold } = failure;
  const op = failure.op ?? "";
  if (evaluator === SCENARIOS_KEY || op.startsWith("failed")) {
    const n = toNumber(actual) ?? 0;
    return `${n} scenario${n === 1 ? "" : "s"} failed to run or score (none allowed)`;
  }
  if (actual === null || actual === undefined) {
    return `${evaluator}: no score (the evaluator never produced a value)`;
  }
  if (op.startsWith("harmRate")) {
    return `${evaluator}: harm rate ${formatScore(actual)} is above the ${threshold} limit`;
  }
  return `${evaluator}: mean ${formatScore(actual)} is below the ${threshold} threshold`;
}

export const EVALUATION_STATUSES: ReadonlyArray<ReleaseStatus> = ["DRAFT", "EVALUATING", "BLOCKED", "PENDING_APPROVAL"];
export const RUN_STATUSES: ReadonlyArray<ReleaseStatus> = ["DRAFT", "BLOCKED"];
export const PROMOTE_STATUSES: ReadonlyArray<ReleaseStatus> = ["APPROVED", "FAILED"];

export function canDecide(status: ReleaseStatus | string | null | undefined): boolean {
  return status === "PENDING_APPROVAL";
}

/** DRAFT (never run, or the last run failed) or BLOCKED (run again); a stalled EVALUATING release is retried through `pipelineStuck`. */
export function canRun(status: ReleaseStatus | string | null | undefined): boolean {
  return (RUN_STATUSES as ReadonlyArray<string>).includes(status ?? "");
}

/** APPROVED, or FAILED (retry of a failed promotion); a stuck PROMOTING release is retried through `promotionStuck`. */
export function canPromote(status: ReleaseStatus | string | null | undefined): boolean {
  return (PROMOTE_STATUSES as ReadonlyArray<string>).includes(status ?? "");
}

/** The backend's worker timeout: a PROMOTING release whose job has not moved for this long may be promoted again. */
export const STUCK_PROMOTION_MS = 15 * 60_000;

/**
 * Mirror of handlers/releases.py `promotion_stuck`: a PROMOTING release can be promoted again when its job is gone (404 after
 * the 24 h TTL), already SUCCEEDED / FAILED without moving the release, or not updated for 15 minutes (worker killed by a
 * timeout or OOM). `jobMissing` is the 404 from GET /jobs/{id}; while the job is unknown the promotion's own start time counts.
 * The API stays the arbiter: an early retry answers 409.
 */
export function promotionStuck(
  release: Pick<Release, "status" | "promotion">,
  job: Pick<Job<unknown>, "status" | "createdAt" | "updatedAt"> | null | undefined,
  jobMissing = false,
  now: number = Date.now(),
): boolean {
  if (release.status !== "PROMOTING") {
    return false;
  }
  if (jobMissing || (job && TERMINAL_JOB_STATES.includes(job.status))) {
    return true;
  }
  const reference = parseDate(job ? (job.updatedAt ?? job.createdAt) : release.promotion?.startedAt);
  return !!reference && now - reference.getTime() > STUCK_PROMOTION_MS;
}

/** The backend's worker timeout for a release run (deploy or evaluate job): same 15 minutes as a promotion. */
export const STUCK_PIPELINE_MS = STUCK_PROMOTION_MS;

/**
 * A run job that SUCCEEDED moments ago has usually handed over (the "release" job queues the "evaluate" job and the worker
 * rewrites `pipeline.jobId` before the job is marked SUCCEEDED): the page simply has not refetched the release yet. A
 * SUCCEEDED job counts as stuck only once this grace since its `updatedAt` has passed.
 */
export const SUCCEEDED_HANDOVER_GRACE_MS = 30_000;

/**
 * Mirror of `promotionStuck` for the evaluation run: an EVALUATING release can be run again when the job named by
 * `pipeline.jobId` is gone (404), FAILED without moving the release, SUCCEEDED more than `SUCCEEDED_HANDOVER_GRACE_MS` ago
 * without the release following, or not updated for 15 minutes. While the job is unknown the run's own start time counts.
 * The API stays the arbiter: an early retry answers 409.
 */
export function pipelineStuck(
  release: Pick<Release, "status" | "pipeline">,
  job: Pick<Job<unknown>, "status" | "createdAt" | "updatedAt"> | null | undefined,
  jobMissing = false,
  now: number = Date.now(),
): boolean {
  if (release.status !== "EVALUATING") {
    return false;
  }
  if (jobMissing || job?.status === "FAILED") {
    return true;
  }
  if (job?.status === "SUCCEEDED") {
    const finished = parseDate(job.updatedAt ?? job.createdAt);
    return !finished || now - finished.getTime() > SUCCEEDED_HANDOVER_GRACE_MS;
  }
  const reference = parseDate(job ? (job.updatedAt ?? job.createdAt) : release.pipeline?.startedAt);
  return !!reference && now - reference.getTime() > STUCK_PIPELINE_MS;
}

/**
 * The last run failed and the release can be run again: DRAFT with the error (the deploy or the dataset run failed), or
 * BLOCKED with `pipeline.stage` FAILED and the error (the worker could not be started for "Run again": start_run returned
 * the release to its previous status).
 */
export function runFailed(release: Pick<Release, "status" | "pipeline" | "error">): boolean {
  if (!release.error) {
    return false;
  }
  return release.status === "DRAFT" || (release.status === "BLOCKED" && release.pipeline?.stage === "FAILED");
}

/**
 * In DRAFT or EVALUATING any `evaluation` on the release is the previous run's verdict: the backend keeps it until the new
 * verdict lands (BLOCKED or PENDING_APPROVAL), so the screens must not present it as the current one.
 */
export function evaluationIsPrevious(release: Pick<Release, "status" | "evaluation">): boolean {
  return !!release.evaluation && (release.status === "DRAFT" || release.status === "EVALUATING");
}

export function acceptsEvaluation(status: ReleaseStatus | string | null | undefined): boolean {
  return (EVALUATION_STATUSES as ReadonlyArray<string>).includes(status ?? "");
}

/** "deploying…" or "evaluating…" for an EVALUATING release (the list columns); null for every other status. */
export function runProgressLabel(release: Pick<Release, "status" | "pipeline">): string | null {
  if (release.status !== "EVALUATING") {
    return null;
  }
  return release.pipeline?.stage === "DEPLOYING" ? "deploying…" : "evaluating…";
}

/**
 * What happens next, per status (shown under the release header); `stuck` = `promotionStuck(...)` for a PROMOTING release
 * or `pipelineStuck(...)` for an EVALUATING one.
 */
export function nextStep(release: Pick<Release, "status" | "promotion" | "pipeline" | "blueprintVersion" | "error" | "evaluation">, stuck = false): string {
  const version = `v${release.blueprintVersion}`;
  if (release.status === "PROMOTING" && stuck) {
    return `Promotion job ${release.promotion?.jobId ?? ""} did not finish (failed, gone or silent for 15 minutes) while the release stayed PROMOTING: an approver can promote again, which starts a new job.`;
  }
  if (release.status === "EVALUATING" && stuck) {
    return `Run job ${release.pipeline?.jobId ?? ""} did not finish (failed, gone or silent for 15 minutes) while the release stayed EVALUATING: the run stalled; run it again.`;
  }
  switch (release.status) {
    case "DRAFT":
      return release.error
        ? `The last run failed (${release.error.code}). Fix the cause and run it again.${release.evaluation ? " The verdict shown is the previous run's." : ""}`
        : `Not evaluated yet. Run the evaluation: the platform deploys blueprint ${version} to Dev if needed, runs the dataset and applies the thresholds (BLOCKED or PENDING_APPROVAL).`;
    case "EVALUATING":
      if (release.pipeline?.stage === "DEPLOYING") {
        return `Deploying blueprint ${version} to Dev (about a minute; the first Dev harness of a project takes four to five) before the dataset runs.`;
      }
      if (release.pipeline?.stage === "EVALUATING") {
        return "Running the evaluation dataset against the Dev harness: about five to six minutes, then the thresholds decide BLOCKED or PENDING_APPROVAL.";
      }
      return "The evaluation run is in progress.";
    case "BLOCKED":
      if (runFailed(release)) {
        return `The last run could not start (${release.error?.code}): the release keeps the previous verdict. Fix the cause and run it again.`;
      }
      return "The scores miss at least one threshold: fix the blueprint (new version) and create a new release, or run this one again.";
    case "PENDING_APPROVAL":
      return "Every threshold is met. An approver reviews the evidence and approves or rejects (admins and developers get 403: separation of duties).";
    case "APPROVED":
      return "Approved. An approver can promote it: the platform creates the Prod key, updates the Prod harness and enables online evaluation.";
    case "REJECTED":
      return "Rejected. Create a new release for another attempt.";
    case "PROMOTING":
      return `Promotion job ${release.promotion?.jobId ?? ""} is running (about 20 s; up to a few minutes for a first Prod harness).`;
    case "PROMOTED":
      return "Live on the Prod endpoint. Online evaluation scores Prod sessions a few minutes after they go quiet.";
    case "FAILED":
      return "The promotion failed; fix the cause named in the error and promote again.";
    default:
      return "";
  }
}

export const STATUS_ORDER: ReadonlyArray<ReleaseStatus> = [
  "DRAFT",
  "EVALUATING",
  "BLOCKED",
  "PENDING_APPROVAL",
  "APPROVED",
  "REJECTED",
  "PROMOTING",
  "PROMOTED",
  "FAILED",
];

export function sortNewestFirst<T extends { createdAt: string }>(rows: ReadonlyArray<T>): T[] {
  return [...rows].sort((a, b) => (a.createdAt < b.createdAt ? 1 : a.createdAt > b.createdAt ? -1 : 0));
}
