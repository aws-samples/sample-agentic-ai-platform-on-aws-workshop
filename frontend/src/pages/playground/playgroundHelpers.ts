/**
 * Pure helpers of the Playground: session ids that satisfy the InvokeHarness rule, the harness per environment from the
 * registered resources, usage and budget-error parsing. The chat itself lives in PlaygroundPage.tsx.
 */
import type { Env, InvokeOutput, InvokeUsage, JobError, Resource } from "../../api/types";
import { toNumber } from "../../utils/format";

/** InvokeHarness runtimeSessionId: 33-100 characters of [a-zA-Z0-9_-] starting with a letter or digit (backend handlers/invoke.py). */
export const SESSION_ID_RE = /^[a-zA-Z0-9][a-zA-Z0-9_-]{32,99}$/;
export const SESSION_SUFFIX = "playground";

function randomUuid(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  const hex = Array.from({ length: 32 }, () => Math.floor(Math.random() * 16).toString(16)).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

/** `<uuid4>-playground` (47 characters): the same shape as the backend's `<uuid4>-platform` default. */
export function newSessionId(suffix: string = SESSION_SUFFIX): string {
  return `${randomUuid()}-${suffix}`;
}

export function isValidSessionId(value: string): boolean {
  return SESSION_ID_RE.test(value);
}

export interface HarnessInfo {
  name: string;
  arn: string | null;
  version: string | null;
  blueprintVersion: string | null;
  model: string | null;
  endpoint: string | null;
  /** The endpoint the playground reaches: DEFAULT (latest version) for Dev, `live` for Prod. */
  qualifier: string;
}

/** The registered `harness` resource per environment (null until module 6 deploys / promotes). */
export function harnessByEnv(resources: ReadonlyArray<Resource> | undefined | null): Record<Env, HarnessInfo | null> {
  const result: Record<Env, HarnessInfo | null> = { dev: null, prod: null };
  for (const env of ["dev", "prod"] as Env[]) {
    const row = (resources ?? []).find((r) => r.env === env && r.type === "harness" && !!r.arn);
    if (!row) {
      continue;
    }
    const attributes = row.attributes ?? {};
    const str = (key: string) => (attributes[key] === undefined || attributes[key] === null ? null : String(attributes[key]));
    result[env] = {
      name: row.name,
      arn: row.arn,
      version: str("version"),
      blueprintVersion: str("blueprintVersion"),
      model: str("model"),
      endpoint: str("endpoint"),
      qualifier: env === "prod" ? "live" : "DEFAULT",
    };
  }
  return result;
}

export interface UsageSummary {
  input: number | null;
  output: number | null;
  total: number | null;
}

/** Token usage as the harness reports it (camelCase from the backend; snake_case tolerated). */
export function usageSummary(usage: InvokeUsage | Record<string, unknown> | null | undefined): UsageSummary {
  const u = (usage ?? {}) as Record<string, unknown>;
  const input = toNumber(u.inputTokens ?? u.input_tokens);
  const output = toNumber(u.outputTokens ?? u.output_tokens);
  const total = toNumber(u.totalTokens ?? u.total_tokens) ?? (input !== null || output !== null ? (input ?? 0) + (output ?? 0) : null);
  return { input, output, total };
}

export interface BudgetDetails {
  alias: string | null;
  maxBudget: number | null;
  spend: number | null;
  budgetResetAt: string | null;
  currentCost: number | null;
}

/** The cap, spend and reset time from a BUDGET_EXCEEDED job error (worker_jobs.budget_details), null for other errors. */
export function budgetDetails(error: JobError | null | undefined): BudgetDetails | null {
  if (!error || error.code !== "BUDGET_EXCEEDED") {
    return null;
  }
  const d = (error.details ?? {}) as Record<string, unknown>;
  return {
    alias: typeof d.alias === "string" ? d.alias : null,
    maxBudget: toNumber(d.maxBudget),
    spend: toNumber(d.spend),
    budgetResetAt: typeof d.budgetResetAt === "string" ? d.budgetResetAt : null,
    currentCost: toNumber(d.currentCost),
  };
}

export type TurnStatus = "queued" | "running" | "done" | "failed";

export interface Turn {
  id: string;
  prompt: string;
  status: TurnStatus;
  startedAt: number;
  jobId: string | null;
  output: InvokeOutput | null;
  error: JobError | null;
  feedback: "up" | "down" | null;
}

/** First turn of a session: a fresh microVM (25-35 s); later turns about 4 s. */
export function expectedSeconds(turnIndex: number): string {
  return turnIndex === 0 ? "25-35 s (new session: a fresh microVM starts)" : "about 4 s";
}

export const SUGGESTED_PROMPTS: ReadonlyArray<{ id: string; text: string }> = [
  { id: "leave-balance", text: "What is my leave balance? My employee id is E1001." },
  { id: "remote-work", text: "How many days per week can I work remotely under the remote work policy?" },
  { id: "public-holidays", text: "Which public holidays are there in November 2026?" },
  { id: "long-leave", text: "Submit an annual leave request for employee E1012: 12 days starting 2027-02-01." },
];
