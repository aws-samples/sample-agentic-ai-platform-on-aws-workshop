/**
 * Response and request shapes of the platform API (build contract sections 3 and 4), as the backend 0.4.0 returns them
 * (verified live on 9 Oct 2026 - see .claude/docs/phase2b-ui-notes.md). Every list route returns a bare JSON array.
 */

export type Env = "dev" | "prod";
export const ENVS: ReadonlyArray<Env> = ["dev", "prod"];

export interface ApiErrorBody {
  error: { code: string; message: string; details?: unknown };
}

export interface Health {
  status: string;
  version: string;
  time: string;
}

export interface Me {
  username: string;
  email: string | null;
  groups: string[];
  role: string | null;
  roles: string[];
}

/** The three workshop personas; their usernames equal the role names (platform_sdk.auth.PERSONA_USERS). */
export type Persona = "admin" | "developer" | "approver";

/** POST /me/switch: another persona's Cognito tokens plus the decoded ID-token claims (the UI's "Switch persona" menu). */
export interface SwitchPersonaResult {
  persona: Persona;
  username: string;
  idToken: string;
  accessToken: string;
  refreshToken: string | null;
  expiresIn: number;
  claims: Record<string, unknown>;
}

export interface DefaultLimits {
  maxIterations: number;
  maxTokens: number;
  timeoutSeconds: number;
}

export interface DefaultProjectCap {
  maxBudget: number;
  budgetDuration: string;
  tpmLimit: number;
  rpmLimit: number;
}

export interface DefaultKeyCap {
  maxBudget: number;
  budgetDuration: string;
}

export interface Settings {
  registryId: string | null;
  registryArn: string | null;
  policyEngineId: string | null;
  policyEngineArn: string | null;
  defaultLimits: DefaultLimits;
  defaultProjectCap: DefaultProjectCap;
  defaultKeyCap: DefaultKeyCap;
  updatedAt: string | null;
  updatedBy: string | null;
}

/** PUT /settings merges: nested objects deep-merge, `null` removes a key. */
export interface SettingsUpdate {
  registryId?: string | null;
  registryArn?: string | null;
  policyEngineId?: string | null;
  policyEngineArn?: string | null;
  defaultLimits?: Partial<Record<keyof DefaultLimits, number | null>>;
  defaultProjectCap?: Partial<Record<keyof DefaultProjectCap, number | string | null>>;
  defaultKeyCap?: Partial<Record<keyof DefaultKeyCap, number | string | null>>;
}

export interface Domain {
  domainId: string;
  name: string;
  description: string;
  owner: string;
  createdAt: string;
  createdBy: string;
  projectCount?: number;
}

export interface DomainCreate {
  domainId: string;
  name: string;
  description?: string;
  owner?: string;
}

export interface ProjectLiteLLM {
  teamId: string;
  teamAlias: string;
  maxBudget: number;
  budgetDuration: string;
  tpmLimit: number;
  rpmLimit: number;
  models: string[];
}

export interface ProjectRoles {
  agentRoleArn: string;
  gatewayRoleArn: string;
}

export interface Project {
  projectId: string;
  domainId: string;
  name: string;
  description: string;
  status: string;
  litellm: ProjectLiteLLM;
  roles: Partial<Record<Env, ProjectRoles>>;
  allowedToolArns: string[];
  knowledgeBaseIds: string[];
  owner?: string;
  createdAt: string;
  createdBy: string;
  updatedAt?: string;
  updatedBy?: string;
}

export interface ProjectCreate {
  projectId: string;
  domainId: string;
  name: string;
  description?: string;
  models: string[];
  maxBudget?: number;
  budgetDuration?: string;
  tpmLimit?: number;
  rpmLimit?: number;
  allowedToolArns?: string[];
  knowledgeBaseIds?: string[];
  owner?: string;
}

export interface ProjectUpdate {
  name?: string;
  description?: string;
  owner?: string;
  models?: string[];
  maxBudget?: number;
  budgetDuration?: string;
  tpmLimit?: number;
  rpmLimit?: number;
  allowedToolArns?: string[];
  knowledgeBaseIds?: string[];
}

export interface ProjectDeleteSummary {
  deleted: string;
  keys: unknown[];
  team: boolean;
  roles: unknown[];
  items: number;
}

export const RESOURCE_TYPES = [
  "gateway",
  "gateway_target",
  "policy",
  "memory",
  "harness",
  "harness_endpoint",
  "credential_provider",
  "registry_record",
  "online_evaluation",
  "knowledge_base",
  "tool",
] as const;

export type ResourceType = (typeof RESOURCE_TYPES)[number];

export interface Resource {
  projectId: string;
  env: Env;
  type: ResourceType | string;
  name: string;
  arn: string | null;
  id: string | null;
  url: string | null;
  attributes: Record<string, unknown>;
  registeredAt: string;
  registeredBy: string;
  updatedAt?: string;
  updatedBy?: string;
}

export interface ResourceUpsert {
  env: Env;
  type: ResourceType | string;
  name: string;
  arn?: string;
  id?: string;
  url?: string;
  attributes?: Record<string, unknown>;
}

export interface Blueprint {
  projectId: string;
  version: number;
  spec: Record<string, unknown>;
  systemPrompt: string;
  notes: string;
  model: string;
  thresholds: Record<string, number>;
  createdAt: string;
  createdBy: string;
  existing?: boolean;
}

export interface BlueprintCreate {
  spec: Record<string, unknown>;
  systemPrompt: string;
  notes?: string;
}

/** The CreateHarness payload the platform would send (dry run); shape owned by platform_sdk.blueprint.render. */
export type RenderedHarness = Record<string, unknown>;

/** "release" runs the deploy stage of a release run and queues the "evaluate" job (the dataset run and the thresholds). */
export type JobType = "invoke" | "deploy" | "promote" | "release" | "evaluate";
export type JobStatus = "QUEUED" | "RUNNING" | "SUCCEEDED" | "FAILED";
export const TERMINAL_JOB_STATES: ReadonlyArray<JobStatus> = ["SUCCEEDED", "FAILED"];

export interface JobError {
  code: string;
  message: string;
  details?: Record<string, unknown>;
}

export interface Job<TOutput = Record<string, unknown>> {
  jobId: string;
  type: JobType;
  status: JobStatus;
  input: Record<string, unknown>;
  output: TOutput;
  error: JobError | string | null;
  createdBy: string;
  createdAt: string;
  updatedAt: string;
  ttl?: number;
}

export interface JobAccepted {
  jobId: string;
  status: JobStatus;
  type: JobType;
  projectId?: string;
  env?: Env;
  version?: number;
  sessionId?: string;
  releaseId?: string;
  blueprintVersion?: number;
  /** POST /releases/{id}/run: the pipeline stage the run starts in (DEPLOYING, or EVALUATING when the deploy is skipped). */
  stage?: string;
}

export interface DeployOutput {
  harnessArn: string;
  harnessId: string;
  harnessVersion: string | number;
  endpoint: string;
  logGroup: string;
  keyAlias: string;
  keyCreated: boolean;
  credentialProviderArn: string;
  model: string;
  blueprintVersion: number;
  seconds: number;
}

/** Output of a "release" job: the deploy stage of a release run (skipped when the Dev harness already runs the version); it queues the "evaluate" job. */
export interface ReleaseRunOutput {
  deploySkipped: boolean;
  evaluationJobId: string;
  deploy?: DeployOutput;
}

/** Output of an "evaluate" job: the dataset run against the Dev harness and the verdict of the version's thresholds. */
export interface EvaluateOutput {
  runId: string;
  datasetName: string;
  scenarioCount: number;
  verdict: "PASSED" | "BLOCKED";
  status: string;
  seconds: number;
  scores: Record<string, EvaluatorScore>;
  failedScenarios: string[];
}

export interface InvokeUsage {
  inputTokens?: number;
  outputTokens?: number;
  totalTokens?: number;
  [key: string]: unknown;
}

export interface InvokeOutput {
  text: string;
  toolsUsed: string[];
  usage: InvokeUsage;
  stopReason: string;
  sessionId: string;
  seconds: number;
  modelCalls?: number;
}

export type ReleaseStatus =
  | "DRAFT"
  | "EVALUATING"
  | "BLOCKED"
  | "PENDING_APPROVAL"
  | "APPROVED"
  | "REJECTED"
  | "PROMOTING"
  | "PROMOTED"
  | "FAILED";

export interface EvaluatorScore {
  mean: number;
  min?: number;
  max?: number;
  passRate?: number;
  count?: number;
  errors?: number;
  failed?: number;
}

export interface EvaluationDetail {
  evaluator?: string;
  actual?: number | null;
  threshold?: number;
  op?: string;
  passed?: boolean;
  [key: string]: unknown;
}

/** One missed threshold as stored by platform_sdk.evaluation.apply_thresholds: {evaluator, actual, threshold, op}. */
export interface FailureLike {
  evaluator?: string;
  actual?: number | null;
  threshold?: number | null;
  op?: string | null;
}

export interface ReleaseEvaluation {
  runId: string | null;
  datasetName: string | null;
  scenarioCount: number | null;
  scores: Record<string, EvaluatorScore>;
  thresholds: Record<string, number>;
  verdict: "PASSED" | "BLOCKED";
  passed: boolean;
  /** Objects since backend 0.4.0; plain strings only on rows written by older builds. */
  failures: Array<FailureLike | string>;
  details: Record<string, EvaluationDetail> | EvaluationDetail[];
  results: Array<Record<string, unknown>>;
  failedScenarios: string[];
  submittedBy: string;
  completedAt: string;
  /** "pipeline" when the platform ran the dataset itself (create or POST /releases/{id}/run), "api" for a manual POST /releases/{id}/evaluation. */
  source?: "pipeline" | "api";
}

export interface ReleaseApproval {
  by: string;
  at: string;
  comment: string;
  decision: "APPROVED" | "REJECTED";
}

/** What the promote job did about the Catalogue: the project's AGENT record (created or updated and approved), or why not. */
export interface PromotionRegistryRecord {
  name: string;
  recordId?: string;
  recordArn?: string | null;
  registryId?: string;
  status?: string;
  recordVersion?: string;
  action?: "created" | "updated";
  /** The record was written but left in a status that could not be approved (deprecated, failed, still updating). */
  warning?: string;
  /** No registry configured in the settings. */
  skipped?: string;
  /** The registry call failed; the promotion itself stands. */
  error?: string;
  /** The record exists but its resource row could not be written. */
  resourceError?: string;
}

/** POST /catalogue/agents/sync: the promote job's catalogue step, run again for every project's newest promoted release. */
export interface CatalogueAgentSyncEntry extends PromotionRegistryRecord {
  projectId: string;
  releaseId?: string;
}

export interface CatalogueAgentSync {
  agents: CatalogueAgentSyncEntry[];
}

export interface ReleasePromotion {
  jobId: string;
  startedAt?: string;
  by?: string;
  harnessVersion?: string | number;
  endpoint?: string;
  at?: string;
  harnessArn?: string;
  onlineEvaluationConfigId?: string;
  /** Absent on releases promoted before October 2026. */
  registryRecord?: PromotionRegistryRecord;
}

/** Where a release run is: DEPLOYING (the "release" job), EVALUATING (the "evaluate" job), then DONE or FAILED. */
export type PipelineStage = "DEPLOYING" | "EVALUATING" | "DONE" | "FAILED";

/** The platform-run evaluation of a release: deploy the blueprint version to Dev (unless the Dev harness runs it already), run the dataset, apply the thresholds. */
export interface ReleasePipeline {
  stage: PipelineStage;
  /** The job to poll now: the "release" job while DEPLOYING, the "evaluate" job while EVALUATING, the last one afterwards. */
  jobId: string | null;
  /** null when the Dev harness already ran this blueprint version (deploy skipped). */
  deployJobId: string | null;
  /** Set once the "evaluate" job is queued. */
  evaluationJobId: string | null;
  deploySkipped: boolean;
  startedAt: string;
  by: string;
  finishedAt?: string | null;
  harnessVersion?: string | null;
  harnessArn?: string | null;
  logGroup?: string | null;
  runId?: string | null;
  seconds?: number | null;
  /** Set with stage FAILED: `stage` names the stage that failed (DEPLOYING or EVALUATING), `jobId` the job that reported it. */
  error?: (JobError & { jobId?: string; stage?: PipelineStage | string }) | null;
}

export interface Release {
  releaseId: string;
  projectId: string;
  blueprintVersion: number;
  status: ReleaseStatus;
  notes: string;
  evaluation: ReleaseEvaluation | null;
  approval: ReleaseApproval | null;
  promotion: ReleasePromotion | null;
  /** null for releases created before the platform ran evaluations itself, or created with autoRun=false. */
  pipeline: ReleasePipeline | null;
  /** With status FAILED: the promotion failed. With status DRAFT: the evaluation run failed (deploy or evaluation) and the release can be run again. */
  error: (JobError & { jobId?: string; stage?: PipelineStage | string }) | null;
  createdAt: string;
  createdBy: string;
  updatedAt: string;
}

export interface EvaluationSubmission {
  runId?: string;
  datasetName?: string;
  scenarioCount?: number;
  scores: Record<string, EvaluatorScore>;
  results?: Array<Record<string, unknown>>;
  failedScenarios?: string[];
}

export type KeyStatus = "active" | "suspended";

export interface AgentKey {
  projectId: string;
  alias: string;
  env: Env;
  model: string;
  litellmKeyId: string;
  credentialProviderArn: string;
  credentialProviderName: string;
  maxBudget: number;
  budgetDuration: string;
  status: KeyStatus;
  adopted: boolean;
  createdAt: string;
  createdBy: string;
  updatedAt?: string;
  updatedBy?: string;
}

export interface KeyCreate {
  env: Env;
  model: string;
  maxBudget?: number;
  budgetDuration?: string;
  adopt?: { litellmKeyId: string; credentialProviderArn: string; maxBudget?: number; budgetDuration?: string };
}

export interface KeyUpdate {
  maxBudget?: number;
  status?: KeyStatus;
}

export type RecordStatus =
  | "DRAFT"
  | "PENDING_APPROVAL"
  | "APPROVED"
  | "REJECTED"
  | "DEPRECATED"
  | "CREATING"
  | "UPDATING"
  | "CREATE_FAILED"
  | "UPDATE_FAILED";

export const RECORD_STATUSES: ReadonlyArray<RecordStatus> = [
  "DRAFT",
  "PENDING_APPROVAL",
  "APPROVED",
  "REJECTED",
  "DEPRECATED",
  "CREATING",
  "UPDATING",
  "CREATE_FAILED",
  "UPDATE_FAILED",
];

export type DecisionStatus = "APPROVED" | "REJECTED" | "DEPRECATED";

/** The Catalogue tab a record belongs to: AGENT and MCP records by type, CUSTOM records by the `kind` in their descriptor. */
export type CatalogueCategory = "model" | "knowledge-base" | "tool" | "agent" | "other";

export interface CatalogueRecord {
  recordId: string;
  recordArn: string | null;
  name: string;
  displayName: string | null;
  description: string | null;
  recordType: string;
  recordVersion: string | null;
  status: RecordStatus | string;
  statusReason: string | null;
  createdAt: string | null;
  updatedAt: string | null;
  createdBy: string | null;
  /** Set by the backend (absent on backends older than October 2026: such records show under the All tab only). */
  category?: CatalogueCategory | null;
  /** Only on GET /catalogue/{recordId} and on search results. */
  descriptors?: Record<string, unknown>;
  customMetadata?: Record<string, unknown>;
}

export interface CatalogueStatusResult {
  recordId: string;
  recordArn: string | null;
  status: string;
  statusReason: string | null;
  updatedAt: string | null;
}

export interface GovernanceModel {
  alias: string;
  modelId: string | null;
  litellmModel?: string | null;
  provider: string;
  tier: string | null;
  description: string | null;
  inputCostPerToken: number | null;
  outputCostPerToken: number | null;
  region?: string | null;
  /** `name` is resolved by the backend (GetGuardrail) so the table can show a readable label next to the id. */
  guardrail: { id: string | null; version: string | null; name?: string | null } | null;
  source: "litellm" | "ssm";
}

export interface GuardrailSummary {
  topics?: string[];
  contentFilters?: Array<{ type: string; input: string; output: string }>;
  piiEntities?: Array<{ type: string; action: string }>;
  blockedInputMessaging?: string | null;
  blockedOutputsMessaging?: string | null;
  error?: string;
}

export interface Guardrail {
  id: string | null;
  version: string | null;
  arn: string | null;
  name: string | null;
  status?: string;
  description?: string;
  summary: GuardrailSummary;
}

export type GatewayMode = "LOG_ONLY" | "ENFORCE";

export interface GovernanceGateway {
  projectId: string;
  env: Env;
  gatewayId: string | null;
  name: string | null;
  arn: string | null;
  url: string | null;
  mode: GatewayMode | null;
  policyEngineArn: string | null;
  status: string | null;
  error?: string;
}

/** Where the model gateway is; `deployed` is false until the landing zone (module 1 of the learning path) exists. */
export interface GovernanceModelGateway {
  deployed: boolean;
  url: string | null;
  openaiBase: string | null;
  adminUiUrl: string | null;
  /** True on the private landing zone: an internal load balancer, reachable only from inside the VPC (the backend uses a relay Lambda). */
  private?: boolean;
  /** Human-readable origin of the callers that can reach it, e.g. "inside the VPC (relay function agent-platform-model-gateway-relay)". */
  reachableFrom?: string | null;
}

export interface Governance {
  models: GovernanceModel[];
  guardrail: Guardrail;
  policyEngine: { id: string | null; arn: string | null };
  gateways: GovernanceGateway[];
  registry: { id: string | null; arn: string | null };
  modelGateway?: GovernanceModelGateway;
}

/** GET /governance/model-gateway/credentials (admin only): the LiteLLM admin UI sign-in; the password is the master key. */
export interface ModelGatewayCredentials {
  url: string;
  adminUiUrl: string;
  username: string;
  password: string;
  secretArn: string;
}

/** A Lambda the gateway role may be allowed to invoke (GET /governance/project-options). */
export interface ToolOption {
  arn: string;
  name: string;
  description: string | null;
  /** Where the candidate was found: "scenario", "gateway <name> target <name>", "registered: <project>/<env> target <name>",
   * "project allow-list only" (named by a project, returned by no inventory). */
  sources: string[];
  /** The projects whose allowedToolArns name this Lambda (absent on backends older than October 2026). */
  projects?: string[];
  /** False when the row exists only in a project's allow-list (no inventory returned it): shown on the Governance screen,
   * left out of the project form's pick lists. Absent on older backends (treated as true). */
  inventoried?: boolean;
}

/** A Bedrock knowledge base the gateway role may be allowed to retrieve from (GET /governance/project-options). */
export interface KnowledgeBaseOption {
  id: string;
  arn: string;
  name: string;
  description: string | null;
  status: string | null;
  sources: string[];
  /** The projects whose knowledgeBaseIds name this knowledge base (absent on older backends). */
  projects?: string[];
  /** False when only a project's allow-list names it (see ToolOption.inventoried). */
  inventoried?: boolean;
}

/** Shared by the project form's pick lists and the Governance screen's Knowledge bases and Tools tabs. */
export interface ProjectOptions {
  tools: ToolOption[];
  knowledgeBases: KnowledgeBaseOption[];
  /** Listing problems the backend worked around (for example a missing bedrock:ListKnowledgeBases permission). */
  warnings: string[];
}

export interface GatewayModeResult {
  gatewayId: string;
  name: string | null;
  mode: GatewayMode | null;
  policyEngineArn: string | null;
  status: string | null;
}

/** [ISO timestamp, value] pairs sorted by time. */
export type Series = Array<[string, number]>;

export interface MetricsWindow {
  start: string;
  end: string;
  period: number;
}

export interface HarnessMetricsInfo {
  name: string;
  endpoint: string;
  metricName: string;
  arn: string | null;
  version: string | number | null;
  dimensions: Record<string, unknown>;
}

export interface PolicyMetricsInfo {
  policyEngineId: string;
  gatewayId: string;
  gatewayName: string | null;
  /** Tool evaluations (a tools/list request evaluates every tool once): never add these to the totals. */
  byTool: Record<string, { allow: number; deny: number }>;
  scope: "gateway";
  dimensions: Record<string, unknown>;
}

export interface EvaluationMetricsInfo {
  configId: string;
  name: string;
  evaluators: Record<string, { mean: number | null; samples: number; labels: Record<string, number> }>;
}

export interface Metrics {
  projectId: string;
  env: Env;
  hours: number;
  period: number;
  window: MetricsWindow | null;
  series: Record<string, Series>;
  totals: Record<string, number | null>;
  harness: HarnessMetricsInfo | null;
  policy: PolicyMetricsInfo | null;
  evaluation: EvaluationMetricsInfo | null;
}

export interface SpendKey {
  alias: string;
  env: Env;
  model: string;
  status: KeyStatus;
  litellmKeyId: string;
  credentialProviderArn: string;
  maxBudget: number | null;
  budgetDuration: string | null;
  spend: number | null;
  budgetResetAt: string | null;
  blocked: boolean | null;
  models?: string[];
  error?: string;
}

export interface SpendTeam {
  teamId: string;
  alias: string;
  spend: number;
  maxBudget: number | null;
  budgetDuration: string | null;
  budgetResetAt: string | null;
  tpmLimit: number | null;
  rpmLimit: number | null;
  models: string[];
  blocked: boolean;
  keys?: Array<Record<string, unknown>>;
}

export interface SpendLogRow {
  requestId: string | null;
  startTime: string | null;
  endTime: string | null;
  model: string | null;
  spend: number | null;
  promptTokens: number | null;
  completionTokens: number | null;
  totalTokens: number | null;
  keyAlias: string | null;
  teamId: string | null;
  callType: string | null;
}

export interface Spend {
  projectId: string;
  env: Env | null;
  team: SpendTeam | null;
  keys: SpendKey[];
  byEnv: Record<string, { keys: number; spend: number; maxBudget: number; blocked: number }>;
  logs: SpendLogRow[];
}

export interface InvokeRequest {
  projectId: string;
  env: Env;
  prompt: string;
  sessionId?: string;
}

export type FeedbackRating = "up" | "down";

export interface Feedback {
  feedbackId: string;
  projectId: string;
  env: Env;
  sessionId: string;
  rating: FeedbackRating;
  comment: string;
  createdBy: string;
  createdAt: string;
}

export interface FeedbackCreate {
  projectId: string;
  env: Env;
  sessionId: string;
  rating: FeedbackRating;
  comment?: string;
}
