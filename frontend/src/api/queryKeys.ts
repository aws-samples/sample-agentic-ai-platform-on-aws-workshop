/** React Query keys, one factory per resource so mutations can invalidate precisely. */
import type { Env } from "./types";

export const qk = {
  me: () => ["me"] as const,
  settings: () => ["settings"] as const,
  domains: () => ["domains"] as const,
  projects: (domainId?: string) => ["projects", domainId ?? "all"] as const,
  project: (projectId: string) => ["project", projectId] as const,
  resources: (projectId: string, env?: Env) => ["resources", projectId, env ?? "all"] as const,
  keys: (projectId: string) => ["keys", projectId] as const,
  blueprints: (projectId: string) => ["blueprints", projectId] as const,
  render: (projectId: string, version: number | string, env: Env) => ["render", projectId, String(version), env] as const,
  releases: (projectId?: string) => ["releases", projectId ?? "all"] as const,
  release: (releaseId: string) => ["release", releaseId] as const,
  catalogue: (status?: string) => ["catalogue", "list", status ?? "all"] as const,
  catalogueSearch: (q: string) => ["catalogue", "search", q] as const,
  catalogueRecord: (recordId: string) => ["catalogue", "record", recordId] as const,
  governance: () => ["governance"] as const,
  modelGatewayCredentials: () => ["model-gateway-credentials"] as const,
  projectOptions: () => ["project-options"] as const,
  metrics: (projectId: string, env: Env, hours: number) => ["metrics", projectId, env, hours] as const,
  spend: (projectId: string, env?: Env) => ["spend", projectId, env ?? "all"] as const,
  feedback: (projectId?: string) => ["feedback", projectId ?? "all"] as const,
  job: (jobId: string) => ["job", jobId] as const,
};
