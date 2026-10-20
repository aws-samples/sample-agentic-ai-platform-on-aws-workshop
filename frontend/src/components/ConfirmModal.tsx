import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Modal from "@cloudscape-design/components/modal";
import SpaceBetween from "@cloudscape-design/components/space-between";
import type { ReactNode } from "react";

import { formatErrorContent } from "../layout/notifications";

export function ConfirmModal({
  visible,
  header,
  children,
  confirmText = "Confirm",
  confirmVariant = "primary",
  loading = false,
  error,
  disabled = false,
  onConfirm,
  onDismiss,
}: {
  visible: boolean;
  header: ReactNode;
  children?: ReactNode;
  confirmText?: string;
  confirmVariant?: "primary" | "normal";
  loading?: boolean;
  error?: unknown;
  disabled?: boolean;
  onConfirm: () => void;
  onDismiss: () => void;
}) {
  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header={header}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={loading}>
              Cancel
            </Button>
            <Button variant={confirmVariant} onClick={onConfirm} loading={loading} disabled={disabled} data-testid="confirm">
              {confirmText}
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="s">
        {children}
        {error ? (
          <Alert type="error" header="The request failed">
            {formatErrorContent(error)}
          </Alert>
        ) : null}
      </SpaceBetween>
    </Modal>
  );
}
