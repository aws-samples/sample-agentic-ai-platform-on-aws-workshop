import Badge from "@cloudscape-design/components/badge";
import StatusIndicator, { type StatusIndicatorProps } from "@cloudscape-design/components/status-indicator";

import type { GatewayMode } from "../api/types";

type IndicatorType = StatusIndicatorProps.Type;

const RELEASE: Record<string, IndicatorType> = {
  DRAFT: "pending",
  EVALUATING: "in-progress",
  BLOCKED: "error",
  PENDING_APPROVAL: "pending",
  APPROVED: "success",
  REJECTED: "stopped",
  PROMOTING: "loading",
  PROMOTED: "success",
  FAILED: "error",
};

const RECORD: Record<string, IndicatorType> = {
  DRAFT: "pending",
  PENDING_APPROVAL: "pending",
  APPROVED: "success",
  REJECTED: "stopped",
  DEPRECATED: "warning",
  CREATING: "loading",
  UPDATING: "loading",
  CREATE_FAILED: "error",
  UPDATE_FAILED: "error",
};

const GENERIC: Record<string, IndicatorType> = {
  ACTIVE: "success",
  READY: "success",
  SUCCEEDED: "success",
  RUNNING: "in-progress",
  QUEUED: "pending",
  CREATING: "loading",
  UPDATING: "loading",
  DELETING: "loading",
  FAILED: "error",
  ERROR: "error",
  INVALID: "error",
  NOT_FOUND: "error",
  UNKNOWN: "stopped",
};

export function releaseStatusType(status: string | null | undefined): IndicatorType {
  return (status && RELEASE[status]) || "stopped";
}

export function recordStatusType(status: string | null | undefined): IndicatorType {
  return (status && RECORD[status]) || "stopped";
}

export function genericStatusType(status: string | null | undefined): IndicatorType {
  return (status && (GENERIC[status] ?? RELEASE[status] ?? RECORD[status])) || "stopped";
}

function humanize(status: string | null | undefined): string {
  return status ? status.replace(/_/g, " ").toLowerCase().replace(/^./, (c) => c.toUpperCase()) : "Unknown";
}

export function ReleaseStatus({ status }: { status: string | null | undefined }) {
  return <StatusIndicator type={releaseStatusType(status)}>{humanize(status)}</StatusIndicator>;
}

export function RecordStatus({ status }: { status: string | null | undefined }) {
  return <StatusIndicator type={recordStatusType(status)}>{humanize(status)}</StatusIndicator>;
}

export function GenericStatus({ status, label }: { status: string | null | undefined; label?: string }) {
  return <StatusIndicator type={genericStatusType(status)}>{label ?? humanize(status)}</StatusIndicator>;
}

export function KeyStatus({ status, blocked }: { status: string | null | undefined; blocked?: boolean | null }) {
  if (blocked && status === "active") {
    return <StatusIndicator type="warning">Active, blocked by LiteLLM (cap reached)</StatusIndicator>;
  }
  if (status === "suspended") {
    return <StatusIndicator type="stopped">Suspended</StatusIndicator>;
  }
  if (status === "active") {
    return <StatusIndicator type="success">Active</StatusIndicator>;
  }
  return <StatusIndicator type="stopped">{humanize(status)}</StatusIndicator>;
}

export function ModeBadge({ mode }: { mode: GatewayMode | string | null | undefined }) {
  if (mode === "ENFORCE") {
    return <Badge color="green">ENFORCE</Badge>;
  }
  if (mode === "LOG_ONLY") {
    return <Badge color="grey">LOG_ONLY</Badge>;
  }
  return <Badge color="grey">no policy engine</Badge>;
}

export function EnvBadge({ env }: { env: string | null | undefined }) {
  return <Badge color={env === "prod" ? "blue" : "grey"}>{env ?? "—"}</Badge>;
}

export function TypeBadge({ type }: { type: string | null | undefined }) {
  return <Badge color="grey">{type ?? "—"}</Badge>;
}
