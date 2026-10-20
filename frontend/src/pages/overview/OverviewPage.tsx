import BarChart from "@cloudscape-design/components/bar-chart";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Container from "@cloudscape-design/components/container";
import ContentLayout from "@cloudscape-design/components/content-layout";
import FormField from "@cloudscape-design/components/form-field";
import Grid from "@cloudscape-design/components/grid";
import Header from "@cloudscape-design/components/header";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import Link from "@cloudscape-design/components/link";
import SegmentedControl from "@cloudscape-design/components/segmented-control";
import Select, { type SelectProps } from "@cloudscape-design/components/select";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Table from "@cloudscape-design/components/table";
import { useEffect, useMemo } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { useDomains, useFeedback, useMetrics, useProjects, useSpend } from "../../api/hooks";
import type { Env, Metrics, Spend } from "../../api/types";
import { useMe } from "../../auth/MeContext";
import {
  SERIES_COLORS,
  STATUS_COLORS,
  bucketCategories,
  categoryTickFormatter,
  chartPeriod,
  hasData,
  periodLabel as formatPeriod,
  rebucket,
  timeDomain,
  toCategories,
} from "../../components/charts";
import { EmptyState } from "../../components/EmptyState";
import { ErrorAlert } from "../../components/ErrorAlert";
import { InfoHeader } from "../../components/InfoPopover";
import { KpiTile } from "../../components/KpiTile";
import { SpendBar } from "../../components/SpendBar";
import { useBreadcrumbs } from "../../layout/breadcrumbs";
import {
  formatCompact,
  formatDateTime,
  formatMillis,
  formatNumber,
  formatPercent,
  formatUsd,
  toNumber,
} from "../../utils/format";

const RANGES = [
  { id: "1h", text: "1 h", hours: 1 },
  { id: "24h", text: "24 h", hours: 24 },
  { id: "7d", text: "7 d", hours: 168 },
] as const;

type RangeId = (typeof RANGES)[number]["id"];

function rangeHours(id: string | null): { id: RangeId; hours: number } {
  const range = RANGES.find((r) => r.id === id) ?? RANGES[1];
  return { id: range.id, hours: range.hours };
}

/** `?domain=all` means every domain; an absent parameter follows the selected project's domain. */
const ALL_DOMAINS = "all";

export function OverviewPage() {
  useBreadcrumbs([{ text: "Overview", href: "/" }]);
  const navigate = useNavigate();
  const me = useMe();
  const [params, setParams] = useSearchParams();
  const domains = useDomains();
  const projects = useProjects();

  const env: Env = params.get("env") === "prod" ? "prod" : "dev";
  const range = rangeHours(params.get("range"));
  const projectId = params.get("project") ?? "";
  const selectedProject = projects.data?.find((p) => p.projectId === projectId);
  const domainParam = params.get("domain");
  const domainId = domainParam === ALL_DOMAINS ? "" : (domainParam ?? selectedProject?.domainId ?? "");
  const canOpenProject = me.canAccessScreen("domains"); // approvers have no Domains and projects screen

  // Default to the first project once the list is known (hr-assistant in the workshop account).
  useEffect(() => {
    if (!projectId && projects.data?.length) {
      const sorted = [...projects.data].sort((a, b) => a.projectId.localeCompare(b.projectId));
      const next = new URLSearchParams(params);
      next.set("project", sorted[0].projectId);
      setParams(next, { replace: true });
    }
  }, [projectId, projects.data, params, setParams]);

  const update = (key: string, value: string | null) => {
    const next = new URLSearchParams(params);
    if (value) {
      next.set(key, value);
    } else {
      next.delete(key);
    }
    if (key === "domain" && value && value !== ALL_DOMAINS && selectedProject?.domainId !== value) {
      // a specific domain was chosen and the current project is not in it: jump to that domain's first project
      const first = projects.data?.filter((p) => p.domainId === value).sort((a, b) => a.projectId.localeCompare(b.projectId))[0];
      if (first) {
        next.set("project", first.projectId);
      }
    }
    setParams(next, { replace: true });
  };

  const projectOptions: SelectProps.Option[] = (projects.data ?? [])
    .filter((p) => !domainId || p.domainId === domainId)
    .sort((a, b) => a.projectId.localeCompare(b.projectId))
    .map((p) => ({ value: p.projectId, label: p.name || p.projectId, description: p.projectId }));
  const domainOptions: SelectProps.Option[] = [
    { value: ALL_DOMAINS, label: "All domains" },
    ...(domains.data ?? []).map((d) => ({ value: d.domainId, label: d.name || d.domainId, description: d.domainId })),
  ];

  const metrics = useMetrics(projectId || undefined, env, range.hours);
  const spend = useSpend(projectId || undefined, env);
  const feedback = useFeedback(projectId || undefined, { enabled: !!projectId });

  const filterBar = (
    <Container>
      <ColumnLayout columns={4}>
        <FormField label="Domain">
          <Select
            selectedOption={domainOptions.find((o) => o.value === (domainId || ALL_DOMAINS)) ?? domainOptions[0]}
            options={domainOptions}
            onChange={({ detail }) => update("domain", detail.selectedOption.value ?? ALL_DOMAINS)}
            statusType={domains.isLoading ? "loading" : domains.error ? "error" : "finished"}
            errorText="Domains could not be loaded"
            data-testid="overview-domain"
          />
        </FormField>
        <FormField label="Project">
          <Select
            selectedOption={projectOptions.find((o) => o.value === projectId) ?? null}
            options={projectOptions}
            placeholder={projects.isLoading ? "Loading projects…" : "Choose a project"}
            empty="No projects yet: create one in Domains and projects"
            onChange={({ detail }) => update("project", detail.selectedOption.value ?? null)}
            statusType={projects.isLoading ? "loading" : projects.error ? "error" : "finished"}
            errorText="Projects could not be loaded"
            data-testid="overview-project"
          />
        </FormField>
        <FormField label="Environment">
          <SegmentedControl
            selectedId={env}
            options={[
              { id: "dev", text: "Dev" },
              { id: "prod", text: "Prod" },
            ]}
            onChange={({ detail }) => update("env", detail.selectedId)}
          />
        </FormField>
        <FormField label="Range">
          <SegmentedControl
            selectedId={range.id}
            options={RANGES.map((r) => ({ id: r.id, text: r.text }))}
            onChange={({ detail }) => update("range", detail.selectedId)}
          />
        </FormField>
      </ColumnLayout>
    </Container>
  );

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description="Usage, cost against caps, policy decisions and quality per project and environment, read from CloudWatch and the model gateway."
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                iconName="refresh"
                onClick={() => {
                  void metrics.refetch();
                  void spend.refetch();
                  void feedback.refetch();
                }}
                loading={metrics.isFetching || spend.isFetching}
                disabled={!projectId}
                data-testid="overview-refresh"
              >
                Refresh
              </Button>
              {selectedProject && canOpenProject ? <Button onClick={() => navigate(`/projects/${selectedProject.projectId}`)}>Open project</Button> : null}
            </SpaceBetween>
          }
        >
          Overview
        </Header>
      }
    >
      <SpaceBetween size="l">
        {filterBar}
        {projects.error ? <ErrorAlert header="Projects could not be loaded" error={projects.error} onRetry={() => void projects.refetch()} /> : null}
        {!projects.isLoading && projects.data && projects.data.length === 0 ? (
          <Container>
            <EmptyState
              title="No projects yet"
              description="Create a domain and a project (POST /domains and POST /projects, or Domains and projects); the dashboard fills up as gateways, keys and harnesses are registered."
              action={canOpenProject ? <Button onClick={() => navigate("/domains")}>Go to Domains and projects</Button> : undefined}
            />
          </Container>
        ) : null}
        {projectId ? (
          <Dashboard
            projectId={projectId}
            env={env}
            hours={range.hours}
            metrics={metrics.data}
            metricsError={metrics.error}
            metricsLoading={metrics.isLoading}
            retryMetrics={() => void metrics.refetch()}
            spend={spend.data}
            spendError={spend.error}
            spendLoading={spend.isLoading}
            feedback={feedback.data}
            feedbackError={feedback.error}
          />
        ) : null}
      </SpaceBetween>
    </ContentLayout>
  );
}

interface DashboardProps {
  projectId: string;
  env: Env;
  hours: number;
  metrics: Metrics | undefined;
  metricsError: Error | null;
  metricsLoading: boolean;
  retryMetrics: () => void;
  spend: Spend | undefined;
  spendError: Error | null;
  spendLoading: boolean;
  feedback: ReturnType<typeof useFeedback>["data"];
  feedbackError: Error | null;
}

function Dashboard(props: DashboardProps) {
  const { projectId, env, hours, metrics, spend, feedback } = props;
  const navigate = useNavigate();
  const canOpenProject = useMe().canAccessScreen("domains");
  const series = metrics?.series ?? {};
  const totals = metrics?.totals ?? {};
  const invocations = toNumber(totals.invocations);
  const errors = toNumber(totals.errors);
  const errorRate = invocations && invocations > 0 ? (errors ?? 0) / invocations : null;
  const p90 = toNumber(totals.latencyP90Max);
  const envSpend = spend?.byEnv?.[env];
  const policyAllow = toNumber(totals.policyAllow);
  const policyDeny = toNumber(totals.policyDeny);
  const feedbackRows = feedback ?? [];
  const ups = feedbackRows.filter((f) => f.rating === "up").length;
  const downs = feedbackRows.filter((f) => f.rating === "down").length;

  const domain = useMemo(() => timeDomain(metrics?.window, hours), [metrics?.window, hours]);
  const chartStatus = props.metricsLoading ? "loading" : props.metricsError ? "error" : "finished";
  const barPeriod = chartPeriod(hours, metrics?.period ?? (hours <= 3 ? 60 : hours <= 24 ? 300 : 3600));
  const periodLabel = formatPeriod(barPeriod);
  const bars = (key: string, mode: "sum" | "max" = "sum") =>
    hasData(series[key]) ? toCategories(rebucket(series[key], domain, barPeriod, mode)) : [];
  const categories = useMemo(() => bucketCategories(domain, barPeriod), [domain, barPeriod]);
  // Errors are a subset of invocations: stack successes + errors so the bar height is the invocation count.
  const errorBars = bars("errors");
  const successfulBars = bars("invocations").map((point, index) => ({
    x: point.x,
    y: Math.max(0, point.y - (errorBars[index]?.x === point.x ? errorBars[index].y : 0)),
  }));
  const categoryTick = useMemo(() => categoryTickFormatter(hours), [hours]);

  const harnessMissing = !!metrics && !metrics.harness;
  const envLabel = env === "prod" ? "Prod" : "Dev";

  // Spend and tokens per key (LiteLLM): the keys of this environment; tokens from the last 50 spend-log rows.
  const keyRows = (spend?.keys ?? []).filter((k) => k.env === env);
  const tokensByKey = new Map<string, number>();
  for (const row of spend?.logs ?? []) {
    if (!row.keyAlias || !keyRows.some((k) => k.alias === row.keyAlias)) {
      continue;
    }
    tokensByKey.set(row.keyAlias, (tokensByKey.get(row.keyAlias) ?? 0) + (toNumber(row.totalTokens) ?? 0));
  }

  const kpis = (
    <Container header={<Header variant="h2">{`${projectId} · ${envLabel} · last ${hours <= 24 ? `${hours} h` : `${hours / 24} d`}`}</Header>}>
      {props.metricsError ? <ErrorAlert header="Metrics could not be loaded" error={props.metricsError} onRetry={props.retryMetrics} /> : null}
      <ColumnLayout columns={4} variant="text-grid">
        <KpiTile
          label="Invocations"
          loading={props.metricsLoading}
          value={harnessMissing ? "—" : formatCompact(invocations ?? 0)}
          hint={props.metricsLoading ? undefined : harnessMissing ? `No ${envLabel} harness registered yet` : `${formatNumber(totals.sessions ?? 0)} sessions`}
        />
        <KpiTile
          label="Error rate"
          loading={props.metricsLoading}
          value={errorRate === null ? "—" : formatPercent(errorRate)}
          hint={props.metricsLoading ? undefined : invocations ? `${formatNumber(errors ?? 0)} errors of ${formatNumber(invocations)} invocations` : "No invocations in the window"}
        />
        <KpiTile
          label="p90 latency"
          loading={props.metricsLoading}
          value={p90 === null ? "—" : formatMillis(p90)}
          hint={props.metricsLoading ? undefined : "Highest p90 bucket in the window"}
        />
        <KpiTile label={`Spend vs cap`} loading={props.spendLoading} value={envSpend ? formatUsd(envSpend.spend) : "—"}>
          {props.spendLoading ? null : envSpend ? (
            <SpendBar spend={envSpend.spend} cap={envSpend.maxBudget} blocked={envSpend.blocked > 0} />
          ) : (
            <Box variant="small" color="text-body-secondary" display="block">
              {props.spendError ? "Spend could not be loaded" : `No ${envLabel} key yet (POST /projects/{id}/keys or the deploy job creates the Dev key; the first promotion creates the Prod key)`}
            </Box>
          )}
          {spend?.team ? (
            <Box variant="small" color="text-body-secondary" display="block">
              Project cap: {formatUsd(spend.team.spend)} of {formatUsd(spend.team.maxBudget)} per {spend.team.budgetDuration ?? "period"}
            </Box>
          ) : null}
        </KpiTile>
      </ColumnLayout>
    </Container>
  );

  const invocationsChart = (
    <Container
      header={
        <InfoHeader help="Harness endpoint metrics from CloudWatch (AWS/Bedrock-AgentCore): successful and failed invocations" description={`per ${periodLabel}`}>
          Invocations and errors
        </InfoHeader>
      }
    >
      <BarChart
        statusType={chartStatus}
        height={220}
        hideFilter
        stackedBars
        xScaleType="categorical"
        xDomain={categories}
        xTitle="Time"
        yTitle="Invocations"
        ariaLabel="Successful and failed invocations per period"
        series={[
          { title: "Successful", type: "bar", data: successfulBars, color: SERIES_COLORS.slot1 },
          { title: "Errors", type: "bar", data: errorBars, color: STATUS_COLORS.critical },
        ]}
        i18nStrings={{ xTickFormatter: categoryTick }}
        loadingText="Loading metrics"
        errorText="Metrics could not be loaded"
        recoveryText="Retry"
        onRecoveryClick={props.retryMetrics}
        empty={
          <EmptyState
            title={harnessMissing ? `No ${envLabel} harness yet` : "No invocations in this window"}
            description={
              harnessMissing
                ? env === "dev"
                  ? "The deploy job (POST /projects/{id}/deploy, or a release run) creates the Dev harness; invocations appear here within a minute."
                  : "Promoting an approved release (POST /releases/{id}/promote) creates the Prod harness."
                : "Send a request from the Playground (POST /invoke), then widen the range."
            }
          />
        }
      />
    </Container>
  );

  const latencyChart = (
    <Container header={<InfoHeader help="Per invocation: highest p50 and p90" description={`per ${periodLabel}`}>Latency</InfoHeader>}>
      <BarChart
        statusType={chartStatus}
        height={220}
        hideFilter
        xScaleType="categorical"
        xDomain={categories}
        xTitle="Time"
        yTitle="Latency"
        ariaLabel="Latency p50 and p90 per period"
        series={[
          { title: "p50", type: "bar", data: bars("latencyP50", "max"), color: SERIES_COLORS.slot3, valueFormatter: (v) => formatMillis(v) },
          { title: "p90", type: "bar", data: bars("latencyP90", "max"), color: SERIES_COLORS.slot1, valueFormatter: (v) => formatMillis(v) },
        ]}
        i18nStrings={{ xTickFormatter: categoryTick, yTickFormatter: (v) => formatMillis(v) }}
        loadingText="Loading metrics"
        errorText="Metrics could not be loaded"
        recoveryText="Retry"
        onRecoveryClick={props.retryMetrics}
        empty={<EmptyState title="No latency samples" description="Latency appears with the first invocations of the window." />}
      />
    </Container>
  );

  const spendCharts = (
    <Container
      header={
        <InfoHeader help="LiteLLM spend of this environment's agent keys (current budget period) and tokens in the last 50 model calls">
          Spend and tokens per key
        </InfoHeader>
      }
    >
      {props.spendError ? <ErrorAlert header="Spend could not be loaded" error={props.spendError} /> : null}
      {keyRows.length === 0 ? (
        <EmptyState
          title={`No ${envLabel} keys`}
          description="Keys are created with POST /projects/{id}/keys or by the deploy job (Dev) and by the first promotion (Prod); they appear in the project's Keys tab."
        />
      ) : keyRows.length === 1 ? (
        <KeyValuePairs
          columns={3}
          items={[
            { label: "Key", value: keyRows[0].alias },
            { label: "Spend this period", value: `${formatUsd(keyRows[0].spend)} of ${formatUsd(keyRows[0].maxBudget)}` },
            { label: "Tokens (last 50 calls)", value: formatNumber(tokensByKey.get(keyRows[0].alias) ?? 0) },
          ]}
        />
      ) : (
        <ColumnLayout columns={2}>
          <BarChart
            height={200}
            hideFilter
            hideLegend
            xScaleType="categorical"
            yTitle="USD"
            ariaLabel="Spend per key"
            series={[
              {
                title: "Spend",
                type: "bar",
                data: keyRows.map((k) => ({ x: k.alias, y: toNumber(k.spend) ?? 0 })),
                color: SERIES_COLORS.slot1,
                valueFormatter: (v) => formatUsd(v),
              },
            ]}
            empty={<EmptyState title="No spend" />}
          />
          <BarChart
            height={200}
            hideFilter
            hideLegend
            xScaleType="categorical"
            yTitle="Tokens"
            ariaLabel="Tokens per key"
            series={[
              {
                title: "Tokens",
                type: "bar",
                data: keyRows.map((k) => ({ x: k.alias, y: tokensByKey.get(k.alias) ?? 0 })),
                color: SERIES_COLORS.slot1,
                valueFormatter: (v) => formatNumber(v),
              },
            ]}
            empty={<EmptyState title="No model calls logged" />}
          />
        </ColumnLayout>
      )}
    </Container>
  );

  const byTool = Object.entries(metrics?.policy?.byTool ?? {})
    .map(([tool, counts]) => ({ tool, allow: toNumber(counts.allow) ?? 0, deny: toNumber(counts.deny) ?? 0 }))
    .sort((a, b) => b.deny - a.deny || b.allow - a.allow || a.tool.localeCompare(b.tool));

  const policySection = (
    <Container
      header={
        <InfoHeader
          help="Policy engine decisions of the environment's gateway (requests)"
          description={metrics?.policy ? `Gateway ${metrics.policy.gatewayName ?? metrics.policy.gatewayId}` : undefined}
        >
          Policy decisions
        </InfoHeader>
      }
    >
      {metrics && !metrics.policy ? (
        <EmptyState
          title="No gateway registered for this environment"
          description="Register the environment's gateway with PUT /projects/{id}/resources (type gateway, with its policy engine); the allow / deny series appear here afterwards."
        />
      ) : (
        <SpaceBetween size="l">
          <ColumnLayout columns={3} variant="text-grid">
            <KpiTile label="Allowed requests" value={props.metricsLoading ? "…" : formatNumber(policyAllow ?? 0)} />
            <KpiTile label="Denied requests" value={props.metricsLoading ? "…" : formatNumber(policyDeny ?? 0)} />
            <KpiTile
              label="Deny rate"
              value={
                props.metricsLoading
                  ? "…"
                  : (policyAllow ?? 0) + (policyDeny ?? 0) > 0
                    ? formatPercent((policyDeny ?? 0) / ((policyAllow ?? 0) + (policyDeny ?? 0)))
                    : "—"
              }
            />
          </ColumnLayout>
          <BarChart
            statusType={chartStatus}
            height={200}
            hideFilter
            stackedBars
            xScaleType="categorical"
            xDomain={categories}
            xTitle="Time"
            yTitle="Requests"
            ariaLabel="Allowed and denied requests per period"
            series={[
              { title: "Allowed", type: "bar", data: bars("policyAllow"), color: STATUS_COLORS.positive },
              { title: "Denied", type: "bar", data: bars("policyDeny"), color: STATUS_COLORS.critical },
            ]}
            i18nStrings={{ xTickFormatter: categoryTick }}
            loadingText="Loading metrics"
            errorText="Metrics could not be loaded"
            recoveryText="Retry"
            onRecoveryClick={props.retryMetrics}
            empty={<EmptyState title="No policy decisions in this window" description="Tool calls through the gateway produce allow and deny decisions." />}
          />
          <Table
            variant="embedded"
            header={
              <Header
                variant="h3"
                counter={`(${byTool.length})`}
                description="Tool evaluations: a tools/list request evaluates every tool once, so these counts exceed the request totals above and must not be added to them."
              >
                Decisions per tool
              </Header>
            }
            columnDefinitions={[
              { id: "tool", header: "Tool", cell: (row) => row.tool, isRowHeader: true },
              { id: "allow", header: "Allow", cell: (row) => formatNumber(row.allow) },
              { id: "deny", header: "Deny", cell: (row) => formatNumber(row.deny) },
            ]}
            items={byTool}
            empty={<EmptyState title="No per-tool decisions" />}
          />
        </SpaceBetween>
      )}
    </Container>
  );

  const evaluators = Object.entries(metrics?.evaluation?.evaluators ?? {}).sort(([a], [b]) => a.localeCompare(b));
  // Mean per evaluator as bars: the service publishes one aggregate point per 5-minute bucket and workshop traffic lands in a
  // single bucket, so a line through one point was invisible (the same trap as the invocation charts); a bar always shows.
  const qualityBars = evaluators.map(([name, entry]) => ({ x: name, y: toNumber(entry.mean) ?? 0 }));
  const qualitySection = (
    <Container
      header={
        <InfoHeader help="Online evaluation of the Prod endpoint (Bedrock-AgentCore/Evaluations): mean score per evaluator in the window, 1 = good on every scale">
          Quality scores
        </InfoHeader>
      }
    >
      {evaluators.length === 0 ? (
        <EmptyState
          title="No online evaluation scores"
          description={
            env === "prod"
              ? "The first promotion creates the online evaluation config; scores appear a few minutes after Prod sessions go quiet."
              : "Online evaluation watches the Prod endpoint only; switch the environment to Prod once a release has been promoted."
          }
        />
      ) : (
        <SpaceBetween size="l">
          <BarChart
            statusType={chartStatus}
            height={200}
            hideFilter
            hideLegend
            xScaleType="categorical"
            xDomain={evaluators.map(([name]) => name)}
            yDomain={[0, 1]}
            xTitle="Evaluator"
            yTitle="Mean score"
            ariaLabel="Mean online-evaluation score per evaluator"
            series={[{ title: "Mean score", type: "bar", data: qualityBars, color: SERIES_COLORS.slot1, valueFormatter: (v) => v.toFixed(3) }]}
            empty={<EmptyState title="No samples in this window" />}
          />
          <Table
            variant="embedded"
            columnDefinitions={[
              { id: "evaluator", header: "Evaluator", cell: (row) => row.name, isRowHeader: true },
              { id: "mean", header: "Mean score", cell: (row) => (row.mean === null ? "—" : row.mean.toFixed(3)) },
              { id: "samples", header: "Samples", cell: (row) => formatNumber(row.samples) },
              {
                id: "labels",
                header: "Labels",
                cell: (row) =>
                  Object.entries(row.labels)
                    .map(([label, count]) => `${label}: ${formatNumber(count)}`)
                    .join(", ") || "—",
              },
            ]}
            items={evaluators.map(([name, entry]) => ({ name, mean: entry.mean, samples: entry.samples, labels: entry.labels }))}
          />
        </SpaceBetween>
      )}
    </Container>
  );

  const feedbackSection = (
    <Container
      header={
        <InfoHeader help="Thumbs up / down from the playground (POST /feedback), all environments of the project" counter={`(${feedbackRows.length})`}>
          Feedback
        </InfoHeader>
      }
    >
      {props.feedbackError ? <ErrorAlert header="Feedback could not be loaded" error={props.feedbackError} /> : null}
      <SpaceBetween size="m">
        <ColumnLayout columns={2} variant="text-grid">
          <KpiTile label="Thumbs up" value={formatNumber(ups)} />
          <KpiTile label="Thumbs down" value={formatNumber(downs)} />
        </ColumnLayout>
        <Table
          variant="embedded"
          columnDefinitions={[
            { id: "rating", header: "Rating", cell: (row) => (row.rating === "up" ? "Up" : "Down") },
            { id: "env", header: "Env", cell: (row) => row.env },
            { id: "comment", header: "Comment", cell: (row) => row.comment || "—" },
            { id: "by", header: "By", cell: (row) => row.createdBy },
            { id: "at", header: "At", cell: (row) => formatDateTime(row.createdAt) },
          ]}
          items={feedbackRows.slice(0, 10)}
          empty={<EmptyState title="No feedback yet" description="Rate an answer in the playground." />}
        />
      </SpaceBetween>
    </Container>
  );

  const anySeries = hasData(series.invocations, series.latencyP90, series.policyAllow, series.policyDeny);

  return (
    <SpaceBetween size="l">
      {kpis}
      <Grid gridDefinition={[{ colspan: { default: 12, m: 6 } }, { colspan: { default: 12, m: 6 } }]}>
        {invocationsChart}
        {latencyChart}
      </Grid>
      {spendCharts}
      {policySection}
      {qualitySection}
      {feedbackSection}
      {!anySeries && metrics ? (
        <Box variant="small" color="text-body-secondary">
          Window {formatDateTime(metrics.window?.start)} to {formatDateTime(metrics.window?.end)} (period {metrics.period} s). Nothing
          recorded yet for this selection
          {canOpenProject ? (
            <>
              ; the <Link onFollow={(e) => { e.preventDefault(); navigate(`/projects/${projectId}?tab=resources`); }} href={`/projects/${projectId}?tab=resources`}>Resources tab</Link> shows what is registered.
            </>
          ) : (
            "."
          )}
        </Box>
      ) : null}
    </SpaceBetween>
  );
}
