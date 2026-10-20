import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { useApi } from "../ApiProvider";
import { JOB_POLL_CAP_MS, JOB_POLL_INTERVAL_MS } from "../client";
import { qk } from "../queryKeys";
import { TERMINAL_JOB_STATES, type Job, type JobError } from "../types";

export interface UseJobOptions {
  intervalMs?: number;
  maxMs?: number;
}

export function isTerminalJob(job: Job<unknown> | undefined | null): boolean {
  return !!job && TERMINAL_JOB_STATES.includes(job.status);
}

/** The job's error as an object (older jobs may carry a plain string). */
export function jobError(job: Job<unknown> | undefined | null): JobError | null {
  if (!job || !job.error) {
    return null;
  }
  if (typeof job.error === "string") {
    return { code: "JOB_FAILED", message: job.error };
  }
  return job.error;
}

/**
 * Poll GET /jobs/{id} every 2 s until SUCCEEDED or FAILED (deploy, promote, invoke). Polling stops after `maxMs`
 * (10 min) and `timedOut` becomes true; the caller shows "still running" and offers to keep waiting.
 */
export function useJob<TOutput = Record<string, unknown>>(jobId: string | undefined | null, options: UseJobOptions = {}) {
  const api = useApi();
  const intervalMs = options.intervalMs ?? JOB_POLL_INTERVAL_MS;
  const maxMs = options.maxMs ?? JOB_POLL_CAP_MS;
  const startedAt = useRef<number>(Date.now());
  const [timedOut, setTimedOut] = useState(false);

  useEffect(() => {
    startedAt.current = Date.now();
    setTimedOut(false);
  }, [jobId]);

  const query = useQuery({
    queryKey: qk.job(jobId ?? "none"),
    queryFn: ({ signal }) => api.getJob<TOutput>(jobId as string, signal),
    enabled: !!jobId,
    staleTime: 0,
    refetchInterval: (current) => {
      const job = current.state.data as Job<TOutput> | undefined;
      if (isTerminalJob(job)) {
        return false;
      }
      if (Date.now() - startedAt.current > maxMs) {
        setTimedOut(true);
        return false;
      }
      return intervalMs;
    },
  });

  const job = query.data;
  return {
    job,
    isTerminal: isTerminalJob(job),
    isRunning: !!jobId && !isTerminalJob(job) && !timedOut,
    timedOut,
    error: query.error,
    isLoading: query.isLoading,
    refetch: query.refetch,
    /** Reset the 10-minute cap and poll again. */
    keepWaiting: () => {
      startedAt.current = Date.now();
      setTimedOut(false);
      void query.refetch();
    },
  };
}
