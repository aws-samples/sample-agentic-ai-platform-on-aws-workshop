import Alert from "@cloudscape-design/components/alert";
import Button from "@cloudscape-design/components/button";
import type { ReactNode } from "react";

import { describeError } from "../api/client";

export function ErrorAlert({
  header,
  error,
  onRetry,
  children,
}: {
  header?: ReactNode;
  error: unknown;
  onRetry?: () => void;
  children?: ReactNode;
}) {
  const { code, message, status } = describeError(error);
  let hint: string | null = null;
  if (status === 403) {
    hint = "The API refused this action for your role (the UI mirrors the backend's authorization table).";
  } else if (status === 401) {
    hint = "Your session has expired: sign in again.";
  } else if (code === "TIMEOUT" || code === "NETWORK_ERROR") {
    hint = "The API did not answer. Check the connection and retry.";
  }
  return (
    <Alert
      type="error"
      header={header ?? "Request failed"}
      action={onRetry ? <Button onClick={onRetry}>Retry</Button> : undefined}
    >
      <div>
        <strong>{code}</strong>
        {status ? ` (${status})` : ""}: {message}
      </div>
      {hint ? <div>{hint}</div> : null}
      {children}
    </Alert>
  );
}
