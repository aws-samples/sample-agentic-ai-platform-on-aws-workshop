import Alert from "@cloudscape-design/components/alert";
import Button from "@cloudscape-design/components/button";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Container from "@cloudscape-design/components/container";
import Form from "@cloudscape-design/components/form";
import Header from "@cloudscape-design/components/header";
import Input from "@cloudscape-design/components/input";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { useEffect, useState } from "react";

import { isApiError } from "../../api/client";
import { usePutSettings, useSettings } from "../../api/hooks";
import type { Settings, SettingsUpdate } from "../../api/types";
import { useMe } from "../../auth/MeContext";
import { ErrorAlert } from "../../components/ErrorAlert";
import { InfoFormField, InfoHeader } from "../../components/InfoPopover";
import { formatErrorContent, useNotifications } from "../../layout/notifications";
import { formatDateTime } from "../../utils/format";
import { durationError, nestedErrorPath, numberError } from "../domains/validation";

type FieldKey =
  | "defaultLimits.maxIterations"
  | "defaultLimits.maxTokens"
  | "defaultLimits.timeoutSeconds"
  | "defaultProjectCap.maxBudget"
  | "defaultProjectCap.budgetDuration"
  | "defaultProjectCap.tpmLimit"
  | "defaultProjectCap.rpmLimit"
  | "defaultKeyCap.maxBudget"
  | "defaultKeyCap.budgetDuration";

type FormState = Record<FieldKey, string>;

const FIELDS: Array<{ key: FieldKey; label: string; description: string; kind: "integer" | "number" | "duration" }> = [
  { key: "defaultLimits.maxIterations", label: "Max iterations", description: "Agent loop iterations per invocation", kind: "integer" },
  { key: "defaultLimits.maxTokens", label: "Max tokens", description: "Total output tokens per invocation", kind: "integer" },
  { key: "defaultLimits.timeoutSeconds", label: "Timeout (s)", description: "Harness invocation timeout", kind: "integer" },
  { key: "defaultProjectCap.maxBudget", label: "Project cap (USD)", description: "LiteLLM team max_budget", kind: "number" },
  { key: "defaultProjectCap.budgetDuration", label: "Project budget period", description: "7d, 24h, 1mo", kind: "duration" },
  { key: "defaultProjectCap.tpmLimit", label: "Tokens per minute", description: "LiteLLM team tpm_limit", kind: "integer" },
  { key: "defaultProjectCap.rpmLimit", label: "Requests per minute", description: "LiteLLM team rpm_limit", kind: "integer" },
  { key: "defaultKeyCap.maxBudget", label: "Key cap (USD)", description: "LiteLLM key max_budget per agent, env and model", kind: "number" },
  { key: "defaultKeyCap.budgetDuration", label: "Key budget period", description: "How often does budget reset", kind: "duration" },
];

function toForm(settings: Settings): FormState {
  return {
    "defaultLimits.maxIterations": String(settings.defaultLimits.maxIterations),
    "defaultLimits.maxTokens": String(settings.defaultLimits.maxTokens),
    "defaultLimits.timeoutSeconds": String(settings.defaultLimits.timeoutSeconds),
    "defaultProjectCap.maxBudget": String(settings.defaultProjectCap.maxBudget),
    "defaultProjectCap.budgetDuration": settings.defaultProjectCap.budgetDuration,
    "defaultProjectCap.tpmLimit": String(settings.defaultProjectCap.tpmLimit),
    "defaultProjectCap.rpmLimit": String(settings.defaultProjectCap.rpmLimit),
    "defaultKeyCap.maxBudget": String(settings.defaultKeyCap.maxBudget),
    "defaultKeyCap.budgetDuration": settings.defaultKeyCap.budgetDuration,
  };
}

function localError(kind: "integer" | "number" | "duration", value: string): string | undefined {
  if (kind === "duration") {
    return durationError(value);
  }
  return numberError(value, { minimum: kind === "integer" ? 1 : 0, integer: kind === "integer" });
}

/** Default limits and caps (PUT /settings, admin); nested validation errors from the API land on the matching field. */
export function SettingsForm() {
  const me = useMe();
  const settings = useSettings();
  const save = usePutSettings();
  const notifications = useNotifications();
  const [form, setForm] = useState<FormState | null>(null);
  const [touched, setTouched] = useState(false);
  const [serverErrors, setServerErrors] = useState<Partial<Record<FieldKey, string>>>({});
  const canWrite = me.can("settings:write");

  useEffect(() => {
    if (settings.data && !form) {
      setForm(toForm(settings.data));
    }
  }, [settings.data, form]);

  if (settings.error) {
    return <ErrorAlert header="Settings could not be loaded" error={settings.error} onRetry={() => void settings.refetch()} />;
  }
  if (!form || !settings.data) {
    return <Container header={<Header variant="h2">Default limits and caps</Header>}>Loading settings…</Container>;
  }
  const errors = Object.fromEntries(FIELDS.map((f) => [f.key, serverErrors[f.key] ?? localError(f.kind, form[f.key])])) as Partial<Record<FieldKey, string>>;
  const hasLocalErrors = FIELDS.some((f) => localError(f.kind, form[f.key]));
  const dirty = JSON.stringify(form) !== JSON.stringify(toForm(settings.data));

  const submit = () => {
    setTouched(true);
    setServerErrors({});
    if (hasLocalErrors) {
      return;
    }
    const update: SettingsUpdate = {
      defaultLimits: {
        maxIterations: Number(form["defaultLimits.maxIterations"]),
        maxTokens: Number(form["defaultLimits.maxTokens"]),
        timeoutSeconds: Number(form["defaultLimits.timeoutSeconds"]),
      },
      defaultProjectCap: {
        maxBudget: Number(form["defaultProjectCap.maxBudget"]),
        budgetDuration: form["defaultProjectCap.budgetDuration"].trim(),
        tpmLimit: Number(form["defaultProjectCap.tpmLimit"]),
        rpmLimit: Number(form["defaultProjectCap.rpmLimit"]),
      },
      defaultKeyCap: {
        maxBudget: Number(form["defaultKeyCap.maxBudget"]),
        budgetDuration: form["defaultKeyCap.budgetDuration"].trim(),
      },
    };
    save.mutate(update, {
      onSuccess: (saved) => {
        setForm(toForm(saved));
        setTouched(false);
        notifications.success("Default limits and caps saved", "New projects and keys use these defaults; existing teams and keys keep their caps.");
      },
      onError: (error) => {
        const path = isApiError(error) ? nestedErrorPath(error.message) : null;
        if (path) {
          setServerErrors({ [`${path.section}.${path.field}` as FieldKey]: error.message });
        }
      },
    });
  };

  const sections: Array<{ title: string; prefix: string }> = [
    { title: "Agent limits (blueprint rendering)", prefix: "defaultLimits." },
    { title: "Project cap (LiteLLM team)", prefix: "defaultProjectCap." },
    { title: "LiteLLM key cap", prefix: "defaultKeyCap." },
  ];

  return (
    <Container
      header={
        <InfoHeader
          help="Defaults applied when a project or key is created without explicit values."
          description={`Last change ${
            settings.data.updatedAt ? `${formatDateTime(settings.data.updatedAt)} by ${settings.data.updatedBy ?? "—"}` : "never (built-in defaults)"
          }.`}
          actions={
            canWrite ? (
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  disabled={!dirty || save.isPending}
                  onClick={() => {
                    setForm(toForm(settings.data as Settings));
                    setServerErrors({});
                    setTouched(false);
                  }}
                >
                  Reset
                </Button>
                <Button variant="primary" onClick={submit} loading={save.isPending} disabled={!dirty} data-testid="save-settings">
                  Save
                </Button>
              </SpaceBetween>
            ) : undefined
          }
        >
          Default limits and caps
        </InfoHeader>
      }
    >
      <Form errorText={save.error && !Object.keys(serverErrors).length ? formatErrorContent(save.error) : undefined}>
        <SpaceBetween size="l">
          {!canWrite ? <Alert type="info">Only platform admins can change the defaults; the values are shown read-only.</Alert> : null}
          {sections.map((section) => (
            <div key={section.prefix}>
              <Header variant="h3">{section.title}</Header>
              <ColumnLayout columns={4}>
                {FIELDS.filter((f) => f.key.startsWith(section.prefix)).map((f) => (
                  <InfoFormField key={f.key} label={f.label} help={f.description} errorText={touched || serverErrors[f.key] ? errors[f.key] : undefined}>
                    <Input
                      value={form[f.key]}
                      disabled={!canWrite}
                      data-testid={`setting-${f.key}`}
                      type={f.kind === "duration" ? "text" : "number"}
                      inputMode={f.kind === "duration" ? "text" : "decimal"}
                      onChange={({ detail }) => {
                        setForm({ ...form, [f.key]: detail.value });
                        if (serverErrors[f.key]) {
                          setServerErrors((current) => {
                            const next = { ...current };
                            delete next[f.key];
                            return next;
                          });
                        }
                      }}
                    />
                  </InfoFormField>
                ))}
              </ColumnLayout>
            </div>
          ))}
        </SpaceBetween>
      </Form>
    </Container>
  );
}
