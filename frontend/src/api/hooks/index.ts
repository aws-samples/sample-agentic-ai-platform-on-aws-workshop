/** React Query hooks, one per resource; mutations invalidate the queries they change. */
import { useMutation, useQuery, useQueryClient, type UseQueryOptions } from "@tanstack/react-query";

import { useApi } from "../ApiProvider";
import { qk } from "../queryKeys";
import type {
  AgentKey,
  Blueprint,
  CatalogueRecord,
  DecisionStatus,
  Domain,
  DomainCreate,
  Env,
  Feedback,
  FeedbackCreate,
  GatewayMode,
  Governance,
  InvokeRequest,
  KeyCreate,
  KeyUpdate,
  Me,
  Metrics,
  ModelGatewayCredentials,
  Project,
  ProjectCreate,
  ProjectOptions,
  ProjectUpdate,
  Release,
  RenderedHarness,
  Resource,
  Settings,
  SettingsUpdate,
  Spend,
} from "../types";

export { isTerminalJob, jobError, useJob } from "./useJob";
export { useApproveRelease, useCreateRelease, usePromoteRelease, useRejectRelease, useRunRelease } from "./releases";

type QueryOverrides<T> = Omit<UseQueryOptions<T, Error>, "queryKey" | "queryFn">;

// ---- me and settings -----------------------------------------------------------------------------------------------
export function useMeQuery(enabled: boolean) {
  const api = useApi();
  return useQuery<Me, Error>({
    queryKey: qk.me(),
    queryFn: ({ signal }) => api.me(signal),
    enabled,
    staleTime: 5 * 60_000,
  });
}

export function useSettings(overrides?: QueryOverrides<Settings>) {
  const api = useApi();
  return useQuery<Settings, Error>({ queryKey: qk.settings(), queryFn: ({ signal }) => api.getSettings(signal), ...overrides });
}

export function usePutSettings() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (update: SettingsUpdate) => api.putSettings(update),
    onSuccess: (settings) => {
      queryClient.setQueryData(qk.settings(), settings);
      void queryClient.invalidateQueries({ queryKey: qk.governance() });
    },
  });
}

// ---- domains -------------------------------------------------------------------------------------------------------
export function useDomains(overrides?: QueryOverrides<Domain[]>) {
  const api = useApi();
  return useQuery<Domain[], Error>({ queryKey: qk.domains(), queryFn: ({ signal }) => api.listDomains(signal), ...overrides });
}

export function useCreateDomain() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (domain: DomainCreate) => api.createDomain(domain),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: qk.domains() }),
  });
}

export function useDeleteDomain() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (domainId: string) => api.deleteDomain(domainId),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: qk.domains() }),
  });
}

// ---- projects ------------------------------------------------------------------------------------------------------
export function useProjects(domainId?: string, overrides?: QueryOverrides<Project[]>) {
  const api = useApi();
  return useQuery<Project[], Error>({
    queryKey: qk.projects(domainId),
    queryFn: ({ signal }) => api.listProjects(domainId, signal),
    ...overrides,
  });
}

export function useProject(projectId: string | undefined, overrides?: QueryOverrides<Project>) {
  const api = useApi();
  return useQuery<Project, Error>({
    queryKey: qk.project(projectId ?? ""),
    queryFn: ({ signal }) => api.getProject(projectId as string, signal),
    enabled: !!projectId,
    ...overrides,
  });
}

function invalidateProjects(queryClient: ReturnType<typeof useQueryClient>, projectId?: string) {
  void queryClient.invalidateQueries({ queryKey: ["projects"] });
  void queryClient.invalidateQueries({ queryKey: qk.domains() });
  if (projectId) {
    void queryClient.invalidateQueries({ queryKey: qk.project(projectId) });
  }
}

export function useCreateProject() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (project: ProjectCreate) => api.createProject(project),
    onSuccess: (project) => invalidateProjects(queryClient, project.projectId),
  });
}

export function useUpdateProject() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ projectId, fields }: { projectId: string; fields: ProjectUpdate }) => api.updateProject(projectId, fields),
    onSuccess: (project) => {
      queryClient.setQueryData(qk.project(project.projectId), project);
      invalidateProjects(queryClient);
      // the LiteLLM team caps changed: the Overview's "Project cap" line reads them from GET /spend
      void queryClient.invalidateQueries({ queryKey: ["spend", project.projectId] });
    },
  });
}

export function useDeleteProject() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ projectId, force }: { projectId: string; force?: boolean }) => api.deleteProject(projectId, force),
    onSuccess: (_result, variables) => {
      queryClient.removeQueries({ queryKey: qk.project(variables.projectId) });
      invalidateProjects(queryClient);
      void queryClient.invalidateQueries({ queryKey: ["releases"] });
    },
  });
}

// ---- resources, keys, blueprints -----------------------------------------------------------------------------------
export function useResources(projectId: string | undefined, env?: Env, overrides?: QueryOverrides<Resource[]>) {
  const api = useApi();
  return useQuery<Resource[], Error>({
    queryKey: qk.resources(projectId ?? "", env),
    queryFn: ({ signal }) => api.listResources(projectId as string, env, signal),
    enabled: !!projectId,
    ...overrides,
  });
}

export function useKeys(projectId: string | undefined, overrides?: QueryOverrides<AgentKey[]>) {
  const api = useApi();
  return useQuery<AgentKey[], Error>({
    queryKey: qk.keys(projectId ?? ""),
    queryFn: ({ signal }) => api.listKeys(projectId as string, signal),
    enabled: !!projectId,
    ...overrides,
  });
}

function invalidateKeys(queryClient: ReturnType<typeof useQueryClient>, projectId: string) {
  void queryClient.invalidateQueries({ queryKey: qk.keys(projectId) });
  void queryClient.invalidateQueries({ queryKey: ["spend", projectId] });
  void queryClient.invalidateQueries({ queryKey: ["resources", projectId] });
}

export function useCreateKey() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ projectId, key }: { projectId: string; key: KeyCreate }) => api.createKey(projectId, key),
    onSuccess: (_key, variables) => invalidateKeys(queryClient, variables.projectId),
  });
}

export function useUpdateKey() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ projectId, env, model, update }: { projectId: string; env: Env; model: string; update: KeyUpdate }) =>
      api.updateKey(projectId, env, model, update),
    onSuccess: (_key, variables) => invalidateKeys(queryClient, variables.projectId),
  });
}

export function useBlueprints(projectId: string | undefined, overrides?: QueryOverrides<Blueprint[]>) {
  const api = useApi();
  return useQuery<Blueprint[], Error>({
    queryKey: qk.blueprints(projectId ?? ""),
    queryFn: ({ signal }) => api.listBlueprints(projectId as string, signal),
    enabled: !!projectId,
    ...overrides,
  });
}

/** Dry-run render of a blueprint version for an environment (400 BLUEPRINT_UNRESOLVED names the missing resource and the module that creates it). */
export function useRenderBlueprint(projectId: string | undefined, version: number | string | undefined, env: Env, enabled = true) {
  const api = useApi();
  return useQuery<RenderedHarness, Error>({
    queryKey: qk.render(projectId ?? "", version ?? "", env),
    queryFn: ({ signal }) => api.renderBlueprint(projectId as string, version as number | string, env, signal),
    enabled: enabled && !!projectId && version !== undefined && version !== null && version !== "",
    retry: false,
  });
}

// ---- releases ------------------------------------------------------------------------------------------------------
export function useReleases(projectId: string | undefined, overrides?: QueryOverrides<Release[]>) {
  const api = useApi();
  return useQuery<Release[], Error>({
    queryKey: qk.releases(projectId ?? ""),
    queryFn: ({ signal }) => api.listReleases(projectId as string, signal),
    enabled: !!projectId,
    ...overrides,
  });
}

export function useAllReleases(overrides?: QueryOverrides<Release[]>) {
  const api = useApi();
  return useQuery<Release[], Error>({ queryKey: qk.releases(), queryFn: ({ signal }) => api.listAllReleases(signal), ...overrides });
}

export function useRelease(releaseId: string | undefined, overrides?: QueryOverrides<Release>) {
  const api = useApi();
  return useQuery<Release, Error>({
    queryKey: qk.release(releaseId ?? ""),
    queryFn: ({ signal }) => api.getRelease(releaseId as string, signal),
    enabled: !!releaseId,
    ...overrides,
  });
}

// ---- catalogue -----------------------------------------------------------------------------------------------------
export function useCatalogue(status?: string, overrides?: QueryOverrides<CatalogueRecord[]>) {
  const api = useApi();
  return useQuery<CatalogueRecord[], Error>({
    queryKey: qk.catalogue(status),
    queryFn: ({ signal }) => api.listCatalogue(status, signal),
    ...overrides,
  });
}

export function useCatalogueSearch(q: string, overrides?: QueryOverrides<CatalogueRecord[]>) {
  const api = useApi();
  return useQuery<CatalogueRecord[], Error>({
    queryKey: qk.catalogueSearch(q),
    queryFn: ({ signal }) => api.searchCatalogue(q, signal),
    enabled: q.trim().length > 0,
    ...overrides,
  });
}

export function useCatalogueRecord(recordId: string | undefined, overrides?: QueryOverrides<CatalogueRecord>) {
  const api = useApi();
  return useQuery<CatalogueRecord, Error>({
    queryKey: qk.catalogueRecord(recordId ?? ""),
    queryFn: ({ signal }) => api.getCatalogueRecord(recordId as string, signal),
    enabled: !!recordId,
    ...overrides,
  });
}

export function useSetCatalogueStatus() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ recordId, status, reason }: { recordId: string; status: DecisionStatus; reason?: string }) =>
      api.setCatalogueStatus(recordId, status, reason),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["catalogue"] }),
  });
}

// ---- governance ----------------------------------------------------------------------------------------------------
export function useGovernance(overrides?: QueryOverrides<Governance>) {
  const api = useApi();
  return useQuery<Governance, Error>({
    queryKey: qk.governance(),
    queryFn: ({ signal }) => api.governance(signal),
    staleTime: 30_000,
    ...overrides,
  });
}

/** Admin only: fetched by the Governance screen's Model gateway card; the value stays masked until the eye icon is clicked. */
export function useModelGatewayCredentials(enabled: boolean, overrides?: QueryOverrides<ModelGatewayCredentials>) {
  const api = useApi();
  return useQuery<ModelGatewayCredentials, Error>({
    queryKey: qk.modelGatewayCredentials(),
    queryFn: ({ signal }) => api.modelGatewayCredentials(signal),
    enabled,
    staleTime: 5 * 60_000,
    retry: false,
    ...overrides,
  });
}

/** Admin only: the pick lists of the project form (tool Lambdas, knowledge bases); fetched while a project modal is open. */
export function useProjectOptions(enabled: boolean, overrides?: QueryOverrides<ProjectOptions>) {
  const api = useApi();
  return useQuery<ProjectOptions, Error>({
    queryKey: qk.projectOptions(),
    queryFn: ({ signal }) => api.projectOptions(signal),
    enabled,
    staleTime: 60_000,
    ...overrides,
  });
}

export function useSetGatewayMode() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ gatewayId, mode }: { gatewayId: string; mode: GatewayMode }) => api.setGatewayMode(gatewayId, mode),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: qk.governance() }),
  });
}

// ---- metrics, spend, feedback --------------------------------------------------------------------------------------
export function useMetrics(projectId: string | undefined, env: Env, hours: number, overrides?: QueryOverrides<Metrics>) {
  const api = useApi();
  return useQuery<Metrics, Error>({
    queryKey: qk.metrics(projectId ?? "", env, hours),
    queryFn: ({ signal }) => api.metrics(projectId as string, env, hours, signal),
    enabled: !!projectId,
    staleTime: 60_000,
    ...overrides,
  });
}

export function useSpend(projectId: string | undefined, env?: Env, overrides?: QueryOverrides<Spend>) {
  const api = useApi();
  return useQuery<Spend, Error>({
    queryKey: qk.spend(projectId ?? "", env),
    queryFn: ({ signal }) => api.spend(projectId as string, env, signal),
    enabled: !!projectId,
    staleTime: 30_000,
    ...overrides,
  });
}

export function useFeedback(projectId: string | undefined, overrides?: QueryOverrides<Feedback[]>) {
  const api = useApi();
  return useQuery<Feedback[], Error>({
    queryKey: qk.feedback(projectId),
    queryFn: ({ signal }) => api.listFeedback(projectId, signal),
    ...overrides,
  });
}

export function useSendFeedback() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (entry: FeedbackCreate) => api.feedback(entry),
    onSuccess: (_result, entry) => {
      void queryClient.invalidateQueries({ queryKey: qk.feedback(entry.projectId) });
      void queryClient.invalidateQueries({ queryKey: qk.feedback() });
    },
  });
}

/** POST /invoke -> 202 {jobId, sessionId}; follow it with useJob(jobId). */
export function useInvoke() {
  const api = useApi();
  return useMutation({ mutationFn: (request: InvokeRequest) => api.invoke(request) });
}
