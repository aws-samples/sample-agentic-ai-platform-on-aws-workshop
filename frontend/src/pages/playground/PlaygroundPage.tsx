import { Avatar, ChatBubble, SupportPromptGroup } from "@cloudscape-design/chat-components";
import Alert from "@cloudscape-design/components/alert";
import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import ButtonGroup from "@cloudscape-design/components/button-group";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Container from "@cloudscape-design/components/container";
import ContentLayout from "@cloudscape-design/components/content-layout";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import Modal from "@cloudscape-design/components/modal";
import PromptInput from "@cloudscape-design/components/prompt-input";
import SegmentedControl from "@cloudscape-design/components/segmented-control";
import Select, { type SelectProps } from "@cloudscape-design/components/select";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Textarea from "@cloudscape-design/components/textarea";
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import "./playground.css";

import { describeError } from "../../api/client";
import { jobError, useInvoke, useJob, useProjects, useResources, useSendFeedback } from "../../api/hooks";
import type { Env, FeedbackRating, InvokeOutput, JobError } from "../../api/types";
import { useMe } from "../../auth/MeContext";
import { CopyText } from "../../components/CopyText";
import { EmptyState } from "../../components/EmptyState";
import { ErrorAlert } from "../../components/ErrorAlert";
import { InfoFormField } from "../../components/InfoPopover";
import { useBreadcrumbs } from "../../layout/breadcrumbs";
import { formatErrorContent, useNotifications } from "../../layout/notifications";
import { formatDateTime, formatNumber, formatRelative, formatSeconds, formatUsd } from "../../utils/format";
import { AnswerMarkdown } from "./AnswerMarkdown";
import { SUGGESTED_PROMPTS, budgetDetails, expectedSeconds, harnessByEnv, newSessionId, usageSummary, type Turn } from "./playgroundHelpers";

const EMPTY_HINTS: Record<Env, string> = {
  dev: "The deploy job (POST /projects/{id}/deploy, or a release run) puts a blueprint version on Dev; the harness appears here as soon as the job registers it.",
  prod: "Promoting an approved release (POST /releases/{id}/promote) creates the Prod harness; it and its live endpoint appear here afterwards.",
};

export function PlaygroundPage() {
  useBreadcrumbs([{ text: "Playground", href: "/playground" }]);
  const me = useMe();
  const navigate = useNavigate();
  const notifications = useNotifications();
  const [params, setParams] = useSearchParams();
  const projects = useProjects();
  const projectId = params.get("project") ?? "";
  const env: Env = params.get("env") === "prod" ? "prod" : "dev";
  const resources = useResources(projectId || undefined);
  const harnesses = useMemo(() => harnessByEnv(resources.data), [resources.data]);
  const harness = harnesses[env];

  const [sessionId, setSessionId] = useState(() => newSessionId());
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [activeTurnId, setActiveTurnId] = useState<string | null>(null);
  const [feedbackFor, setFeedbackFor] = useState<{ turn: Turn; rating: FeedbackRating } | null>(null);
  const invoke = useInvoke();
  const transcriptEnd = useRef<HTMLDivElement>(null);

  // Default to the first project once the list is known.
  useEffect(() => {
    if (!projectId && projects.data?.length) {
      const sorted = [...projects.data].sort((a, b) => a.projectId.localeCompare(b.projectId));
      const next = new URLSearchParams(params);
      next.set("project", sorted[0].projectId);
      setParams(next, { replace: true });
    }
  }, [projectId, projects.data, params, setParams]);

  const activeTurn = turns.find((t) => t.id === activeTurnId) ?? null;
  const job = useJob<InvokeOutput>(activeTurn?.jobId ?? null);
  // Fold the finished job into its turn.
  useEffect(() => {
    if (!activeTurn || !job.isTerminal || !job.job) {
      return;
    }
    const finished = job.job;
    setTurns((current) =>
      current.map((t) =>
        t.id === activeTurn.id
          ? {
              ...t,
              status: finished.status === "SUCCEEDED" ? "done" : "failed",
              output: finished.status === "SUCCEEDED" ? (finished.output as InvokeOutput) : null,
              error: finished.status === "FAILED" ? jobError(finished) : null,
            }
          : t,
      ),
    );
    setActiveTurnId(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- react to the terminal transition only
  }, [job.isTerminal, job.job?.status]);
  useEffect(() => {
    if (activeTurn && job.job?.status === "RUNNING" && activeTurn.status === "queued") {
      setTurns((current) => current.map((t) => (t.id === activeTurn.id ? { ...t, status: "running" } : t)));
    }
  }, [job.job?.status, activeTurn]);
  useEffect(() => {
    transcriptEnd.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [turns.length, activeTurn?.status]);

  const busy = !!activeTurn || invoke.isPending;
  const startSession = (reason?: string) => {
    setSessionId(newSessionId());
    setTurns([]);
    setActiveTurnId(null);
    invoke.reset();
    if (reason) {
      notifications.info("New session", reason);
    }
  };
  // One conversation per project and environment: whenever the pair changes - through the Select / SegmentedControl, the
  // side-navigation link (drops the params), the back button or a deep link - the transcript and session id are discarded,
  // so Prod answers never stay on screen while the next message goes to the Dev harness with the Prod session id.
  const conversationKey = `${projectId}|${env}`;
  const previousKey = useRef(conversationKey);
  useEffect(() => {
    if (previousKey.current !== conversationKey) {
      previousKey.current = conversationKey;
      startSession();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- react to the project / environment change only
  }, [conversationKey]);
  const update = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    next.set(key, value);
    setParams(next, { replace: true });
  };

  const send = (text: string) => {
    const prompt = text.trim();
    if (!prompt || busy || !projectId) {
      return;
    }
    const turn: Turn = { id: `${Date.now()}-${turns.length}`, prompt, status: "queued", startedAt: Date.now(), jobId: null, output: null, error: null, feedback: null };
    setTurns((current) => [...current, turn]);
    setActiveTurnId(turn.id);
    setDraft("");
    invoke.mutate(
      { projectId, env, prompt, sessionId },
      {
        onSuccess: (accepted) => {
          if (accepted.sessionId && accepted.sessionId !== sessionId) {
            setSessionId(accepted.sessionId);
          }
          setTurns((current) => current.map((t) => (t.id === turn.id ? { ...t, jobId: accepted.jobId, status: "running" } : t)));
        },
        onError: (error) => {
          const described = describeError(error);
          setTurns((current) => current.map((t) => (t.id === turn.id ? { ...t, status: "failed", error: { code: described.code, message: described.message } } : t)));
          setActiveTurnId(null);
        },
      },
    );
  };

  const projectOptions: SelectProps.Option[] = [...(projects.data ?? [])]
    .sort((a, b) => a.projectId.localeCompare(b.projectId))
    .map((p) => ({ value: p.projectId, label: p.name || p.projectId, description: p.projectId }));
  const envLabel = env === "prod" ? "Prod" : "Dev";
  const username = me.me?.username ?? "you";
  const canOpenProject = me.canAccessScreen("domains"); // approvers have no project detail screen

  const controls = (
    <Container>
      <ColumnLayout columns={3}>
        <FormField label="Project">
          <Select
            selectedOption={projectOptions.find((o) => o.value === projectId) ?? null}
            options={projectOptions}
            placeholder={projects.isLoading ? "Loading projects…" : "Choose a project"}
            empty="No projects yet"
            statusType={projects.isLoading ? "loading" : projects.error ? "error" : "finished"}
            onChange={({ detail }) => detail.selectedOption.value && update("project", detail.selectedOption.value)}
            data-testid="playground-project"
          />
        </FormField>
        <FormField label="Environment" description={harness ? `${harness.name} · version ${harness.version ?? "?"} (blueprint v${harness.blueprintVersion ?? "?"}) · endpoint ${harness.qualifier}` : resources.isLoading ? "Checking the registered harness…" : `No ${envLabel} harness registered`}>
          <SegmentedControl
            selectedId={env}
            options={[
              { id: "dev", text: harnesses.dev ? "Dev" : "Dev (no harness)" },
              { id: "prod", text: harnesses.prod ? "Prod" : "Prod (no harness)" },
            ]}
            onChange={({ detail }) => update("env", detail.selectedId)}
          />
        </FormField>
        <InfoFormField label="Session" help="One AgentCore runtime session per conversation; memory and context stay within it">
          <SpaceBetween direction="horizontal" size="xs" alignItems="center">
            <span data-testid="session-id">
              <CopyText value={sessionId} max={40} />
            </span>
            <Button onClick={() => startSession(`Session ${sessionId.slice(0, 8)}… closed; the next turn starts a fresh microVM (25-35 s).`)} disabled={busy} iconName="refresh" data-testid="new-session">
              New session
            </Button>
          </SpaceBetween>
        </InfoFormField>
      </ColumnLayout>
    </Container>
  );

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description="Chat with a project's Dev or Prod harness as an employee. POST /invoke queues an InvokeHarness job; the answer arrives when GET /jobs/{id} reports SUCCEEDED."
          actions={projectId ? <Button onClick={() => navigate(`/?project=${encodeURIComponent(projectId)}&env=${env}&range=1h`)}>Open in Overview</Button> : undefined}
        >
          Playground
        </Header>
      }
    >
      <SpaceBetween size="l">
        {controls}
        {projects.error ? <ErrorAlert header="Projects could not be loaded" error={projects.error} onRetry={() => void projects.refetch()} /> : null}
        {resources.error ? <ErrorAlert header="The project's resources could not be loaded" error={resources.error} onRetry={() => void resources.refetch()} /> : null}
        {projectId && !resources.isLoading && !harness ? (
          <Container>
            <EmptyState
              title={`${projectId} has no ${envLabel} harness yet`}
              description={EMPTY_HINTS[env]}
              action={
                <SpaceBetween direction="horizontal" size="xs">
                  {canOpenProject ? <Button onClick={() => navigate(`/projects/${projectId}?tab=resources`)}>Open the project's resources</Button> : null}
                  {env === "prod" ? <Button onClick={() => navigate(`/releases?projectId=${encodeURIComponent(projectId)}`)}>Open releases</Button> : null}
                </SpaceBetween>
              }
            />
          </Container>
        ) : null}
        {projectId && harness ? (
          <Container
            header={
              <Header
                variant="h2"
                description={`${projectId} · ${envLabel} · ${harness.name} (${harness.qualifier})${harness.model ? ` · model ${harness.model} through the model gateway` : ""}`}
              >
                Conversation
              </Header>
            }
            footer={
              <SpaceBetween size="xs">
                <PromptInput
                  value={draft}
                  onChange={({ detail }) => setDraft(detail.value)}
                  onAction={({ detail }) => send(detail.value)}
                  actionButtonIconName="send"
                  actionButtonAriaLabel="Send"
                  disableActionButton={busy || !draft.trim()}
                  disabled={busy}
                  placeholder={busy ? "Waiting for the answer…" : "Ask as an employee, for example: What is my leave balance? My employee id is E1001."}
                  ariaLabel="Message to the agent"
                  maxRows={4}
                  data-testid="prompt-input"
                />
                <Box variant="small" color="text-body-secondary">
                  {turns.length === 0
                    ? "The first turn of a session takes 25-35 s (a fresh microVM starts and the agent loads its tools); later turns answer in about 4 s."
                    : `Turn ${turns.length + 1} of this session: expect ${expectedSeconds(turns.length)}.`}
                </Box>
              </SpaceBetween>
            }
          >
            <SpaceBetween size="m">
              {turns.length === 0 ? (
                <SupportPromptGroup
                  ariaLabel="Suggested prompts"
                  alignment="horizontal"
                  items={SUGGESTED_PROMPTS.map((p) => ({ id: p.id, text: p.text }))}
                  onItemClick={({ detail }) => {
                    const prompt = SUGGESTED_PROMPTS.find((p) => p.id === detail.id);
                    if (prompt) {
                      send(prompt.text);
                    }
                  }}
                />
              ) : null}
              {turns.map((turn, index) => (
                <TurnView
                  key={turn.id}
                  turn={turn}
                  index={index}
                  username={username}
                  projectId={projectId}
                  canFeedback={me.can("feedback")}
                  canOpenProject={canOpenProject}
                  onFeedback={(rating) => setFeedbackFor({ turn, rating })}
                />
              ))}
              {activeTurn && job.timedOut ? (
                <Alert type="warning" header="Still waiting after 10 minutes" action={<Button onClick={job.keepWaiting}>Keep waiting</Button>}>
                  The invoke job has not finished; the harness timeout is far shorter, so something is wrong. Check the job in CloudWatch or start a new session.
                </Alert>
              ) : null}
              {activeTurn && job.error ? <ErrorAlert header="The job could not be read" error={job.error} onRetry={() => void job.refetch()} /> : null}
              <div ref={transcriptEnd} />
            </SpaceBetween>
          </Container>
        ) : null}
      </SpaceBetween>
      {feedbackFor ? (
        <FeedbackModal
          projectId={projectId}
          env={env}
          sessionId={sessionId}
          turn={feedbackFor.turn}
          rating={feedbackFor.rating}
          onDismiss={() => setFeedbackFor(null)}
          onSent={(rating) => {
            setTurns((current) => current.map((t) => (t.id === feedbackFor.turn.id ? { ...t, feedback: rating } : t)));
            setFeedbackFor(null);
          }}
        />
      ) : null}
    </ContentLayout>
  );
}

function TurnView({
  turn,
  index,
  username,
  projectId,
  canFeedback,
  canOpenProject,
  onFeedback,
}: {
  turn: Turn;
  index: number;
  username: string;
  projectId: string;
  canFeedback: boolean;
  canOpenProject: boolean;
  onFeedback: (rating: FeedbackRating) => void;
}) {
  const navigate = useNavigate();
  const running = turn.status === "queued" || turn.status === "running";
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) {
      return;
    }
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [running]);
  const elapsed = Math.max(0, Math.round((now - turn.startedAt) / 1000));
  const usage = usageSummary(turn.output?.usage);
  const budget = budgetDetails(turn.error);
  const initials = username.slice(0, 2).toUpperCase();

  return (
    <SpaceBetween size="xs">
      <ChatBubble type="outgoing" ariaLabel={`${username} at turn ${index + 1}`} avatar={<Avatar ariaLabel={username} tooltipText={username} initials={initials} />}>
        <span style={{ whiteSpace: "pre-wrap" }}>{turn.prompt}</span>
      </ChatBubble>
      <ChatBubble
        type="incoming"
        ariaLabel={`Agent answer ${index + 1}`}
        showLoadingBar={running}
        avatar={<Avatar color="gen-ai" iconName="gen-ai" ariaLabel="HR assistant" tooltipText="HR assistant" loading={running} />}
        actions={
          turn.status === "done" && canFeedback ? (
            <ButtonGroup
              variant="icon"
              ariaLabel="Rate this answer"
              onItemClick={({ detail }) => onFeedback(detail.id === "up" ? "up" : "down")}
              items={[
                { type: "icon-toggle-button", id: "up", text: "Helpful", iconName: "thumbs-up", pressedIconName: "thumbs-up-filled", pressed: turn.feedback === "up", disabled: turn.feedback !== null && turn.feedback !== "up" },
                { type: "icon-toggle-button", id: "down", text: "Not helpful", iconName: "thumbs-down", pressedIconName: "thumbs-down-filled", pressed: turn.feedback === "down", disabled: turn.feedback !== null && turn.feedback !== "down" },
              ]}
            />
          ) : undefined
        }
      >
        {running ? (
          <SpaceBetween size="xxs">
            <Box color="text-body-secondary">
              {turn.status === "queued" ? "Queued…" : "Thinking…"} {formatSeconds(elapsed)} elapsed; expect {expectedSeconds(index)}.
            </Box>
            {turn.jobId ? (
              <Box variant="small" color="text-body-secondary">
                job {turn.jobId}
              </Box>
            ) : null}
          </SpaceBetween>
        ) : turn.status === "failed" ? (
          budget ? (
            <Alert
              type="error"
              header="Spending cap reached: the model gateway refused the call"
              action={canOpenProject ? <Button onClick={() => navigate(`/projects/${projectId}?tab=keys`)}>Open the project's keys</Button> : undefined}
            >
              <SpaceBetween size="xxs">
                <Box>
                  Key <code>{budget.alias ?? "?"}</code> has spent {formatUsd(budget.spend ?? budget.currentCost)} of its {formatUsd(budget.maxBudget)} cap
                  {budget.budgetResetAt ? ` · resets ${formatRelative(budget.budgetResetAt)} (${formatDateTime(budget.budgetResetAt)})` : ""}.
                </Box>
                <Box variant="small">A platform admin raises the cap (Set cap on the project's Keys tab); the model gateway unblocks the key immediately. {turn.error?.message}</Box>
              </SpaceBetween>
            </Alert>
          ) : (
            <Alert type="error" header={turn.error?.code === "HARNESS_NOT_DEPLOYED" ? "No harness for this environment" : `The turn failed: ${turn.error?.code ?? "ERROR"}`}>
              {turn.error ? formatErrorContent(toError(turn.error)) : "unknown error"}
            </Alert>
          )
        ) : (
          <SpaceBetween size="s">
            <div className="playground-markdown" data-testid="answer-text">
              {/* markdown without images or raw HTML (AnswerMarkdown): model output cannot inject markup or trigger image fetches */}
              <AnswerMarkdown>{turn.output?.text?.trim() || "(empty answer)"}</AnswerMarkdown>
            </div>
            <SpaceBetween direction="horizontal" size="xs" alignItems="center">
              <Box variant="small" color="text-body-secondary">
                Tools:
              </Box>
              {turn.output?.toolsUsed?.length ? (
                turn.output.toolsUsed.map((tool, i) => (
                  <Badge key={`${tool}-${i}`} color="blue">
                    {tool}
                  </Badge>
                ))
              ) : (
                <Box variant="small" color="text-body-secondary">
                  none
                </Box>
              )}
            </SpaceBetween>
            <Box variant="small" color="text-body-secondary" data-testid="turn-usage">
              {formatSeconds(turn.output?.seconds ?? elapsed)} · {usage.total !== null ? `${formatNumber(usage.total)} tokens (${formatNumber(usage.input ?? 0)} in, ${formatNumber(usage.output ?? 0)} out)` : "usage not reported"}
              {turn.output?.modelCalls ? ` · ${turn.output.modelCalls} model call${turn.output.modelCalls === 1 ? "" : "s"}` : ""}
              {turn.output?.stopReason ? ` · stop ${turn.output.stopReason}` : ""}
              {turn.feedback ? (
                <>
                  {" · "}
                  <StatusIndicator type="success">rated {turn.feedback}</StatusIndicator>
                </>
              ) : null}
            </Box>
          </SpaceBetween>
        )}
      </ChatBubble>
    </SpaceBetween>
  );
}

function toError(error: JobError): Error {
  const e = new Error(error.message);
  e.name = error.code;
  return e;
}

function FeedbackModal({
  projectId,
  env,
  sessionId,
  turn,
  rating,
  onDismiss,
  onSent,
}: {
  projectId: string;
  env: Env;
  sessionId: string;
  turn: Turn;
  rating: FeedbackRating;
  onDismiss: () => void;
  onSent: (rating: FeedbackRating) => void;
}) {
  const send = useSendFeedback();
  const notifications = useNotifications();
  const [comment, setComment] = useState("");
  const submit = () => {
    send.mutate(
      { projectId, env, sessionId, rating, comment: comment.trim() },
      {
        onSuccess: () => {
          notifications.success(`Feedback recorded: thumbs ${rating}`, "It counts on the project's Overview (Feedback section).");
          onSent(rating);
        },
      },
    );
  };
  return (
    <Modal
      visible
      onDismiss={onDismiss}
      header={rating === "up" ? "Rate this answer: helpful" : "Rate this answer: not helpful"}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={send.isPending}>
              Cancel
            </Button>
            <Button variant="primary" onClick={submit} loading={send.isPending} data-testid="feedback-submit">
              Send feedback
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        <Box variant="small" color="text-body-secondary">
          POST /feedback {"{"}projectId {projectId}, env {env}, sessionId {sessionId.slice(0, 12)}…, rating {rating}{"}"} for the answer to "{turn.prompt.slice(0, 80)}
          {turn.prompt.length > 80 ? "…" : ""}".
        </Box>
        <InfoFormField label="Comment" help="What was good or wrong" constraintText="Optional">
          <Textarea value={comment} onChange={({ detail }) => setComment(detail.value)} rows={3} data-testid="feedback-comment" />
        </InfoFormField>
        {send.error ? (
          <Alert type="error" header="Feedback was not recorded">
            {formatErrorContent(send.error)}
          </Alert>
        ) : null}
      </SpaceBetween>
    </Modal>
  );
}
