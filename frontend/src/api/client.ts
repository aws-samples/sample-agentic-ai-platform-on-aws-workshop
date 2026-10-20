/**
 * Platform API client: a fetch wrapper (bearer ID token, JSON, 30 s timeout) with one typed method per route of
 * build contract section 4 - the same names as platform_sdk/platform_sdk/api.py in camelCase. Errors arrive as
 * `ApiError(code, message, status)` built from the contract shape `{"error": {"code", "message", "details"?}}`.
 */
import type {
  AgentKey,
  Blueprint,
  BlueprintCreate,
  CatalogueRecord,
  CatalogueStatusResult,
  DecisionStatus,
  Domain,
  DomainCreate,
  Env,
  EvaluationSubmission,
  Feedback,
  FeedbackCreate,
  GatewayMode,
  GatewayModeResult,
  Governance,
  Health,
  InvokeRequest,
  Job,
  JobAccepted,
  KeyCreate,
  KeyUpdate,
  Me,
  Metrics,
  ModelGatewayCredentials,
  Persona,
  Project,
  ProjectCreate,
  ProjectDeleteSummary,
  ProjectOptions,
  ProjectUpdate,
  RecordStatus,
  Release,
  RenderedHarness,
  Resource,
  ResourceUpsert,
  Settings,
  SettingsUpdate,
  Spend,
  SwitchPersonaResult,
} from "./types";
import { TERMINAL_JOB_STATES } from "./types";

export const DEFAULT_TIMEOUT_MS = 30_000;
export const JOB_POLL_INTERVAL_MS = 2_000;
export const JOB_POLL_CAP_MS = 10 * 60_000;

export class ApiError extends Error {
  readonly code: string;
  readonly status: number | null;
  readonly details?: unknown;
  readonly body?: unknown;

  constructor(code: string, message: string, status: number | null = null, body?: unknown, details?: unknown) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
    this.body = body;
    this.details = details;
  }

  /** "CODE (status): message" like PlatformApiError.__str__. */
  toString(): string {
    return this.status ? `${this.code} (${this.status}): ${this.message}` : `${this.code}: ${this.message}`;
  }
}

export function isApiError(value: unknown): value is ApiError {
  return value instanceof ApiError || (typeof value === "object" && value !== null && (value as Error).name === "ApiError");
}

/** The ApiError for an HTTP status >= 400 and its (parsed or raw) body; mirrors PlatformClient._error in api.py. */
export function errorFromResponse(status: number, body: unknown, statusText = ""): ApiError {
  if (typeof body === "object" && body !== null && !Array.isArray(body)) {
    const record = body as Record<string, unknown>;
    const error = record.error;
    if (typeof error === "object" && error !== null) {
      const err = error as Record<string, unknown>;
      const code = typeof err.code === "string" && err.code ? err.code : `HTTP_${status}`;
      const message = typeof err.message === "string" && err.message ? err.message : JSON.stringify(body);
      return new ApiError(code, message, status, body, err.details);
    }
    if ("message" in record) {
      // API Gateway's own errors: {"message": "Unauthorized"}
      return new ApiError(`HTTP_${status}`, String(record.message), status, body);
    }
  }
  const text = typeof body === "string" ? body : body == null ? "" : JSON.stringify(body);
  return new ApiError(`HTTP_${status}`, (text || statusText || `HTTP ${status}`).slice(0, 500), status, body);
}

export type QueryParams = Record<string, string | number | boolean | null | undefined>;

export interface RequestOptions {
  query?: QueryParams;
  body?: unknown;
  signal?: AbortSignal;
}

export interface ClientOptions {
  baseUrl: string;
  /** The Cognito ID token (bearer); undefined when signed out. */
  getToken: () => string | undefined | null | Promise<string | undefined | null>;
  timeoutMs?: number;
  fetchImpl?: typeof fetch;
  /** Called on every 401 (expired token): the auth layer redirects to the Hosted UI. */
  onUnauthorized?: (error: ApiError) => void;
}

export function buildUrl(baseUrl: string, path: string, query?: QueryParams): string {
  const url = new URL(path.startsWith("/") ? path : `/${path}`, `${baseUrl.replace(/\/+$/, "")}/`);
  // URL() drops the base path when the path is absolute; rebuild from the base's pathname instead.
  const base = new URL(baseUrl.replace(/\/+$/, "") + "/");
  url.pathname = `${base.pathname.replace(/\/+$/, "")}${path.startsWith("/") ? path : `/${path}`}`;
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined && value !== null && value !== "") {
        url.searchParams.set(key, String(value));
      }
    }
  }
  return url.toString();
}

export class PlatformClient {
  readonly baseUrl: string;
  private readonly getToken: ClientOptions["getToken"];
  private readonly timeoutMs: number;
  private readonly fetchImpl: typeof fetch;
  private readonly onUnauthorized?: ClientOptions["onUnauthorized"];

  constructor(options: ClientOptions) {
    this.baseUrl = options.baseUrl.replace(/\/+$/, "");
    this.getToken = options.getToken;
    this.timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.fetchImpl = options.fetchImpl ?? ((input, init) => fetch(input, init));
    this.onUnauthorized = options.onUnauthorized;
  }

  // ---- plumbing ------------------------------------------------------------------------------------------------
  async request<T>(method: string, path: string, options: RequestOptions = {}): Promise<T> {
    const headers: Record<string, string> = { Accept: "application/json" };
    const token = await this.getToken();
    if (token) {
      headers.Authorization = `Bearer ${token}`;
    }
    let bodyText: string | undefined;
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      bodyText = JSON.stringify(options.body);
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    const onOuterAbort = () => controller.abort();
    options.signal?.addEventListener("abort", onOuterAbort);
    let response: Response;
    try {
      response = await this.fetchImpl(buildUrl(this.baseUrl, path, options.query), {
        method,
        headers,
        body: bodyText,
        signal: controller.signal,
      });
    } catch (error) {
      if (controller.signal.aborted && !options.signal?.aborted) {
        throw new ApiError("TIMEOUT", `${method} ${path} did not answer within ${Math.round(this.timeoutMs / 1000)} s`);
      }
      if (options.signal?.aborted) {
        throw new ApiError("ABORTED", `${method} ${path} was cancelled`);
      }
      throw new ApiError("NETWORK_ERROR", `${method} ${path} failed: ${(error as Error).message}`);
    } finally {
      clearTimeout(timer);
      options.signal?.removeEventListener("abort", onOuterAbort);
    }
    const text = await response.text();
    let parsed: unknown = undefined;
    if (text) {
      try {
        parsed = JSON.parse(text);
      } catch {
        parsed = text;
      }
    }
    if (response.status >= 400) {
      const error = errorFromResponse(response.status, parsed, response.statusText);
      if (response.status === 401) {
        this.onUnauthorized?.(error);
      }
      throw error;
    }
    if (response.status === 204 || !text) {
      return undefined as T;
    }
    if (typeof parsed === "string") {
      throw new ApiError("BAD_RESPONSE", `non-JSON body: ${parsed.slice(0, 200)}`, response.status, parsed);
    }
    return parsed as T;
  }

  get<T>(path: string, query?: QueryParams, signal?: AbortSignal): Promise<T> {
    return this.request<T>("GET", path, { query, signal });
  }

  /** GET a list route: a bare JSON array, or an envelope such as {"items": [...]} (asList). */
  list<T>(path: string, query?: QueryParams, signal?: AbortSignal): Promise<T[]> {
    return this.request<unknown>("GET", path, { query, signal }).then((response) => asList<T>(response));
  }

  post<T>(path: string, body: unknown = {}, signal?: AbortSignal): Promise<T> {
    return this.request<T>("POST", path, { body, signal });
  }

  put<T>(path: string, body: unknown): Promise<T> {
    return this.request<T>("PUT", path, { body });
  }

  patch<T>(path: string, body: unknown): Promise<T> {
    return this.request<T>("PATCH", path, { body });
  }

  delete<T>(path: string, query?: QueryParams): Promise<T> {
    return this.request<T>("DELETE", path, { query });
  }

  // ---- public, me, settings ------------------------------------------------------------------------------------
  health(): Promise<Health> {
    return this.get("/health");
  }

  me(signal?: AbortSignal): Promise<Me> {
    return this.get("/me", undefined, signal);
  }

  /** POST /me/switch: the tokens of another persona (the "Switch persona" menu; a workshop shortcut, see src/auth/switchPersona.ts). */
  switchPersona(persona: Persona): Promise<SwitchPersonaResult> {
    return this.post("/me/switch", { persona });
  }

  getSettings(signal?: AbortSignal): Promise<Settings> {
    return this.get("/settings", undefined, signal);
  }

  putSettings(update: SettingsUpdate): Promise<Settings> {
    return this.put("/settings", update);
  }

  // ---- domains and projects ------------------------------------------------------------------------------------
  listDomains(signal?: AbortSignal): Promise<Domain[]> {
    return this.list<Domain>("/domains", undefined, signal);
  }

  createDomain(domain: DomainCreate): Promise<Domain> {
    return this.post("/domains", { description: "", owner: "", ...domain });
  }

  getDomain(domainId: string, signal?: AbortSignal): Promise<Domain> {
    return this.get(`/domains/${encodeURIComponent(domainId)}`, undefined, signal);
  }

  deleteDomain(domainId: string): Promise<{ deleted: string }> {
    return this.delete(`/domains/${encodeURIComponent(domainId)}`);
  }

  listProjects(domainId?: string, signal?: AbortSignal): Promise<Project[]> {
    return this.list<Project>("/projects", { domainId }, signal);
  }

  createProject(project: ProjectCreate): Promise<Project> {
    return this.post("/projects", project);
  }

  getProject(projectId: string, signal?: AbortSignal): Promise<Project> {
    return this.get(`/projects/${encodeURIComponent(projectId)}`, undefined, signal);
  }

  updateProject(projectId: string, fields: ProjectUpdate): Promise<Project> {
    return this.patch(`/projects/${encodeURIComponent(projectId)}`, fields);
  }

  deleteProject(projectId: string, force = false): Promise<ProjectDeleteSummary> {
    return this.delete(`/projects/${encodeURIComponent(projectId)}`, { force: force ? "true" : undefined });
  }

  // ---- resources -----------------------------------------------------------------------------------------------
  listResources(projectId: string, env?: Env, signal?: AbortSignal): Promise<Resource[]> {
    return this.list<Resource>(`/projects/${encodeURIComponent(projectId)}/resources`, { env }, signal);
  }

  registerResource(projectId: string, resource: ResourceUpsert): Promise<Resource> {
    return this.put(`/projects/${encodeURIComponent(projectId)}/resources`, resource);
  }

  deleteResource(projectId: string, env: Env, type: string, name: string): Promise<unknown> {
    return this.delete(
      `/projects/${encodeURIComponent(projectId)}/resources/${env}/${encodeURIComponent(type)}/${encodeURIComponent(name)}`,
    );
  }

  // ---- blueprints and deploy -----------------------------------------------------------------------------------
  listBlueprints(projectId: string, signal?: AbortSignal): Promise<Blueprint[]> {
    return this.list<Blueprint>(`/projects/${encodeURIComponent(projectId)}/blueprints`, undefined, signal);
  }

  createBlueprint(projectId: string, blueprint: BlueprintCreate): Promise<Blueprint> {
    return this.post(`/projects/${encodeURIComponent(projectId)}/blueprints`, { notes: "", ...blueprint });
  }

  getBlueprint(projectId: string, version: number | string, signal?: AbortSignal): Promise<Blueprint> {
    return this.get(`/projects/${encodeURIComponent(projectId)}/blueprints/${version}`, undefined, signal);
  }

  /** Dry run: the harness payload the platform would create for this environment (no side effects). */
  renderBlueprint(projectId: string, version: number | string, env: Env = "dev", signal?: AbortSignal): Promise<RenderedHarness> {
    return this.get(`/projects/${encodeURIComponent(projectId)}/blueprints/${version}/render`, { env }, signal);
  }

  /** POST /projects/{id}/deploy -> 202 {jobId}; the worker renders and creates or updates the Dev harness. */
  deploy(projectId: string, version: number | string, env: Env = "dev"): Promise<JobAccepted> {
    return this.post(`/projects/${encodeURIComponent(projectId)}/deploy`, { version, env });
  }

  // ---- releases ------------------------------------------------------------------------------------------------
  listReleases(projectId: string, signal?: AbortSignal): Promise<Release[]> {
    return this.list<Release>(`/projects/${encodeURIComponent(projectId)}/releases`, undefined, signal);
  }

  /** GET /releases: every release of every project, newest first (the Releases screen). */
  listAllReleases(signal?: AbortSignal): Promise<Release[]> {
    return this.list<Release>("/releases", undefined, signal);
  }

  /**
   * POST /projects/{id}/releases -> 201. With `autoRun` (the default) the platform runs the release at once: the answer is already
   * EVALUATING with `pipeline` set, or DRAFT with `error` (WORKER_UNAVAILABLE) when the worker could not be started.
   */
  createRelease(projectId: string, blueprintVersion: number | string, notes = "", autoRun = true): Promise<Release> {
    return this.post(`/projects/${encodeURIComponent(projectId)}/releases`, { blueprintVersion, notes, autoRun });
  }

  getRelease(releaseId: string, signal?: AbortSignal): Promise<Release> {
    return this.get(`/releases/${encodeURIComponent(releaseId)}`, undefined, signal);
  }

  /**
   * POST /releases/{id}/run -> 202 {jobId, status: EVALUATING, type: "release", stage}: deploy the version to Dev if needed, run the
   * dataset, apply the thresholds (developer or admin; DRAFT, BLOCKED or a stalled EVALUATING release, otherwise 409).
   */
  runRelease(releaseId: string): Promise<JobAccepted & { releaseId: string; stage: string }> {
    return this.post(`/releases/${encodeURIComponent(releaseId)}/run`);
  }

  /** Manual submission of a dataset run's scores (the CLI); the platform's own runs arrive through `runRelease`. */
  submitEvaluation(releaseId: string, evaluation: EvaluationSubmission): Promise<Release> {
    return this.post(`/releases/${encodeURIComponent(releaseId)}/evaluation`, evaluation);
  }

  approveRelease(releaseId: string, comment = ""): Promise<Release> {
    return this.post(`/releases/${encodeURIComponent(releaseId)}/approve`, { comment });
  }

  rejectRelease(releaseId: string, comment = ""): Promise<Release> {
    return this.post(`/releases/${encodeURIComponent(releaseId)}/reject`, { comment });
  }

  /** POST /releases/{id}/promote -> 202 {jobId}; the worker renders for prod and repoints `live`. */
  promoteRelease(releaseId: string): Promise<JobAccepted> {
    return this.post(`/releases/${encodeURIComponent(releaseId)}/promote`);
  }

  // ---- keys ----------------------------------------------------------------------------------------------------
  listKeys(projectId: string, signal?: AbortSignal): Promise<AgentKey[]> {
    return this.list<AgentKey>(`/projects/${encodeURIComponent(projectId)}/keys`, undefined, signal);
  }

  createKey(projectId: string, key: KeyCreate): Promise<AgentKey> {
    return this.post(`/projects/${encodeURIComponent(projectId)}/keys`, key);
  }

  /** PATCH /projects/{id}/keys/{env}/{model} with maxBudget and/or status (active | suspended). */
  updateKey(projectId: string, env: Env, model: string, update: KeyUpdate): Promise<AgentKey> {
    return this.patch(`/projects/${encodeURIComponent(projectId)}/keys/${env}/${encodeURIComponent(model)}`, update);
  }

  // ---- catalogue and governance --------------------------------------------------------------------------------
  listCatalogue(status?: RecordStatus | string, signal?: AbortSignal): Promise<CatalogueRecord[]> {
    return this.list<CatalogueRecord>("/catalogue", { status }, signal);
  }

  searchCatalogue(q: string, signal?: AbortSignal): Promise<CatalogueRecord[]> {
    return this.list<CatalogueRecord>("/catalogue/search", { q }, signal);
  }

  /** One record with its descriptors (also for DRAFT / PENDING_APPROVAL records, which search does not return). */
  getCatalogueRecord(recordId: string, signal?: AbortSignal): Promise<CatalogueRecord> {
    return this.get(`/catalogue/${encodeURIComponent(recordId)}`, undefined, signal);
  }

  setCatalogueStatus(recordId: string, status: DecisionStatus, reason = ""): Promise<CatalogueStatusResult> {
    return this.post(`/catalogue/${encodeURIComponent(recordId)}/status`, { status, reason });
  }

  governance(signal?: AbortSignal): Promise<Governance> {
    return this.get("/governance", undefined, signal);
  }

  setGatewayMode(gatewayId: string, mode: GatewayMode): Promise<GatewayModeResult> {
    return this.post(`/governance/gateways/${encodeURIComponent(gatewayId)}/mode`, { mode });
  }

  /** Admin only: the LiteLLM admin UI sign-in (user admin, password = the master key), shown masked in Governance. */
  modelGatewayCredentials(signal?: AbortSignal): Promise<ModelGatewayCredentials> {
    return this.get("/governance/model-gateway/credentials", undefined, signal);
  }

  /** Admin only: pick lists for the project form (tool Lambdas and knowledge bases with their names). */
  projectOptions(signal?: AbortSignal): Promise<ProjectOptions> {
    return this.get("/governance/project-options", undefined, signal);
  }

  // ---- metrics and spend ---------------------------------------------------------------------------------------
  metrics(projectId: string, env: Env, hours = 24, signal?: AbortSignal): Promise<Metrics> {
    return this.get("/metrics", { projectId, env, hours }, signal);
  }

  spend(projectId: string, env?: Env, signal?: AbortSignal): Promise<Spend> {
    return this.get("/spend", { projectId, env }, signal);
  }

  // ---- playground (asynchronous jobs) --------------------------------------------------------------------------
  /** POST /invoke -> 202 {jobId, sessionId}; the worker runs InvokeHarness (qualifier `live` for prod). */
  invoke(request: InvokeRequest): Promise<JobAccepted & { sessionId: string }> {
    return this.post("/invoke", request);
  }

  getJob<TOutput = Record<string, unknown>>(jobId: string, signal?: AbortSignal): Promise<Job<TOutput>> {
    return this.get(`/jobs/${encodeURIComponent(jobId)}`, undefined, signal);
  }

  /** Poll GET /jobs/{id} every 2 s until SUCCEEDED or FAILED (10 min cap -> ApiError JOB_TIMEOUT). */
  async waitForJob<TOutput = Record<string, unknown>>(
    jobId: string,
    options: { timeoutMs?: number; pollMs?: number; signal?: AbortSignal; onUpdate?: (job: Job<TOutput>) => void } = {},
  ): Promise<Job<TOutput>> {
    const timeoutMs = options.timeoutMs ?? JOB_POLL_CAP_MS;
    const pollMs = options.pollMs ?? JOB_POLL_INTERVAL_MS;
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      const job = await this.getJob<TOutput>(jobId, options.signal);
      options.onUpdate?.(job);
      if (TERMINAL_JOB_STATES.includes(job.status)) {
        return job;
      }
      if (Date.now() >= deadline) {
        throw new ApiError("JOB_TIMEOUT", `job ${jobId} still ${job.status} after ${Math.round(timeoutMs / 1000)} s`);
      }
      await sleep(pollMs, options.signal);
    }
  }

  /** Invoke, wait and return the finished job (output.text, output.toolsUsed, output.usage). */
  async ask(request: InvokeRequest, options?: { timeoutMs?: number; signal?: AbortSignal }): Promise<Job> {
    const accepted = await this.invoke(request);
    const job = await this.waitForJob(accepted.jobId, options);
    if (!job.output || typeof job.output !== "object") {
      job.output = {};
    }
    if (!("sessionId" in job.output)) {
      (job.output as Record<string, unknown>).sessionId = accepted.sessionId;
    }
    return job;
  }

  // ---- feedback ------------------------------------------------------------------------------------------------
  feedback(entry: FeedbackCreate): Promise<Feedback> {
    return this.post("/feedback", { comment: "", ...entry });
  }

  listFeedback(projectId?: string, signal?: AbortSignal): Promise<Feedback[]> {
    return this.list<Feedback>("/feedback", { projectId }, signal);
  }
}

/** Accept a bare JSON list or an envelope such as {"items": [...]} (platform_sdk.api._as_list). */
export function asList<T>(response: unknown): T[] {
  if (response == null) {
    return [];
  }
  if (Array.isArray(response)) {
    return response as T[];
  }
  if (typeof response === "object") {
    for (const value of Object.values(response as Record<string, unknown>)) {
      if (Array.isArray(value)) {
        return value as T[];
      }
    }
  }
  throw new ApiError("BAD_RESPONSE", `expected a list, got ${JSON.stringify(response).slice(0, 200)}`);
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(timer);
      reject(new ApiError("ABORTED", "job polling was cancelled"));
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

/** Human-readable message for an unknown error (ApiError, Error or anything else). */
export function describeError(error: unknown): { code: string; message: string; status: number | null } {
  if (isApiError(error)) {
    return { code: error.code, message: error.message, status: error.status };
  }
  if (error instanceof Error) {
    return { code: error.name || "ERROR", message: error.message, status: null };
  }
  return { code: "ERROR", message: String(error), status: null };
}
