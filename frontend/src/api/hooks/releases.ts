/** Release mutations (create, run, approve, reject, promote); every one invalidates the release lists and the release itself. */
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { useApi } from "../ApiProvider";
import { qk } from "../queryKeys";
import type { Release } from "../types";

function cacheRelease(queryClient: ReturnType<typeof useQueryClient>, release: Release) {
  queryClient.setQueryData(qk.release(release.releaseId), release);
  void queryClient.invalidateQueries({ queryKey: ["releases"] });
}

/** POST /projects/{id}/releases; with `autoRun` (the default) the release comes back EVALUATING with its pipeline, which the detail page follows. */
export function useCreateRelease() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ projectId, blueprintVersion, notes, autoRun }: { projectId: string; blueprintVersion: number; notes?: string; autoRun?: boolean }) =>
      api.createRelease(projectId, blueprintVersion, notes, autoRun),
    onSuccess: (release) => cacheRelease(queryClient, release),
  });
}

/** POST /releases/{id}/run -> 202 {jobId, status: EVALUATING, stage}; follow the run through the release's `pipeline.jobId` with useJob(jobId). */
export function useRunRelease() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (releaseId: string) => api.runRelease(releaseId),
    onSuccess: (_accepted, releaseId) => {
      void queryClient.invalidateQueries({ queryKey: qk.release(releaseId) });
      void queryClient.invalidateQueries({ queryKey: ["releases"] });
    },
  });
}

export function useApproveRelease() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ releaseId, comment }: { releaseId: string; comment?: string }) => api.approveRelease(releaseId, comment),
    onSuccess: (release) => cacheRelease(queryClient, release),
  });
}

export function useRejectRelease() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ releaseId, comment }: { releaseId: string; comment?: string }) => api.rejectRelease(releaseId, comment),
    onSuccess: (release) => cacheRelease(queryClient, release),
  });
}

/** POST /releases/{id}/promote -> 202 {jobId, status: PROMOTING}; follow the job with useJob(jobId). */
export function usePromoteRelease() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (releaseId: string) => api.promoteRelease(releaseId),
    onSuccess: (_accepted, releaseId) => {
      void queryClient.invalidateQueries({ queryKey: qk.release(releaseId) });
      void queryClient.invalidateQueries({ queryKey: ["releases"] });
    },
  });
}
