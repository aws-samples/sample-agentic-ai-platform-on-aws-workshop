import { describe, expect, it } from "vitest";

import type { Release, ReleaseEvaluation, ReleasePipeline } from "../../api/types";
import {
  SUCCEEDED_HANDOVER_GRACE_MS,
  acceptsEvaluation,
  canDecide,
  canPromote,
  canRun,
  describeFailure,
  describeThreshold,
  evaluationIsPrevious,
  evaluatorKey,
  isUpperBound,
  nextStep,
  pipelineStuck,
  promotionStuck,
  runFailed,
  runProgressLabel,
  scoreRows,
  sortNewestFirst,
  thresholdsText,
} from "./releaseHelpers";

const blocked: ReleaseEvaluation = {
  runId: "run-synthetic-v1",
  datasetName: "hr-assistant.json",
  scenarioCount: 10,
  scores: {
    Correctness: { mean: 0.7, min: 0, max: 1, passRate: 0.7, count: 10, errors: 0, failed: 0 },
    GoalSuccessRate: { mean: 0.7, min: 0, max: 1, passRate: 0.7, count: 10, errors: 0, failed: 0 },
    TrajectoryInOrderMatch: { mean: 0.625, min: 0, max: 1, passRate: 0.625, count: 8, errors: 2, failed: 0 },
    Harmfulness: { mean: 1, min: 1, max: 1, passRate: 1, count: 10, errors: 0, failed: 0 },
    Helpfulness: { mean: 0.9, count: 10 },
  },
  thresholds: { Correctness: 0.8, GoalSuccessRate: 0.9, TrajectoryInOrderMatch: 0.9, Harmfulness: 0 },
  verdict: "BLOCKED",
  passed: false,
  failures: [
    { evaluator: "Correctness", actual: 0.7, threshold: 0.8, op: "mean>=" },
    { evaluator: "GoalSuccessRate", actual: 0.7, threshold: 0.9, op: "mean>=" },
    { evaluator: "TrajectoryInOrderMatch", actual: 0.625, threshold: 0.9, op: "mean>=" },
  ],
  details: {
    Correctness: { actual: 0.7, threshold: 0.8, op: "mean>=", passed: false, count: 10, errors: 0 },
    GoalSuccessRate: { actual: 0.7, threshold: 0.9, op: "mean>=", passed: false, count: 10, errors: 0 },
    TrajectoryInOrderMatch: { actual: 0.625, threshold: 0.9, op: "mean>=", passed: false, count: 8, errors: 2 },
    Harmfulness: { actual: 0, threshold: 0, op: "harmRate<=", passed: true, count: 10, errors: 0 },
  },
  results: [],
  failedScenarios: [],
  submittedBy: "developer",
  completedAt: "2026-10-09T03:00:00Z",
};

describe("scoreRows", () => {
  it("lists gated evaluators in threshold order with the compared value, then informational scores", () => {
    const rows = scoreRows(blocked);
    expect(rows.map((r) => r.key)).toEqual(["Correctness", "GoalSuccessRate", "TrajectoryInOrderMatch", "Harmfulness", "Helpfulness"]);
    expect(rows[0]).toMatchObject({ mean: 0.7, actual: 0.7, threshold: 0.8, op: "mean>=", passed: false, count: 10, errors: 0, gated: true });
    const harm = rows[3];
    expect(harm).toMatchObject({ mean: 1, actual: 0, threshold: 0, passed: true, op: "harmRate<=" });
    expect(rows[4]).toMatchObject({ key: "Helpfulness", gated: false, passed: null, threshold: null, mean: 0.9 });
  });

  it("derives the harm rate and the verdict itself when details are missing (older releases)", () => {
    const rows = scoreRows({ ...blocked, details: [] });
    expect(rows.find((r) => r.key === "Harmfulness")).toMatchObject({ actual: 0, passed: true });
    expect(rows.find((r) => r.key === "Correctness")).toMatchObject({ actual: 0.7, passed: false });
  });

  it("accepts details as an array and appends failed scenarios last", () => {
    const rows = scoreRows({
      ...blocked,
      details: [
        { evaluator: "Correctness", actual: 1, threshold: 0.8, op: "mean>=", passed: true },
        { evaluator: "_scenarios", actual: 2, threshold: 0, op: "failed<=", passed: false },
      ],
    });
    expect(rows[rows.length - 1]).toMatchObject({ key: "_scenarios", evaluator: "Failed scenarios", actual: 2, passed: false, gated: true });
    expect(rows[0]).toMatchObject({ key: "Correctness", passed: true });
  });

  it("returns nothing for an unevaluated release", () => {
    expect(scoreRows(null)).toEqual([]);
  });

  it("matches Builtin.-prefixed threshold keys with the stripped names the backend stores", () => {
    const rows = scoreRows({
      ...blocked,
      thresholds: { "Builtin.Correctness": 0.8, "Builtin.Harmfulness": 0 },
      details: { Correctness: { actual: 0.7, threshold: 0.8, op: "mean>=", passed: false }, Harmfulness: { actual: 0, threshold: 0, op: "harmRate<=", passed: true } },
    });
    expect(rows.map((r) => r.key)).toEqual(["Correctness", "Harmfulness", "GoalSuccessRate", "TrajectoryInOrderMatch", "Helpfulness"]);
    expect(rows[0]).toMatchObject({ evaluator: "Correctness", mean: 0.7, actual: 0.7, passed: false, gated: true });
    expect(rows[1]).toMatchObject({ evaluator: "Harmfulness", mean: 1, actual: 0, passed: true, op: "harmRate<=" });
    expect(rows.filter((r) => r.key === "Correctness")).toHaveLength(1); // no duplicate "not gated" row
    expect(evaluatorKey("Builtin.Correctness")).toBe("Correctness");
    expect(evaluatorKey("Correctness")).toBe("Correctness");
    expect(isUpperBound("Builtin.Harmfulness")).toBe(true);
    expect(thresholdsText({ "Builtin.Harmfulness": 0, Correctness: 0.8 })).toBe("Harmfulness harm rate ≤ 0, Correctness mean ≥ 0.8");
    expect(thresholdsText({})).toBe("no thresholds");
  });
});

describe("wording", () => {
  it("describes thresholds and failures in words", () => {
    expect(describeThreshold({ key: "Correctness", op: "mean>=", threshold: 0.8 })).toBe("mean ≥ 0.8");
    expect(describeThreshold({ key: "Harmfulness", op: "harmRate<=", threshold: 0 })).toBe("harm rate ≤ 0");
    expect(describeThreshold({ key: "_scenarios", op: "failed<=", threshold: 0 })).toBe("failed scenarios ≤ 0");
    expect(describeThreshold({ key: "Helpfulness", op: null, threshold: null })).toBe("no threshold");
    expect(describeFailure({ evaluator: "Correctness", actual: 0.7, threshold: 0.8, op: "mean>=" })).toBe("Correctness: mean 0.700 is below the 0.8 threshold");
    expect(describeFailure({ evaluator: "Harmfulness", actual: 0.1, threshold: 0, op: "harmRate<=" })).toBe("Harmfulness: harm rate 0.100 is above the 0 limit");
    expect(describeFailure({ evaluator: "_scenarios", actual: 1, threshold: 0, op: "failed<=" })).toBe("1 scenario failed to run or score (none allowed)");
    expect(describeFailure({ evaluator: "Correctness", actual: null, threshold: 0.8, op: "mean>=" })).toContain("no score");
    expect(describeFailure("legacy text")).toBe("legacy text");
  });
});

type StatusView = Pick<Release, "status" | "promotion" | "pipeline" | "blueprintVersion" | "error" | "evaluation">;

/** A release as the status alert sees it: `at(status, {pipeline, error, evaluation})` fills the fields `nextStep` reads. */
function at(status: Release["status"], extra: Partial<Omit<StatusView, "status" | "blueprintVersion">> = {}): StatusView {
  return { status, blueprintVersion: 3, promotion: null, pipeline: null, error: null, evaluation: null, ...extra };
}

const pipeline = (stage: ReleasePipeline["stage"], jobId = "J-run"): ReleasePipeline => ({
  stage,
  jobId,
  deployJobId: stage === "DEPLOYING" ? jobId : "J-deploy",
  evaluationJobId: stage === "DEPLOYING" ? null : "J-eval",
  deploySkipped: false,
  startedAt: "2026-10-09T11:58:00Z",
  by: "developer",
});

describe("state machine", () => {
  it("mirrors the backend transitions", () => {
    expect(canDecide("PENDING_APPROVAL")).toBe(true);
    expect(canDecide("APPROVED")).toBe(false);
    expect(canPromote("APPROVED")).toBe(true);
    expect(canPromote("FAILED")).toBe(true);
    expect(canPromote("PROMOTING")).toBe(false);
    expect(canPromote("PENDING_APPROVAL")).toBe(false);
    expect(acceptsEvaluation("DRAFT")).toBe(true);
    expect(acceptsEvaluation("BLOCKED")).toBe(true);
    expect(acceptsEvaluation("PROMOTED")).toBe(false);
    // the run: DRAFT (never run, or the last run failed) and BLOCKED (run again); EVALUATING only through pipelineStuck
    expect(canRun("DRAFT")).toBe(true);
    expect(canRun("BLOCKED")).toBe(true);
    expect(canRun("EVALUATING")).toBe(false);
    expect(canRun("PENDING_APPROVAL")).toBe(false);
    expect(canRun("PROMOTED")).toBe(false);
    expect(canRun(undefined)).toBe(false);
  });

  it("explains the next step per status", () => {
    expect(nextStep(at("PENDING_APPROVAL"))).toContain("approver");
    expect(nextStep(at("PROMOTING", { promotion: { jobId: "J1" } }))).toContain("J1");
    expect(nextStep(at("PROMOTING", { promotion: { jobId: "J1" } }), true)).toContain("promote again");
    expect(nextStep(at("FAILED"))).toContain("promote again");
    expect(nextStep(at("BLOCKED"))).toBe("The scores miss at least one threshold: fix the blueprint (new version) and create a new release, or run this one again.");
  });

  it("tells the developer to run a DRAFT release, or why the last run failed", () => {
    expect(nextStep(at("DRAFT"))).toBe(
      "Not evaluated yet. Run the evaluation: the platform deploys blueprint v3 to Dev if needed, runs the dataset and applies the thresholds (BLOCKED or PENDING_APPROVAL).",
    );
    expect(nextStep(at("DRAFT", { error: { code: "WORKER_UNAVAILABLE", message: "no worker", stage: "DEPLOYING" } }))).toBe(
      "The last run failed (WORKER_UNAVAILABLE). Fix the cause and run it again.",
    );
    // the previous verdict stays on the release until the next one lands: say so
    expect(nextStep(at("DRAFT", { error: { code: "HARNESS_FAILED", message: "boom", stage: "DEPLOYING" }, evaluation: blocked }))).toBe(
      "The last run failed (HARNESS_FAILED). Fix the cause and run it again. The verdict shown is the previous run's.",
    );
  });

  it("tells a BLOCKED release apart from one whose Run again could not start (WORKER_UNAVAILABLE keeps the previous status)", () => {
    const error = { code: "WORKER_UNAVAILABLE", message: "no worker", stage: "DEPLOYING", jobId: "J-run2" };
    const couldNotStart = at("BLOCKED", { pipeline: { ...pipeline("FAILED", "J-run2"), error }, error, evaluation: blocked });
    expect(runFailed(couldNotStart)).toBe(true);
    expect(nextStep(couldNotStart)).toBe("The last run could not start (WORKER_UNAVAILABLE): the release keeps the previous verdict. Fix the cause and run it again.");
    // a plain BLOCKED verdict, even with a leftover error object, is not a failed run unless the pipeline says FAILED
    expect(runFailed(at("BLOCKED", { pipeline: pipeline("DONE"), evaluation: blocked }))).toBe(false);
    expect(runFailed(at("BLOCKED", { pipeline: pipeline("DONE"), error, evaluation: blocked }))).toBe(false);
    expect(nextStep(at("BLOCKED", { pipeline: pipeline("DONE"), evaluation: blocked }))).toContain("miss at least one threshold");
    // DRAFT with an error is a failed run whatever the pipeline says; the other statuses never are
    expect(runFailed(at("DRAFT", { error }))).toBe(true);
    expect(runFailed(at("DRAFT"))).toBe(false);
    expect(runFailed(at("FAILED", { error: { code: "PROMOTE_FAILED", message: "x" } }))).toBe(false);
    expect(runFailed(at("EVALUATING", { pipeline: pipeline("EVALUATING"), error }))).toBe(false);
  });

  it("flags the evaluation as the previous run's while the release is DRAFT or EVALUATING", () => {
    expect(evaluationIsPrevious(at("DRAFT", { evaluation: blocked }))).toBe(true);
    expect(evaluationIsPrevious(at("EVALUATING", { pipeline: pipeline("DEPLOYING"), evaluation: blocked }))).toBe(true);
    expect(evaluationIsPrevious(at("DRAFT"))).toBe(false);
    expect(evaluationIsPrevious(at("EVALUATING", { pipeline: pipeline("DEPLOYING") }))).toBe(false);
    for (const status of ["BLOCKED", "PENDING_APPROVAL", "APPROVED", "REJECTED", "PROMOTING", "PROMOTED", "FAILED"] as const) {
      expect(evaluationIsPrevious(at(status, { evaluation: blocked }))).toBe(false);
    }
  });

  it("names the stage of a running evaluation and a stalled run", () => {
    expect(nextStep(at("EVALUATING", { pipeline: pipeline("DEPLOYING") }))).toBe(
      "Deploying blueprint v3 to Dev (about a minute; the first Dev harness of a project takes four to five) before the dataset runs.",
    );
    expect(nextStep(at("EVALUATING", { pipeline: pipeline("EVALUATING") }))).toBe(
      "Running the evaluation dataset against the Dev harness: about five to six minutes, then the thresholds decide BLOCKED or PENDING_APPROVAL.",
    );
    // a run started by an older build or the CLI carries no pipeline
    expect(nextStep(at("EVALUATING"))).toBe("The evaluation run is in progress.");
    const stalled = nextStep(at("EVALUATING", { pipeline: pipeline("EVALUATING", "J-eval") }), true);
    expect(stalled).toContain("J-eval");
    expect(stalled).toContain("run it again");
    // no course pointers on the release screens
    for (const status of ["DRAFT", "EVALUATING", "BLOCKED", "PENDING_APPROVAL", "APPROVED", "REJECTED", "PROMOTING", "PROMOTED", "FAILED"] as const) {
      expect(nextStep(at(status, { pipeline: pipeline("DEPLOYING") }))).not.toMatch(/module|lab|notebook|workshop apply/i);
    }
  });

  it("labels the list columns while a run is in progress", () => {
    expect(runProgressLabel(at("EVALUATING", { pipeline: pipeline("DEPLOYING") }))).toBe("deploying…");
    expect(runProgressLabel(at("EVALUATING", { pipeline: pipeline("EVALUATING") }))).toBe("evaluating…");
    expect(runProgressLabel(at("EVALUATING"))).toBe("evaluating…");
    expect(runProgressLabel(at("BLOCKED", { pipeline: pipeline("DONE") }))).toBeNull();
  });

  it("offers Run again for a stalled EVALUATING release exactly when the API would accept it", () => {
    const now = Date.parse("2026-10-09T12:00:00Z");
    const evaluating = { status: "EVALUATING" as const, pipeline: pipeline("EVALUATING", "J-eval") };
    const job = (status: "QUEUED" | "RUNNING" | "SUCCEEDED" | "FAILED", updatedAt: string) => ({ status, createdAt: "2026-10-09T11:58:00Z", updatedAt });
    // the worker is on it: job RUNNING and recent
    expect(pipelineStuck(evaluating, job("RUNNING", "2026-10-09T11:58:05Z"), false, now)).toBe(false);
    expect(pipelineStuck(evaluating, job("QUEUED", "2026-10-09T11:58:00Z"), false, now)).toBe(false);
    // the job FAILED without moving the release, or is gone (404 after the TTL): stuck at once
    expect(pipelineStuck(evaluating, job("FAILED", "2026-10-09T11:59:00Z"), false, now)).toBe(true);
    expect(pipelineStuck(evaluating, job("FAILED", "2026-10-09T11:59:59Z"), false, now)).toBe(true);
    expect(pipelineStuck(evaluating, undefined, true, now)).toBe(true);
    // a job that SUCCEEDED moments ago has usually handed over (release job -> evaluate job) and the release is not refetched yet
    expect(SUCCEEDED_HANDOVER_GRACE_MS).toBe(30_000);
    expect(pipelineStuck(evaluating, job("SUCCEEDED", "2026-10-09T11:59:59Z"), false, now)).toBe(false);
    expect(pipelineStuck(evaluating, job("SUCCEEDED", "2026-10-09T11:59:30Z"), false, now)).toBe(false); // exactly the grace
    expect(pipelineStuck(evaluating, job("SUCCEEDED", "2026-10-09T11:59:29Z"), false, now)).toBe(true);
    expect(pipelineStuck(evaluating, job("SUCCEEDED", "2026-10-09T11:59:00Z"), false, now)).toBe(true);
    // the worker died hard: the job never left RUNNING for longer than the 15-minute worker timeout
    expect(pipelineStuck(evaluating, job("RUNNING", "2026-10-09T11:44:59Z"), false, now)).toBe(true);
    // no job loaded yet: the run's own start time decides
    expect(pipelineStuck(evaluating, undefined, false, now)).toBe(false);
    expect(pipelineStuck({ ...evaluating, pipeline: { ...evaluating.pipeline, startedAt: "2026-10-09T11:40:00Z" } }, undefined, false, now)).toBe(true);
    // no pipeline at all (older release): nothing to judge by
    expect(pipelineStuck({ status: "EVALUATING", pipeline: null }, undefined, false, now)).toBe(false);
    // other statuses never count as stuck
    expect(pipelineStuck({ status: "BLOCKED", pipeline: pipeline("DONE") }, job("FAILED", "2026-10-09T11:00:00Z"), true, now)).toBe(false);
    expect(pipelineStuck({ status: "PROMOTING", pipeline: pipeline("DONE") }, job("FAILED", "2026-10-09T11:00:00Z"), true, now)).toBe(false);
  });

  it("offers Promote again for a stuck PROMOTING release exactly when the API would accept it", () => {
    const now = Date.parse("2026-10-09T12:00:00Z");
    const promoting = { status: "PROMOTING" as const, promotion: { jobId: "J1", startedAt: "2026-10-09T11:58:00Z" } };
    const job = (status: "QUEUED" | "RUNNING" | "SUCCEEDED" | "FAILED", updatedAt: string) => ({ status, createdAt: "2026-10-09T11:58:00Z", updatedAt });
    // the worker is on it: job RUNNING and recent
    expect(promotionStuck(promoting, job("RUNNING", "2026-10-09T11:58:05Z"), false, now)).toBe(false);
    expect(promotionStuck(promoting, job("QUEUED", "2026-10-09T11:58:00Z"), false, now)).toBe(false);
    // the job finished without moving the release, or is gone (404 after the TTL)
    expect(promotionStuck(promoting, job("FAILED", "2026-10-09T11:59:00Z"), false, now)).toBe(true);
    expect(promotionStuck(promoting, job("SUCCEEDED", "2026-10-09T11:59:00Z"), false, now)).toBe(true);
    expect(promotionStuck(promoting, undefined, true, now)).toBe(true);
    // the worker died hard: the job never left RUNNING for longer than the 15-minute worker timeout
    expect(promotionStuck(promoting, job("RUNNING", "2026-10-09T11:44:59Z"), false, now)).toBe(true);
    // no job loaded yet: the promotion's own start time decides
    expect(promotionStuck(promoting, undefined, false, now)).toBe(false);
    expect(promotionStuck({ ...promoting, promotion: { jobId: "J1", startedAt: "2026-10-09T11:40:00Z" } }, undefined, false, now)).toBe(true);
    // other statuses never count as stuck
    expect(promotionStuck({ status: "APPROVED", promotion: null }, job("FAILED", "2026-10-09T11:00:00Z"), true, now)).toBe(false);
  });

  it("sorts newest first", () => {
    const rows = sortNewestFirst([{ createdAt: "2026-10-09T01:00:00Z", id: 1 }, { createdAt: "2026-10-09T03:00:00Z", id: 2 }]);
    expect(rows.map((r) => r.id)).toEqual([2, 1]);
  });
});
