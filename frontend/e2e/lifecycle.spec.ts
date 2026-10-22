/**
 * Phase 2B exit criterion: the whole release lifecycle clicked through the real UI (Cognito Hosted UI + platform API).
 * State: `AWS_PROFILE=workshop uv run python scripts/lifecycle_test.py --keep` (project lt-hr-assistant with harnesses, releases,
 * metrics, spend, feedback); afterwards `--only-cleanup`. The hr-assistant / invoice-helper workshop state is only read.
 *
 *   admin      lists, project tabs (key cap +0.5 and back, render preview), governance (gateway mode toggled and restored,
 *              settings round trip), catalogue, Overview numbers equal GET /metrics and GET /spend, keyboard + axe checks
 *   developer  creates a release from the latest blueprint version in the UI (the platform starts its run: EVALUATING), posts a
 *              passing evaluation through the API instead of waiting for the run, chats twice in one Playground session and
 *              sends a thumbs up
 *   approver   approves and promotes that release, watches the job to PROMOTED, sees the version history
 */
import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { join } from "node:path";

import { formatCompact, formatNumber, formatUsd } from "../src/utils/format";
import { Api, LT_DEV_KEY, LT_DOMAIN, LT_PROJECT, PageMonitor, SCREENSHOT_DIR, expectNavEntries, goto, idToken, nav, passingEvaluation, runtimeConfig, shot, signIn } from "./helpers";

test.describe.configure({ mode: "serial" });

interface Release {
  releaseId: string;
  status: string;
  blueprintVersion: number;
  promotion?: { harnessVersion?: string; endpoint?: string } | null;
  approval?: { by: string; decision: string } | null;
}
interface Gateway {
  projectId: string;
  env: string;
  gatewayId: string;
  mode: string;
}

/** Shared between the three serial tests; E2E_RELEASE_ID lets the approver test run alone against an existing PENDING_APPROVAL release. */
const state: { releaseId: string; sessionId: string; axe: Record<string, unknown> } = { releaseId: process.env.E2E_RELEASE_ID ?? "", sessionId: "", axe: {} };

async function axeCheck(page: Page, name: string): Promise<void> {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
  const summary = results.violations.map((v) => ({ id: v.id, impact: v.impact, nodes: v.nodes.length, help: v.help }));
  state.axe[name] = summary;
  writeFileSync(join(SCREENSHOT_DIR, "axe-summary.json"), JSON.stringify(state.axe, null, 2));
  const serious = summary.filter((v) => v.impact === "critical" || v.impact === "serious");
  expect(serious, `axe serious/critical violations on ${name}: ${JSON.stringify(serious)}`).toEqual([]);
}

async function kpiValues(page: Page): Promise<string[]> {
  const tiles = page.getByTestId("kpi-value");
  await expect(tiles.first()).toBeVisible();
  // wait until no tile shows a spinner any more
  await expect.poll(async () => (await tiles.allInnerTexts()).filter((t) => t.trim() === "").length, { timeout: 30_000 }).toBe(0);
  return (await tiles.allInnerTexts()).map((t) => t.trim());
}

test("admin: lists, project tabs, governance, catalogue, Overview equals the API, keyboard and axe", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  const monitor = new PageMonitor(page);
  await signIn(page, "admin", "/");
  const cfg = await runtimeConfig();
  const api = new Api(cfg.apiUrl, await idToken(page));
  await expectNavEntries(page, ["Overview", "Domains and projects", "Catalogue", "Governance", "Releases", "Playground"]);
  await shot(page, "admin-overview-default");

  // --- Domains and projects: lt-ops and lt-hr-assistant are listed
  await nav(page, "/domains");
  await expect(page.getByText(LT_DOMAIN, { exact: true }).first()).toBeVisible();
  await expect(page.getByText(LT_PROJECT, { exact: true }).first()).toBeVisible();
  await shot(page, "admin-domains-projects");

  // --- Project create / edit / delete through the modals (throwaway project in the lifecycle domain)
  const throwaway = `lt-ui-e2e-${Date.now().toString(36).slice(-5)}`;
  await page.getByTestId("create-project").click();
  const dialog = page.getByRole("dialog");
  await dialog.getByPlaceholder("hr-assistant").fill(throwaway);
  await dialog.locator('button[aria-haspopup="listbox"]').nth(0).click(); // Domain
  await page.getByRole("option", { name: new RegExp(LT_DOMAIN) }).first().click();
  await dialog.getByPlaceholder("HR Assistant").fill("UI e2e throwaway");
  await dialog.locator('button[aria-haspopup="listbox"]').nth(1).click(); // Allowed models (multiselect)
  await page.getByRole("option", { name: /claude-sonnet-5/ }).first().click();
  await page.keyboard.press("Escape");
  await shot(page, "admin-create-project-modal");
  await dialog.getByTestId("create-project-submit").click();
  await expect(page.getByText(`Project ${throwaway} created`).first()).toBeVisible({ timeout: 60_000 });
  // the create modal opens the new project's detail page; Edit and Delete live in its header
  await expect(page).toHaveURL(new RegExp(`/projects/${throwaway}`));
  await expect(page.getByText("UI e2e throwaway").first()).toBeVisible();
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  const edit = page.getByRole("dialog");
  await edit.getByLabel("Description").fill("edited by the e2e click-through");
  await edit.getByRole("button", { name: "Save changes" }).click();
  await expect(page.getByText(`Project ${throwaway} updated`).first()).toBeVisible({ timeout: 60_000 });
  await expect(page.getByText("edited by the e2e click-through").first()).toBeVisible();
  await shot(page, "admin-project-created-and-edited");
  await page.getByRole("button", { name: "Delete", exact: true }).click();
  const del = page.getByRole("dialog");
  await del.locator("input").first().fill(throwaway);
  await del.getByRole("button", { name: "Delete", exact: true }).click();
  await expect(page.getByText(`Project ${throwaway} deleted`).first()).toBeVisible({ timeout: 60_000 });
  await expect(page).toHaveURL(/\/domains$/);
  const gone = await api.get<Array<{ projectId: string }>>("/projects");
  expect(gone.body.some((p) => p.projectId === throwaway)).toBe(false);

  // --- Project detail tabs of lt-hr-assistant
  await goto(page, `/projects/${LT_PROJECT}?tab=resources`);
  await expect(page.getByText("lt_hr_assistant_dev", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("harness", { exact: true }).first()).toBeVisible();
  await shot(page, "admin-project-resources");

  await goto(page, `/projects/${LT_PROJECT}?tab=keys`);
  const keyRow = page.getByRole("row", { name: new RegExp(LT_DEV_KEY) }).first();
  await keyRow.waitFor();
  const keysBefore = await api.get<Array<{ alias: string; maxBudget: number }>>(`/projects/${LT_PROJECT}/keys`);
  const cap = keysBefore.body.find((k) => k.alias === LT_DEV_KEY)?.maxBudget ?? 1;
  await keyRow.getByRole("radio").first().check();
  await page.getByTestId("raise-cap").click();
  await page.getByRole("dialog").locator("input").first().fill(String(cap + 0.5));
  await page.getByTestId("raise-cap-submit").click();
  await expect(page.getByText(`set to ${formatUsd(cap + 0.5)}`).first()).toBeVisible({ timeout: 60_000 });
  await shot(page, "admin-project-keys-cap-raised");
  await page.getByTestId("raise-cap").click();
  await page.getByRole("dialog").locator("input").first().fill(String(cap));
  await page.getByTestId("raise-cap-submit").click();
  await expect(page.getByText(`set to ${formatUsd(cap)}`).first()).toBeVisible({ timeout: 60_000 });
  const keysAfter = await api.get<Array<{ alias: string; maxBudget: number }>>(`/projects/${LT_PROJECT}/keys`);
  expect(keysAfter.body.find((k) => k.alias === LT_DEV_KEY)?.maxBudget).toBe(cap);

  await goto(page, `/projects/${LT_PROJECT}?tab=blueprints`);
  await expect(page.getByText("Blueprint versions").first()).toBeVisible();
  await page.getByTestId("render-preview").click();
  await expect(page.getByText("liteLlmModelConfig").first()).toBeVisible({ timeout: 60_000 });
  await shot(page, "admin-project-blueprints-render");

  await goto(page, `/projects/${LT_PROJECT}?tab=releases`);
  await expect(page.getByText("Promoted", { exact: true }).first()).toBeVisible();
  await shot(page, "admin-project-releases");

  // --- Governance: models, guardrail, policy engine, lt gateways (toggle and restore), registry, settings round trip
  await nav(page, "/governance");
  await expect(page.getByText("Approved models").first()).toBeVisible();
  await expect(page.getByText("claude-sonnet-5").first()).toBeVisible();
  await expect(page.getByText("hr-baseline").first()).toBeVisible();
  // the approved inventory's two other tabs render against the live API (GET /governance/project-options) and sit in the URL
  await page.getByRole("tab", { name: "Tools" }).click();
  await expect(page.getByRole("heading", { name: /Approved tools/ })).toBeVisible();
  await expect(page).toHaveURL(/tab=tools/);
  await page.getByRole("tab", { name: "Knowledge bases" }).click();
  await expect(page.getByRole("heading", { name: /Approved knowledge bases/ })).toBeVisible();
  await page.getByRole("tab", { name: "Models" }).click();
  await expect(page.getByText("Approved models").first()).toBeVisible();
  await expect(page.getByText("Policy engine and gateways").first()).toBeVisible();
  // Section guidance sits behind the round (i) icon next to each heading: click pins it in a popover, Escape closes it.
  await expect(page.getByText(/Agent Registry/)).toHaveCount(0);
  await page.getByRole("button", { name: "Information about Registry", exact: true }).click();
  await expect(page.getByText(/Agent Registry/).first()).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByText(/Agent Registry/)).toHaveCount(0);
  const gov = await api.get<{ gateways: Gateway[]; registry: { id: string | null } }>("/governance");
  expect(gov.body.registry.id).toBeTruthy();
  const ltDev = gov.body.gateways.find((g) => g.projectId === LT_PROJECT && g.env === "dev");
  expect(ltDev, "lt-hr-assistant dev gateway in GET /governance").toBeTruthy();
  const previousMode = ltDev!.mode;
  const otherMode = previousMode === "ENFORCE" ? "LOG_ONLY" : "ENFORCE";
  const gatewayRow = () => page.getByRole("row", { name: new RegExp(`${LT_PROJECT}.*dev`) }).first();
  await gatewayRow().waitFor();
  await expect(gatewayRow().getByText(previousMode, { exact: true }).first()).toBeVisible();
  await gatewayRow().locator('input[type="checkbox"]').first().click({ force: true });
  await page.getByTestId("confirm").click();
  await expect(page.getByText(`is now ${otherMode}`).first()).toBeVisible({ timeout: 90_000 });
  await shot(page, "admin-governance-gateway-toggled");
  await expect(gatewayRow().getByText(otherMode, { exact: true }).first()).toBeVisible({ timeout: 30_000 });
  await gatewayRow().locator('input[type="checkbox"]').first().click({ force: true });
  await page.getByTestId("confirm").click();
  await expect(page.getByText(`is now ${previousMode}`).first()).toBeVisible({ timeout: 90_000 });
  const govAfter = await api.get<{ gateways: Gateway[] }>("/governance");
  expect(govAfter.body.gateways.find((g) => g.gatewayId === ltDev!.gatewayId)?.mode).toBe(previousMode);

  // Settings round trip (PUT /settings through the form): rpmLimit +1 and back; the API is polled for each value because the
  // "saved" flash of the first save is still visible when the second one is clicked. The finally block restores the default
  // through the API if the UI path fails midway, so the platform defaults never stay changed.
  const settings = await api.get<{ defaultProjectCap: { rpmLimit: number } }>("/settings");
  const rpm = settings.body.defaultProjectCap.rpmLimit;
  const rpmInput = page.getByTestId("setting-defaultProjectCap.rpmLimit").locator("input");
  const readRpm = async () => (await api.get<{ defaultProjectCap: { rpmLimit: number } }>("/settings")).body.defaultProjectCap.rpmLimit;
  try {
    await rpmInput.fill(String(rpm + 1));
    await page.getByTestId("save-settings").click();
    await expect(page.getByText("Default limits and caps saved").first()).toBeVisible({ timeout: 60_000 });
    await expect.poll(readRpm, { timeout: 30_000 }).toBe(rpm + 1);
    await expect(page.getByText(/by admin/).first()).toBeVisible();
    await rpmInput.fill(String(rpm));
    await page.getByTestId("save-settings").click();
    await expect.poll(readRpm, { timeout: 30_000 }).toBe(rpm);
  } finally {
    if ((await readRpm()) !== rpm) {
      console.log(`[e2e] restoring defaultProjectCap.rpmLimit ${rpm} through the API`);
      await api.put("/settings", { defaultProjectCap: { rpmLimit: rpm } });
    }
  }
  await shot(page, "admin-governance");

  // --- Catalogue: whatever the Agent Registry holds right now (the workshop state may be mid-rebuild: `workshop cleanup` empties the
  // registry, modules 4 and 5 fill and approve it again), so the expectations follow GET /catalogue?status=APPROVED
  await nav(page, "/catalogue");
  const records = await api.get<Array<{ name: string; status: string; recordType: string }>>("/catalogue");
  const approved = records.body.filter((r) => r.status === "APPROVED");
  const searchBox = page.getByTestId("catalogue-search").locator("input");
  if (records.body.length) {
    for (const record of records.body) {
      await expect(page.getByText(record.name, { exact: true }).first()).toBeVisible();
    }
  } else {
    await expect(page.getByText(/^No records$|No Agent Registry configured/).first()).toBeVisible();
  }
  // the Records tabs (All / Models / Knowledge bases / Tools / Agents) filter by the backend's category and sit in the URL
  await page.getByRole("tab", { name: /^Agents/ }).click();
  await expect(page).toHaveURL(/tab=agent/);
  await expect(page.getByRole("tab", { name: /^Agents/ })).toHaveAttribute("aria-selected", "true");
  const agents = records.body.filter((r) => r.recordType === "AGENT");
  const notAgent = records.body.find((r) => r.recordType !== "AGENT");
  if (agents.length) {
    await expect(page.getByText(agents[0].name, { exact: true }).first()).toBeVisible();
  } else if (records.body.length) {
    await expect(page.getByText("No agents").first()).toBeVisible();
  }
  if (notAgent) {
    await expect(page.getByText(notAgent.name, { exact: true })).toHaveCount(0); // filtered out of the Agents tab
  }
  await page.getByRole("tab", { name: /^All/ }).click();
  await expect(page.getByRole("tab", { name: /^All/ })).toHaveAttribute("aria-selected", "true");
  await shot(page, "admin-catalogue");
  if (approved.length) {
    await searchBox.fill(approved[0].name.slice(0, 2));
    await searchBox.press("Enter");
    await expect(page.getByText(approved[0].name, { exact: true }).first()).toBeVisible();
  } else {
    await searchBox.fill("hr");
    await searchBox.press("Enter");
    await expect(page.getByText("No approved records match").first()).toBeVisible();
  }
  console.log(`[e2e] catalogue: ${records.body.length} record(s) listed (${approved.length} APPROVED); search ${approved.length ? "found the first approved record" : "shows the no-approved-records state"}`);
  await shot(page, "admin-catalogue-search");

  // --- Overview of lt-hr-assistant dev: KPI values equal GET /metrics and GET /spend
  await goto(page, `/?project=${LT_PROJECT}&env=dev&range=24h`);
  await expect(page.getByText(`${LT_PROJECT} · Dev · last 24 h`).first()).toBeVisible();
  const values = await kpiValues(page);
  const metrics = await api.get<{ totals: Record<string, number | null> }>(`/metrics?projectId=${LT_PROJECT}&env=dev&hours=24`);
  const spend = await api.get<{ byEnv: Record<string, { spend: number }> }>(`/spend?projectId=${LT_PROJECT}&env=dev`);
  expect(values[0], "Invocations KPI equals GET /metrics totals.invocations").toBe(formatCompact(metrics.body.totals.invocations ?? 0));
  expect(values[3], "Spend KPI equals GET /spend byEnv.dev.spend").toBe(formatUsd(spend.body.byEnv.dev.spend));
  expect(values[4], "Allowed requests equals totals.policyAllow").toBe(formatNumber(metrics.body.totals.policyAllow ?? 0));
  expect(values[5], "Denied requests equals totals.policyDeny").toBe(formatNumber(metrics.body.totals.policyDeny ?? 0));
  await page.waitForTimeout(1500); // chart animations
  await shot(page, "admin-overview-lt-dev");
  await axeCheck(page, "overview");
  // "All domains" is an explicit choice (?domain=all): the select keeps it, the project stays and every project is offered
  await page.getByTestId("overview-domain").locator('button[aria-haspopup="listbox"]').click();
  await page.getByRole("option", { name: "All domains" }).click();
  await expect(page).toHaveURL(/domain=all/);
  await expect(page.getByTestId("overview-domain")).toContainText("All domains");
  await expect(page.getByText(`${LT_PROJECT} · Dev · last 24 h`).first()).toBeVisible();
  const allProjects = await api.get<Array<{ projectId: string }>>("/projects");
  await page.getByTestId("overview-project").locator('button[aria-haspopup="listbox"]').click();
  await expect(page.getByRole("option")).toHaveCount(allProjects.body.length);
  await page.keyboard.press("Escape");
  await page.getByTestId("overview-domain").locator('button[aria-haspopup="listbox"]').click();
  await page.getByRole("option", { name: new RegExp(LT_DOMAIN) }).first().click();
  await expect(page).toHaveURL(new RegExp(`domain=${LT_DOMAIN}`));
  await expect(page.getByText(`${LT_PROJECT} · Dev · last 24 h`).first()).toBeVisible();
  await page.getByTestId("overview-refresh").click();
  await expect(page.getByTestId("overview-refresh")).toBeEnabled({ timeout: 30_000 });

  // --- Releases list, release detail (promoted and blocked), keyboard navigation, modal via keyboard
  await nav(page, "/releases");
  await expect(page.getByRole("row", { name: new RegExp(LT_PROJECT) }).first()).toBeVisible();
  await shot(page, "admin-releases");
  await axeCheck(page, "releases");
  const releases = await api.get<Release[]>(`/projects/${LT_PROJECT}/releases`);
  const promoted = releases.body.find((r) => r.status === "PROMOTED");
  const blocked = releases.body.find((r) => r.status === "BLOCKED");
  expect(promoted && blocked, "lifecycle releases PROMOTED + BLOCKED present").toBeTruthy();
  await goto(page, `/releases/${promoted!.releaseId}`);
  await expect(page.getByText("Scores vs thresholds").first()).toBeVisible();
  await expect(page.getByText("Harness version", { exact: true }).first()).toBeVisible();
  const approveButton = page.getByTestId("approve-release");
  await expect(approveButton).toBeDisabled();
  await approveButton.hover();
  await expect(page.getByText(/Approvers only/).filter({ visible: true }).first()).toBeVisible();
  await shot(page, "admin-release-promoted");
  await axeCheck(page, "release-detail");
  await goto(page, `/releases/${blocked!.releaseId}`);
  await expect(page.getByText("Thresholds missed").first()).toBeVisible();
  await expect(page.getByText("fail", { exact: true }).first()).toBeVisible();
  await shot(page, "admin-release-blocked");

  // keyboard: Tab to the side navigation entry, Enter follows it; the focused element has a visible focus ring
  await goto(page, "/releases");
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  let focused = "";
  for (let i = 0; i < 40 && focused !== "Domains and projects"; i += 1) {
    await page.keyboard.press("Tab");
    focused = (await page.evaluate(() => document.activeElement?.textContent?.trim() ?? "")) ?? "";
  }
  expect(focused).toBe("Domains and projects");
  const ring = await page.evaluate(() => {
    const el = document.activeElement as HTMLElement;
    const style = getComputedStyle(el);
    return { outline: style.outlineStyle, boxShadow: style.boxShadow, outlineWidth: style.outlineWidth };
  });
  expect(ring.outline !== "none" || ring.boxShadow !== "none", `focus ring visible: ${JSON.stringify(ring)}`).toBe(true);
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/domains$/);
  await goto(page, "/releases");
  await page.getByRole("button", { name: "Create release" }).first().focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toBeHidden();
  await goto(page, "/playground");
  await axeCheck(page, "playground");
  await goto(page, "/governance");
  await axeCheck(page, "governance");

  monitor.assertClean();
  await context.close();
});

test("developer: creates a release in the UI, passes the evaluation through the API, chats twice in one Playground session, rates the answer", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  const monitor = new PageMonitor(page);
  await signIn(page, "developer", "/releases");
  await expectNavEntries(page, ["Overview", "Domains and projects", "Catalogue", "Releases", "Playground"]);
  const cfg = await runtimeConfig();
  const api = new Api(cfg.apiUrl, await idToken(page));

  // --- Create a release from the latest blueprint version
  const blueprints = await api.get<Array<{ version: number }>>(`/projects/${LT_PROJECT}/blueprints`);
  const latest = Math.max(...blueprints.body.map((b) => b.version));
  await page.getByTestId("create-release").click();
  const dialog = page.getByRole("dialog");
  await dialog.getByTestId("release-project").click();
  await page.getByRole("option", { name: new RegExp(LT_PROJECT) }).first().click();
  await expect(dialog.getByTestId("release-version")).toContainText(`v${latest}`);
  await dialog.getByTestId("release-notes").locator("textarea").fill("created by the UI e2e click-through");
  await shot(page, "developer-create-release-modal");
  await dialog.getByTestId("create-release-submit").click();
  await expect(page).toHaveURL(/\/releases\/[0-9A-Z]+$/, { timeout: 60_000 });
  const uiReleaseId = page.url().split("/").pop() ?? "";
  // Creating a release starts its run: the platform deploys the version to Dev when needed and evaluates it (6-7 minutes). The
  // click-through does not wait for it: a manual evaluation is refused (409) while that run is live, so the approval and
  // promotion steps below use a second release created through the API with autoRun off and a posted passing evaluation.
  await expect(page.getByText("Status: EVALUATING").first()).toBeVisible();
  await expect(page.getByText("Evaluation run").first()).toBeVisible();
  await shot(page, "developer-release-evaluating");
  const refused = await api.post<Release>(`/releases/${uiReleaseId}/evaluation`, passingEvaluation(`run-ui-e2e-refused-${Date.now()}`));
  expect(refused.status, JSON.stringify(refused.body)).toBe(409);

  const created = await api.post<Release>(`/projects/${LT_PROJECT}/releases`, {
    blueprintVersion: latest,
    notes: "created by the UI e2e click-through (evaluation posted through the API)",
    autoRun: false,
  });
  expect(created.status, JSON.stringify(created.body)).toBe(201);
  expect(created.body.status).toBe("DRAFT");
  state.releaseId = created.body.releaseId;
  const evaluated = await api.post<Release>(`/releases/${state.releaseId}/evaluation`, passingEvaluation(`run-ui-e2e-${Date.now()}`));
  expect(evaluated.status, JSON.stringify(evaluated.body)).toBe(200);
  expect(evaluated.body.status).toBe("PENDING_APPROVAL");
  await goto(page, `/releases/${state.releaseId}`);
  await expect(page.getByText("Status: PENDING APPROVAL").first()).toBeVisible();
  await expect(page.getByText("PASSED: every threshold met").first()).toBeVisible();
  await expect(page.getByText("pass", { exact: true })).toHaveCount(4);
  const approveButton = page.getByTestId("approve-release");
  await expect(approveButton).toBeDisabled();
  await approveButton.hover();
  await expect(page.getByText(/Approvers only/).filter({ visible: true }).first()).toBeVisible();
  await shot(page, "developer-release-pending-approval");

  // --- Playground: two turns in one session, then a thumbs up
  await goto(page, `/playground?project=${LT_PROJECT}&env=dev`);
  await expect(page.getByText("Conversation").first()).toBeVisible();
  await expect(page.getByText(/lt_hr_assistant_dev · version/).first()).toBeVisible();
  const sessionId = (await page.getByTestId("session-id").innerText()).trim();
  expect(sessionId.length).toBeGreaterThan(20);
  await shot(page, "developer-playground-empty");
  const input = page.getByTestId("prompt-input").locator("textarea");
  const started = Date.now();
  await input.fill("What is my leave balance? My employee id is E1001.");
  await input.press("Enter");
  await expect(page.getByText(/Thinking…|Queued…/).first()).toBeVisible({ timeout: 30_000 });
  await shot(page, "developer-playground-thinking");
  await expect(page.getByTestId("answer-text")).toHaveCount(1, { timeout: 240_000 });
  const firstSeconds = Math.round((Date.now() - started) / 1000);
  const firstAnswer = await page.getByTestId("answer-text").first().innerText();
  expect(firstAnswer.length).toBeGreaterThan(10);
  await expect(page.getByTestId("turn-usage").first()).toContainText("tokens");
  const second = Date.now();
  await input.fill("Thanks. And how many sick days do I have left?");
  await input.press("Enter");
  await expect(page.getByTestId("answer-text")).toHaveCount(2, { timeout: 240_000 });
  const secondSeconds = Math.round((Date.now() - second) / 1000);
  console.log(`[e2e] playground turns: first ${firstSeconds} s (new session), second ${secondSeconds} s (same session); tools: ${await page.locator('[data-testid="turn-usage"]').count()} usage lines`);
  expect((await page.getByTestId("session-id").innerText()).trim()).toBe(sessionId);
  state.sessionId = sessionId;
  await page.getByRole("button", { name: "Helpful" }).first().click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.getByTestId("feedback-comment").locator("textarea").fill("e2e: correct leave balance");
  await shot(page, "developer-playground-feedback-modal");
  await page.getByTestId("feedback-submit").click();
  await expect(page.getByText("Feedback recorded: thumbs up").first()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("rated up").first()).toBeVisible();
  await shot(page, "developer-playground-conversation");
  const feedback = await api.get<Array<{ sessionId: string; rating: string; createdBy: string }>>(`/feedback?projectId=${LT_PROJECT}`);
  const fullSessionId = feedback.body.find((f) => f.createdBy === "developer" && f.rating === "up" && sessionId.replace(/…$/, "").length && f.sessionId.startsWith(sessionId.slice(0, 20)));
  expect(fullSessionId, "feedback row for this session").toBeTruthy();
  // switching the environment (same page instance, URL param change) is a new conversation: the Dev transcript and its session id
  // never carry over to the Prod harness
  await page.getByRole("button", { name: "Prod", exact: true }).click();
  await expect(page).toHaveURL(/env=prod/);
  await expect(page.getByText(/lt_hr_assistant_prod · version/).first()).toBeVisible();
  await expect(page.getByTestId("answer-text")).toHaveCount(0);
  expect((await page.getByTestId("session-id").innerText()).trim()).not.toBe(sessionId);
  await page.getByTestId("new-session").click();
  await expect(page.getByTestId("answer-text")).toHaveCount(0);

  // --- Empty state for an environment without a harness (whichever workshop project has none right now)
  const projects = await api.get<Array<{ projectId: string }>>("/projects");
  for (const project of projects.body) {
    const resources = await api.get<Array<{ env: string; type: string; arn: string | null }>>(`/projects/${project.projectId}/resources`);
    const env = (["prod", "dev"] as const).find((e) => !resources.body.some((r) => r.env === e && r.type === "harness" && r.arn));
    if (env) {
      await goto(page, `/playground?project=${project.projectId}&env=${env}`);
      await expect(page.getByText(`${project.projectId} has no ${env === "prod" ? "Prod" : "Dev"} harness yet`).first()).toBeVisible();
      await shot(page, "developer-playground-no-harness");
      break;
    }
  }

  monitor.assertClean();
  await context.close();
});

test("approver: approves and promotes the new release, watches the job to PROMOTED, sees the version history", async ({ browser }) => {
  expect(state.releaseId, "release created by the developer test").not.toBe("");
  const context = await browser.newContext();
  const page = await context.newPage();
  const monitor = new PageMonitor(page);
  await signIn(page, "approver", `/releases/${state.releaseId}`);
  await expectNavEntries(page, ["Overview", "Releases", "Playground"]);
  await expect(page).toHaveURL(new RegExp(`/releases/${state.releaseId}$`));
  const cfg = await runtimeConfig();
  const api = new Api(cfg.apiUrl, await idToken(page));

  await expect(page.getByText("Status: PENDING APPROVAL").first()).toBeVisible();
  await expect(page.getByTestId("approve-release")).toBeEnabled();
  await expect(page.getByTestId("promote-release")).toBeDisabled();
  await expect(page.getByRole("button", { name: "Open project" })).toHaveCount(0); // approvers have no project screen
  await shot(page, "approver-release-pending");
  await page.getByTestId("approve-release").click();
  await page.getByTestId("decision-comment").locator("textarea").fill("scores above every threshold (UI e2e)");
  await shot(page, "approver-approve-modal");
  await page.getByTestId("decision-submit").click();
  await expect(page.getByText(/approved by approver/).first()).toBeVisible({ timeout: 60_000 });
  await expect(page.getByText("Status: APPROVED").first()).toBeVisible();
  await expect(page.getByTestId("promote-release")).toBeEnabled();
  await shot(page, "approver-release-approved");

  await page.getByTestId("promote-release").click();
  await expect(page.getByRole("dialog")).toContainText("DEFAULT");
  await page.getByTestId("confirm").click();
  await expect(page.getByText(/Promotion job .* started/).first()).toBeVisible({ timeout: 60_000 });
  await expect(page.getByText("Promotion requested").first()).toBeVisible();
  await expect(page.getByText(/job (QUEUED|RUNNING)/).first()).toBeVisible({ timeout: 30_000 });
  await shot(page, "approver-promotion-running");
  await expect(page.getByText("Harness version", { exact: true }).first()).toBeVisible({ timeout: 8 * 60_000 });
  await expect(page.getByText("Status: PROMOTED").first()).toBeVisible();
  await expect(page.getByText(`Release ${state.releaseId} promoted`).first()).toBeVisible();
  await expect(page.getByText("(this release)").first()).toBeVisible();
  await shot(page, "approver-release-promoted-history");

  const final = await api.get<Release>(`/releases/${state.releaseId}`);
  expect(final.body.status).toBe("PROMOTED");
  expect(final.body.promotion?.endpoint).toBe("DEFAULT");
  expect(final.body.approval?.by).toBe("approver");
  console.log(`[e2e] release ${state.releaseId} PROMOTED: harness version ${final.body.promotion?.harnessVersion} on ${final.body.promotion?.endpoint}`);
  await expect(page.getByText(new RegExp(`v${final.body.promotion?.harnessVersion} on DEFAULT`)).first()).toBeVisible();

  // the role rule is visible on a release that cannot be decided any more
  await expect(page.getByTestId("approve-release")).toBeDisabled();
  // opening another release from the version history gets a fresh page: the finished promotion job of this release must not show there
  const ltReleases = await api.get<Release[]>(`/projects/${LT_PROJECT}/releases`);
  const blockedRelease = ltReleases.body.find((r) => r.status === "BLOCKED");
  expect(blockedRelease, "lifecycle BLOCKED release present").toBeTruthy();
  await page.getByRole("link", { name: blockedRelease!.releaseId }).first().click();
  await expect(page).toHaveURL(new RegExp(`/releases/${blockedRelease!.releaseId}$`));
  await expect(page.getByText("Status: BLOCKED").first()).toBeVisible();
  await expect(page.getByText("Promotion requested")).toHaveCount(0);
  await expect(page.getByText("Not promoted").first()).toBeVisible();
  await expect(page.getByTestId("promote-release")).toBeDisabled();
  await shot(page, "approver-blocked-release-after-promotion");
  // the role notice names only the personas that can use the screen
  await goto(page, "/domains");
  await expect(page.getByText(/Switch persona to admin or developer from the user menu/).first()).toBeVisible();
  await nav(page, "/releases");
  await expect(page.getByRole("row", { name: new RegExp(state.releaseId) })).toContainText("Promoted");
  await shot(page, "approver-releases");

  monitor.assertClean();
  await context.close();
});
