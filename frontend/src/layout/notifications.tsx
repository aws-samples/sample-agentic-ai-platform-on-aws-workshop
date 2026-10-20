import type { FlashbarProps } from "@cloudscape-design/components/flashbar";
import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from "react";

import { describeError } from "../api/client";

export interface NotifyOptions {
  id?: string;
  type?: FlashbarProps.Type;
  header?: ReactNode;
  content?: ReactNode;
  dismissible?: boolean;
  loading?: boolean;
  /** Milliseconds until the message disappears on its own (default 6 s for success, never for errors). */
  autoDismissMs?: number | null;
  action?: ReactNode;
}

export interface NotificationsValue {
  items: FlashbarProps.MessageDefinition[];
  notify: (options: NotifyOptions) => string;
  dismiss: (id: string) => void;
  success: (header: ReactNode, content?: ReactNode) => string;
  info: (header: ReactNode, content?: ReactNode) => string;
  warning: (header: ReactNode, content?: ReactNode) => string;
  /** Formats ApiError / Error as "CODE: message" under the given header. */
  error: (header: ReactNode, error?: unknown) => string;
}

const NotificationsContext = createContext<NotificationsValue | null>(null);

export function formatErrorContent(error: unknown): string {
  const { code, message, status } = describeError(error);
  return status ? `${code} (${status}): ${message}` : `${code}: ${message}`;
}

export function NotificationsProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<FlashbarProps.MessageDefinition[]>([]);
  const counter = useRef(0);
  const timers = useRef(new Map<string, ReturnType<typeof setTimeout>>());

  const dismiss = useCallback((id: string) => {
    const timer = timers.current.get(id);
    if (timer) {
      clearTimeout(timer);
      timers.current.delete(id);
    }
    setItems((current) => current.filter((item) => item.id !== id));
  }, []);

  const notify = useCallback(
    (options: NotifyOptions) => {
      counter.current += 1;
      const id = options.id ?? `n-${counter.current}`;
      const type = options.type ?? "info";
      const autoDismiss = options.autoDismissMs === undefined ? (type === "success" ? 6000 : null) : options.autoDismissMs;
      const item: FlashbarProps.MessageDefinition = {
        id,
        type,
        header: options.header,
        content: options.content,
        loading: options.loading,
        dismissible: options.dismissible ?? true,
        onDismiss: () => dismiss(id),
        action: options.action,
      };
      setItems((current) => [item, ...current.filter((existing) => existing.id !== id)].slice(0, 5));
      const existingTimer = timers.current.get(id);
      if (existingTimer) {
        clearTimeout(existingTimer);
        timers.current.delete(id);
      }
      if (autoDismiss) {
        timers.current.set(
          id,
          setTimeout(() => dismiss(id), autoDismiss),
        );
      }
      return id;
    },
    [dismiss],
  );

  const value = useMemo<NotificationsValue>(
    () => ({
      items,
      notify,
      dismiss,
      success: (header, content) => notify({ type: "success", header, content }),
      info: (header, content) => notify({ type: "info", header, content }),
      warning: (header, content) => notify({ type: "warning", header, content }),
      error: (header, error) => notify({ type: "error", header, content: error === undefined ? undefined : formatErrorContent(error) }),
    }),
    [items, notify, dismiss],
  );
  return <NotificationsContext.Provider value={value}>{children}</NotificationsContext.Provider>;
}

export function useNotifications(): NotificationsValue {
  const value = useContext(NotificationsContext);
  if (!value) {
    throw new Error("useNotifications must be used inside NotificationsProvider");
  }
  return value;
}
