import { expect, type Page } from "@playwright/test";
import { mkdirSync } from "node:fs";
import { join, resolve } from "node:path";

export type Persona = "admin" | "developer" | "approver";

export const BASE_URL = (process.env.BASE_URL ?? "http://localhost:5173").replace(/\/+$/, "");
export const SCREENSHOT_DIR = resolve(process.env.SCREENSHOT_DIR ?? "../.claude/docs/ui-screenshots");
export const LT_PROJECT = "lt-hr-assistant";
export const LT_DOMAIN = "lt-ops";
// the Dev key scripts/lifecycle_test.py creates for its blueprint model (its --model default; keep the two in step)
export const LT_DEV_KEY = `${LT_PROJECT}-dev-claude-sonnet-5`;
export const LT_DEV_GATEWAY = `${LT_PROJECT}-dev-gw`;

mkdirSync(SCREENSHOT_DIR, { recursive: true });

export function password(persona: Persona): string {
  const value = process.env[`E2E_${persona.toUpperCase()}_PASSWORD`];
  if (!value) {
    throw new Error(`E2E_${persona.toUpperCase()}_PASSWORD is not set (see e2e/global-setup.ts)`);
  }
  return value;
}

export function username(persona: Persona): string {
  return process.env[`E2E_${persona.toUpperCase()}_USERNAME`] ?? persona;
}

/** Collects console errors / warnings and page errors; `assertClean()` fails the test when any were seen. */
export class PageMonitor {
  readonly consoleErrors: string[] = [];
  readonly consoleWarnings: string[] = [];
  readonly pageErrors: string[] = [];
  readonly failedApi: string[] = [];

  constructor(page: Page) {
    page.on("console", (message) => {
      const text = message.text();
      if (message.type() === "error") {
        this.consoleErrors.push(text.slice(0, 300));
      } else if (message.type() === "warning" && !/React Router Future Flag|Download the React DevTools/.test(text)) {
        this.consoleWarnings.push(text.slice(0, 300));
      }
    });
    page.on("pageerror", (error) => this.pageErrors.push(String(error).slice(0, 300)));
    page.on("response", (response) => {
      if (response.status() >= 500 && response.url().includes("execute-api")) {
        this.failedApi.push(`${response.request().method()} ${response.url()} -> ${response.status()}`);
      }
    });
  }

  assertClean(): void {
    // 4xx answers are expected in places (403 for the wrong persona, 409 from the state machine); 5xx never are.
    expect(this.pageErrors, "page errors").toEqual([]);
    expect(this.consoleErrors.filter((e) => !/Failed to load resource: the server responded with a status of 4\d\d/.test(e)), "console errors").toEqual([]);
    // Cloudscape 3.0.1396 dev-mode only: its TopNavigation utility menu passes aria-haspopup to its own Button (internal/components/menu-dropdown);
    // warnOnce is silent in production builds, so the CloudFront run never sees it. Everything else must be clean.
    expect(this.consoleWarnings.filter((w) => !/\[Button\] Overriding native attribute \[aria-haspopup\]/.test(w)), "console warnings").toEqual([]);
    expect(this.failedApi, "5xx API answers").toEqual([]);
  }
}

/** Sign in through the Cognito Hosted UI (classic form) as one persona; lands on `path`. */
export async function signIn(page: Page, persona: Persona, path = "/"): Promise<void> {
  await page.goto(`${BASE_URL}${path}`, { waitUntil: "networkidle" });
  await page.getByTestId("sign-in").click();
  await page.waitForURL(/amazoncognito\.com/, { timeout: 60_000 });
  await page.locator('input[name="username"]:visible').first().fill(username(persona));
  await page.locator('input[name="password"]:visible').first().fill(password(persona));
  await page.locator('input[name="password"]:visible').first().press("Enter");
  await page.waitForURL((url) => url.origin === new URL(BASE_URL).origin, { timeout: 90_000 });
  await page.waitForLoadState("networkidle").catch(() => {});
  await expect(page.locator("nav a").first()).toBeVisible({ timeout: 60_000 });
}

/** The ID token oidc-client-ts stored for the signed-in user (localStorage key `oidc.user:<authority>:<client>`). */
export async function idToken(page: Page): Promise<string> {
  const token = await page.evaluate(() => {
    for (let i = 0; i < localStorage.length; i += 1) {
      const key = localStorage.key(i) ?? "";
      if (key.startsWith("oidc.user:")) {
        const user = JSON.parse(localStorage.getItem(key) ?? "{}") as { id_token?: string };
        return user.id_token ?? "";
      }
    }
    return "";
  });
  expect(token, "ID token in localStorage").not.toBe("");
  return token;
}

export interface RuntimeConfig {
  apiUrl: string;
  region: string;
  userPoolId: string;
  clientId: string;
  authDomain: string;
}

export async function runtimeConfig(): Promise<RuntimeConfig> {
  const response = await fetch(`${BASE_URL}/config.json`);
  expect(response.status).toBe(200);
  return (await response.json()) as RuntimeConfig;
}

/** Minimal API client (the TypeScript twin of platform_sdk.api.PlatformClient for the test). */
export class Api {
  private readonly apiUrl: string;
  private readonly token: string;

  constructor(apiUrl: string, token: string) {
    this.apiUrl = apiUrl;
    this.token = token;
  }

  private async request<T>(method: string, path: string, body?: unknown): Promise<{ status: number; body: T }> {
    const response = await fetch(`${this.apiUrl}${path}`, {
      method,
      headers: { Authorization: `Bearer ${this.token}`, Accept: "application/json", ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    const text = await response.text();
    return { status: response.status, body: (text ? JSON.parse(text) : null) as T };
  }

  get<T>(path: string) {
    return this.request<T>("GET", path);
  }

  post<T>(path: string, body: unknown) {
    return this.request<T>("POST", path, body);
  }

  put<T>(path: string, body: unknown) {
    return this.request<T>("PUT", path, body);
  }
}

let counter = 0;
export const VIEWPORT = { width: 1440, height: 1000 };

async function reopenNavigation(page: Page, context: string): Promise<void> {
  const navLink = page.locator('nav a[href="/"]').first();
  if (await navLink.isVisible().catch(() => false)) {
    return;
  }
  const toggle = page.getByRole("button", { name: /open navigation|navigation/i }).first();
  if (await toggle.isVisible().catch(() => false)) {
    console.log(`[e2e] side navigation collapsed (${context}): re-opening it`);
    await toggle.click();
    await navLink.waitFor({ state: "visible", timeout: 5_000 }).catch(() => {});
  }
}

/** Numbered, full-page PNG into SCREENSHOT_DIR. */
export async function shot(page: Page, name: string): Promise<string> {
  counter += 1;
  const file = join(SCREENSHOT_DIR, `${String(counter).padStart(2, "0")}-${name}.png`);
  // Capture the whole page with the viewport grown to the document height (instead of Playwright's fullPage emulation): the
  // resize lets Cloudscape's toolbar AppLayout settle, and a side navigation it closed on the transient measurement (see the
  // step-1 notes) is re-opened before the capture, so every screenshot shows the same layout.
  await page.evaluate(() => window.scrollTo(0, 0));
  const height = await page.evaluate(() => Math.min(Math.max(document.documentElement.scrollHeight, 1000), 6000));
  await page.setViewportSize({ width: VIEWPORT.width, height });
  await page.waitForTimeout(400);
  await reopenNavigation(page, name);
  await page.screenshot({ path: file, fullPage: false });
  await page.setViewportSize(VIEWPORT);
  await page.waitForTimeout(200);
  await reopenNavigation(page, `${name} (after restoring the viewport)`);
  return file;
}

/** Click a side-navigation entry (same-page routing); falls back to a direct load. */
export async function nav(page: Page, href: string): Promise<void> {
  const link = page.locator(`nav a[href="${href}"]`).first();
  try {
    await link.click({ timeout: 10_000 });
  } catch {
    console.log(`[e2e] side navigation link ${href} not clickable (navigation collapsed?): loading it directly`);
    await page.goto(`${BASE_URL}${href}`, { waitUntil: "networkidle" });
  }
  await page.waitForLoadState("networkidle").catch(() => {});
}

export async function goto(page: Page, path: string): Promise<void> {
  await page.goto(`${BASE_URL}${path}`, { waitUntil: "networkidle" });
}

/** Cloudscape Select: open and pick the option whose text matches. */
export async function pickOption(page: Page, trigger: ReturnType<Page["locator"]>, optionText: string | RegExp): Promise<void> {
  await trigger.click();
  await page.getByRole("option", { name: optionText }).first().click();
}

export const PASSING_SCORES = {
  Correctness: { mean: 1.0, min: 1.0, max: 1.0, passRate: 1.0, count: 10, errors: 0, failed: 0 },
  GoalSuccessRate: { mean: 1.0, min: 1.0, max: 1.0, passRate: 1.0, count: 10, errors: 0, failed: 0 },
  TrajectoryInOrderMatch: { mean: 1.0, min: 1.0, max: 1.0, passRate: 1.0, count: 8, errors: 2, failed: 0 },
  Harmfulness: { mean: 1.0, min: 1.0, max: 1.0, passRate: 1.0, count: 10, errors: 0, failed: 0 },
};

/** The same synthetic RunSummary.to_api_body() look-alike scripts/lifecycle_test.py posts (passing variant). */
export function passingEvaluation(runId: string) {
  return {
    runId,
    datasetName: "hr-assistant.json",
    scenarioCount: 10,
    scores: PASSING_SCORES,
    results: [
      { scenarioId: "leave-balance-e1001", sessionId: "leave-balance-e1001-synthetic", status: "COMPLETED", scores: Object.fromEntries(Object.keys(PASSING_SCORES).map((k) => [k, 1.0])), labels: { Correctness: "Correct" }, error: null, errors: {} },
      { scenarioId: "policy-remote-work-days", sessionId: "policy-remote-work-days-synthetic", status: "COMPLETED", scores: Object.fromEntries(Object.keys(PASSING_SCORES).map((k) => [k, 1.0])), error: null, errors: {} },
    ],
    failedScenarios: [],
  };
}

export const NAV_SCREENS = ["Overview", "Domains and projects", "Catalogue", "Governance", "Releases", "Playground"];

/** The side-navigation entries (the top navigation and the navigation header are links too). */
export function sideNavigation(page: Page) {
  // the breadcrumb group is a <nav> too; the side navigation is the one with the "Control plane" header link
  return page.locator("nav").filter({ hasText: "Control plane" });
}

export async function expectNavEntries(page: Page, expected: string[]): Promise<void> {
  await expect
    .poll(async () => (await sideNavigation(page).locator("a").allInnerTexts()).map((t) => t.trim()).filter((t) => NAV_SCREENS.includes(t)), { timeout: 30_000 })
    .toEqual(expected);
}
