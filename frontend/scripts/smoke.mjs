#!/usr/bin/env node
/**
 * Headless smoke test of the deployed (or local) UI with Playwright: signs in through the Cognito Hosted UI as one persona,
 * walks the screens, asserts that data from the platform API renders, and (with WRITE=1) exercises the write actions on the
 * lifecycle-test project lt-hr-assistant only. Never touches hr-assistant / invoice-helper.
 *
 *   BASE_URL=https://<ui> USERNAME=admin PASSWORD=... [WRITE=1] [SCREEN_DIR=...] node scripts/smoke.mjs
 */
import { chromium } from "playwright";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const BASE_URL = (process.env.BASE_URL ?? "http://localhost:5173").replace(/\/+$/, "");
const USERNAME = process.env.USERNAME_OVERRIDE ?? process.env.PERSONA ?? "admin";
const PASSWORD = process.env.PASSWORD;
const WRITE = process.env.WRITE === "1";
const SCREEN_DIR = process.env.SCREEN_DIR ?? "./smoke-screens";
const LT_PROJECT = "lt-hr-assistant";
if (!PASSWORD) {
  console.error("PASSWORD is required");
  process.exit(2);
}
mkdirSync(SCREEN_DIR, { recursive: true });

const results = [];
const consoleErrors = [];
const failedRequests = [];
const pageErrors = [];
let step = 0;

function record(name, ok, detail = "") {
  results.push({ name, ok, detail });
  console.log(`${ok ? "ok  " : "FAIL"} ${name}${detail ? ` - ${detail}` : ""}`);
}

async function shot(page, name) {
  step += 1;
  const file = join(SCREEN_DIR, `${String(step).padStart(2, "0")}-${name}.png`);
  await page.screenshot({ path: file, fullPage: true });
  return file;
}

async function expectText(page, text, name, timeout = 30_000) {
  try {
    await page.getByText(text, { exact: false }).first().waitFor({ state: "visible", timeout });
    record(name, true, `"${text}" visible`);
    return true;
  } catch (error) {
    record(name, false, `"${text}" not visible: ${error.message.split("\n")[0]}`);
    return false;
  }
}

const NAV_HREFS = {
  Overview: "/",
  "Domains and projects": "/domains",
  Catalogue: "/catalogue",
  Governance: "/governance",
  Releases: "/releases",
  Playground: "/playground",
};

/** Click the side-navigation entry; fall back to a direct navigation when the link is not clickable (collapsed nav). */
async function nav(page, label) {
  const href = NAV_HREFS[label];
  const link = page.locator(`nav a[href="${href}"]`).first();
  try {
    await link.scrollIntoViewIfNeeded({ timeout: 5_000 });
    await link.click({ timeout: 10_000 });
  } catch (error) {
    record(`side navigation click "${label}" fell back to goto`, true, String(error).split("\n")[0].slice(0, 120));
    await page.goto(`${BASE_URL}${href}`, { waitUntil: "networkidle" });
  }
  await page.waitForLoadState("networkidle").catch(() => {});
}

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, ignoreHTTPSErrors: true });
const page = await context.newPage();
const consoleWarnings = [];
page.on("console", (message) => {
  if (message.type() === "error") {
    consoleErrors.push(message.text().slice(0, 300));
  } else if (message.type() === "warning" && !/React Router Future Flag|Download the React DevTools/.test(message.text())) {
    consoleWarnings.push(message.text().slice(0, 300));
  }
});
page.on("pageerror", (error) => pageErrors.push(String(error).slice(0, 300)));
page.on("requestfailed", (request) => failedRequests.push(`${request.method()} ${request.url()} ${request.failure()?.errorText ?? ""}`));
page.on("response", (response) => {
  if (response.status() >= 400 && response.url().includes("execute-api")) {
    failedRequests.push(`${response.request().method()} ${response.url()} -> ${response.status()}`);
  }
});

try {
  // 1. Landing page and sign-in through the Hosted UI
  await page.goto(`${BASE_URL}/governance`, { waitUntil: "networkidle" });
  await expectText(page, "AnyCompany Agent Platform", "landing page renders");
  await shot(page, "landing");
  await page.getByTestId("sign-in").click();
  await page.waitForURL(/amazoncognito\.com/, { timeout: 30_000 });
  record("redirected to the Cognito Hosted UI", true, new URL(page.url()).host);
  await page.locator('input[name="username"]:visible').first().fill(USERNAME);
  await page.locator('input[name="password"]:visible').first().fill(PASSWORD);
  await shot(page, "hosted-ui");
  await page.locator('input[name="password"]:visible').first().press("Enter");
  await page.waitForURL((url) => url.origin === new URL(BASE_URL).origin, { timeout: 60_000 });
  await page.waitForLoadState("networkidle").catch(() => {});
  record("returned to the application after sign-in", true, page.url());
  // deep link restored?
  const restored = page.url().replace(/\/$/, "").endsWith("/governance");
  record("deep link /governance restored after sign-in", restored, page.url());

  // 2. Governance (admin only; other personas see the role notice and no Governance entry in the navigation)
  const isAdmin = USERNAME === "admin";
  if (isAdmin) {
    await expectText(page, "Approved models", "Governance screen renders");
    await expectText(page, "claude-sonnet-5-5", "models table shows claude-sonnet-5-5");
    await expectText(page, "hr-baseline", "guardrail panel shows hr-baseline");
    await expectText(page, "Policy engine and gateways", "policy engine section renders");
    await expectText(page, "Default limits and caps", "settings form renders");
  } else {
    await expectText(page, "This screen is not part of your role", `${USERNAME} is told Governance is not part of the role`);
  }
  await shot(page, "governance");
  const persona = await page.locator('[class*="top-navigation"]').first().innerText().catch(() => "");
  record("top navigation shows the persona", new RegExp(USERNAME, "i").test(persona), persona.replace(/\s+/g, " ").slice(0, 120));
  const navLinks = await page.locator("nav a").allInnerTexts();
  const expectedNav = {
    admin: ["Overview", "Domains and projects", "Catalogue", "Governance", "Releases", "Playground"],
    developer: ["Overview", "Domains and projects", "Catalogue", "Releases", "Playground"],
    approver: ["Overview", "Releases", "Playground"],
  }[USERNAME] ?? [];
  const navOk = JSON.stringify(navLinks.filter((t) => expectedNav.includes(t) || t === "Governance")) === JSON.stringify(expectedNav);
  record(`side navigation entries for ${USERNAME}`, navOk, navLinks.join(" | "));

  // 3. Overview
  await nav(page, "Overview");
  await expectText(page, "Invocations", "Overview KPI tiles render");
  await expectText(page, "Policy decisions", "Overview policy section renders");
  await expectText(page, "Decisions per tool", "byTool table renders");
  await shot(page, "overview-default");
  // Switch to the lifecycle project, prod env (has harness metrics and spend)
  await page.goto(`${BASE_URL}/?project=${LT_PROJECT}&env=prod&range=24h`, { waitUntil: "networkidle" });
  await expectText(page, LT_PROJECT, "Overview shows the lifecycle project");
  const navVisible = await page.locator('nav a[href="/releases"]').first().isVisible().catch(() => false);
  record("side navigation open after a fresh deep-link load", navVisible, navVisible ? "visible" : "collapsed");
  await expectText(page, "Spend and tokens per key", "spend section renders");
  await page.waitForTimeout(2500);
  await shot(page, "overview-lt-prod");

  // 4. Domains and projects (admins and developers)
  if (USERNAME === "approver") {
    await page.goto(`${BASE_URL}/domains`, { waitUntil: "networkidle" });
    await expectText(page, "This screen is not part of your role", "approver is told Domains and projects is not part of the role");
    await nav(page, "Releases");
    await expectText(page, "Step 2 of the UI build", "approver sees the Releases screen");
    await shot(page, "approver-releases");
    throw new Error("__done__");
  }
  await nav(page, "Domains and projects");
  await expectText(page, "people-ops", "domains table lists people-ops");
  await expectText(page, "hr-assistant", "projects table lists hr-assistant");
  await shot(page, "domains-projects");

  // 5. Project detail (lifecycle project) tabs
  await page.goto(`${BASE_URL}/projects/${LT_PROJECT}?tab=resources`, { waitUntil: "networkidle" });
  await expectText(page, "Dev resources", "Resources tab renders");
  await expectText(page, "harness", "resources include the harness");
  await shot(page, "project-resources");
  await page.goto(`${BASE_URL}/projects/${LT_PROJECT}?tab=keys`, { waitUntil: "networkidle" });
  await expectText(page, `${LT_PROJECT}-dev-claude-haiku-4-5`, "Keys tab lists the dev key");
  const raiseCapVisible = await page.getByTestId("raise-cap").isVisible().catch(() => false);
  const createKeyVisible = await page.getByTestId("create-key").isVisible().catch(() => false);
  record("key actions follow the role (Raise cap admin-only, Create key dev + admin)", raiseCapVisible === isAdmin && createKeyVisible, `raise cap ${raiseCapVisible}, create key ${createKeyVisible}`);
  await page.waitForTimeout(1500);
  await shot(page, "project-keys");
  await page.goto(`${BASE_URL}/projects/${LT_PROJECT}?tab=blueprints`, { waitUntil: "networkidle" });
  await expectText(page, "Blueprint versions", "Blueprints tab renders");
  await page.getByTestId("render-preview").click();
  const rendered = await expectText(page, "liteLlmModelConfig", "render preview shows the harness payload", 30_000);
  if (rendered) {
    await shot(page, "project-blueprints-render");
  }
  await page.goto(`${BASE_URL}/projects/${LT_PROJECT}?tab=releases`, { waitUntil: "networkidle" });
  await expectText(page, "Promoted", "Releases tab shows the promoted release");
  await shot(page, "project-releases");

  // 6. Catalogue with split panel detail
  await nav(page, "Catalogue");
  await expectText(page, "hr-assistant-tools", "Catalogue lists hr-assistant-tools");
  await page.getByRole("row", { name: /hr-assistant-tools/ }).first().getByRole("radio").check().catch(async () => {
    await page.getByText("hr-assistant-tools", { exact: true }).first().click();
  });
  await expectText(page, "Descriptors", "split panel shows the record detail");
  await expectText(page, "mcpServer", "descriptors JSON loaded (GET /catalogue/{id})");
  await shot(page, "catalogue-detail");
  await page.getByTestId("catalogue-search").locator("input").fill("hr");
  await page.getByTestId("catalogue-search").locator("input").press("Enter");
  await page.waitForLoadState("networkidle").catch(() => {});
  await expectText(page, "hr-assistant-tools", "search for 'hr' returns the MCP record");
  await shot(page, "catalogue-search");

  // 7. Releases list and Playground placeholder
  await nav(page, "Releases");
  await expectText(page, "Step 2 of the UI build", "Releases screen renders the step-2 notice");
  await expectText(page, LT_PROJECT, "Releases list shows the lifecycle releases");
  await shot(page, "releases");
  await nav(page, "Playground");
  await expectText(page, "Coming in step 2", "Playground placeholder renders");

  // 8. Write actions on the lifecycle project only
  if (WRITE) {
    // 8a. governance: toggle the lt dev gateway LOG_ONLY -> ENFORCE -> LOG_ONLY
    await nav(page, "Governance");
    const row = page.getByRole("row", { name: new RegExp(`${LT_PROJECT}.*dev`) }).first();
    await row.waitFor({ timeout: 30_000 });
    const toggle = row.locator('input[type="checkbox"]').first();
    await toggle.click({ force: true });
    await page.getByTestId("confirm").click();
    const enforced = await expectText(page, "is now ENFORCE", "lt dev gateway switched to ENFORCE", 60_000);
    await shot(page, "governance-enforce");
    if (enforced) {
      await page.waitForTimeout(1500);
      const row2 = page.getByRole("row", { name: new RegExp(`${LT_PROJECT}.*dev`) }).first();
      await row2.locator('input[type="checkbox"]').first().click({ force: true });
      await page.getByTestId("confirm").click();
      await expectText(page, "is now LOG_ONLY", "lt dev gateway restored to LOG_ONLY", 60_000);
    }
    // 8b. keys: raise cap of the lt dev key to 1.5 then back to 1
    await page.goto(`${BASE_URL}/projects/${LT_PROJECT}?tab=keys`, { waitUntil: "networkidle" });
    const keyRow = page.getByRole("row", { name: new RegExp(`${LT_PROJECT}-dev-claude-haiku-4-5`) }).first();
    await keyRow.waitFor({ timeout: 30_000 });
    await keyRow.getByRole("radio").first().check();
    await page.getByTestId("raise-cap").click();
    await page.getByRole("dialog").locator("input").first().fill("1.5");
    await page.getByTestId("raise-cap-submit").click();
    await expectText(page, "set to $1.50", "lt dev key cap raised to $1.50", 30_000);
    await shot(page, "keys-raise-cap");
    await page.getByTestId("raise-cap").click();
    await page.getByRole("dialog").locator("input").first().fill("1");
    await page.getByTestId("raise-cap-submit").click();
    await expectText(page, "set to $1.00", "lt dev key cap restored to $1.00", 30_000);
    // 8c. suspend and resume the lt dev key
    await keyRow.getByRole("radio").first().check();
    await page.getByRole("button", { name: "Suspend", exact: true }).click();
    await page.getByTestId("confirm").click();
    await expectText(page, "suspended (blocked in LiteLLM)", "lt dev key suspended", 30_000);
    await page.getByRole("row", { name: new RegExp(`${LT_PROJECT}-dev-claude-haiku-4-5`) }).first().getByRole("radio").first().check();
    await page.getByRole("button", { name: "Resume", exact: true }).click();
    await page.getByTestId("confirm").click();
    await expectText(page, "resumed", "lt dev key resumed", 30_000);
    // 8d. domain create + delete (own throwaway domain)
    await nav(page, "Domains and projects");
    await page.getByTestId("create-domain").click();
    await page.getByRole("dialog").locator("input").nth(0).fill("lt-ui-smoke");
    await page.getByRole("dialog").locator("input").nth(1).fill("UI smoke domain");
    await page.getByTestId("create-domain-submit").click();
    await expectText(page, "Domain lt-ui-smoke created", "domain lt-ui-smoke created", 30_000);
    await shot(page, "domain-created");
    await page.getByRole("row", { name: /lt-ui-smoke/ }).first().getByRole("radio").first().check();
    await page.getByTestId("delete-domain").click();
    await page.getByTestId("confirm").click();
    await expectText(page, "Domain lt-ui-smoke deleted", "domain lt-ui-smoke deleted", 30_000);
    // 8e. catalogue decision modal opens and is cancelled (lab records stay untouched)
    await nav(page, "Catalogue");
    await page.getByRole("row", { name: /kb-hr-policies/ }).first().getByRole("radio").first().check();
    await page.getByTestId("set-status").click();
    await page.getByRole("menuitem", { name: "Deprecate" }).click();
    await expectText(page, "Deprecate HR policy knowledge base", "catalogue decision modal opens", 10_000);
    await shot(page, "catalogue-decision-modal");
    await page.getByRole("button", { name: "Cancel" }).click();
    record("catalogue decision cancelled (lab record untouched)", true);
  }

  // 9. Sign out
  await page.locator("header, [class*='top-navigation']").getByRole("button").last().click().catch(() => {});
  const signOut = page.getByRole("menuitem", { name: "Sign out" });
  if (await signOut.isVisible().catch(() => false)) {
    await signOut.click();
    await page.waitForLoadState("networkidle").catch(() => {});
    await page.waitForTimeout(2000);
    const backHome = page.url().startsWith(BASE_URL) || /amazoncognito/.test(page.url());
    record("sign out redirected through the Hosted UI logout", backHome, page.url());
  } else {
    record("sign out menu reachable", false, "menu item not found");
  }
} catch (error) {
  if (String(error).includes("__done__")) {
    record("persona walk-through finished", true, USERNAME);
  } else {
    record("unexpected failure", false, String(error).split("\n")[0]);
    await shot(page, "failure").catch(() => {});
  }
} finally {
  await browser.close();
}

const failures = results.filter((r) => !r.ok);
const summary = { baseUrl: BASE_URL, persona: USERNAME, write: WRITE, results, consoleErrors, consoleWarnings, failedRequests, pageErrors };
writeFileSync(join(SCREEN_DIR, "summary.json"), JSON.stringify(summary, null, 2));
console.log(`\n${results.length - failures.length}/${results.length} checks passed; console errors ${consoleErrors.length}, console warnings ${consoleWarnings.length}, page errors ${pageErrors.length}, failed API requests ${failedRequests.length}`);
for (const line of Array.from(new Set(consoleWarnings)).slice(0, 10)) console.log(`  warning: ${line}`);
for (const line of pageErrors) console.log(`  pageerror: ${line}`);
for (const line of failedRequests) console.log(`  request: ${line}`);
for (const line of consoleErrors.slice(0, 10)) console.log(`  console: ${line}`);
process.exit(failures.length || pageErrors.length ? 1 : 0);
