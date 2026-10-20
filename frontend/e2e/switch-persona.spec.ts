/**
 * The "Switch persona" menu (user menu, top right): a signed-in learner becomes another persona without a password. Signs in
 * once as developer through the Hosted UI, switches to approver and then to admin, and checks that the navigation, the ID token,
 * the confirmation and the role notice follow the new persona while the page stays where it was.
 */
import { expect, test, type Page } from "@playwright/test";

import { PageMonitor, expectNavEntries, goto, idToken, shot, signIn } from "./helpers";

function claimsOf(jwt: string): Record<string, unknown> {
  const payload = jwt.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
  return JSON.parse(Buffer.from(payload, "base64").toString("utf8")) as Record<string, unknown>;
}

/** Opens the user menu: the last button of the top navigation (the identity is a link). */
async function openUserMenu(page: Page): Promise<void> {
  await page.locator("#app-top-navigation button").last().click();
  await expect(page.getByRole("menuitem", { name: "Sign out" })).toBeVisible();
}

async function switchTo(page: Page, label: string): Promise<void> {
  await openUserMenu(page);
  await page.getByRole("menuitem", { name: label }).click();
  // the page loads again under the new persona; the Shell confirms the switch once
  await expect(page.getByText(`You are now ${label}`).first()).toBeVisible({ timeout: 60_000 });
}

test("switch persona: developer -> approver -> admin without a password, staying on the page", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  const monitor = new PageMonitor(page);
  await signIn(page, "developer", "/releases");
  await expectNavEntries(page, ["Overview", "Domains and projects", "Catalogue", "Releases", "Playground"]);

  await openUserMenu(page);
  await expect(page.getByRole("menuitem", { name: "Agent developer (developer)" })).toHaveAttribute("aria-disabled", "true");
  await expect(page.getByRole("menuitem", { name: "Domain approver (approver)" })).toBeEnabled();
  await shot(page, "switch-persona-menu");
  await page.keyboard.press("Escape");

  await switchTo(page, "Domain approver (approver)");
  await expect(page).toHaveURL(/\/releases$/);
  await expectNavEntries(page, ["Overview", "Releases", "Playground"]);
  expect(claimsOf(await idToken(page))["cognito:username"]).toBe("approver");
  await goto(page, "/domains");
  await expect(page.getByText(/Switch persona to admin or developer from the user menu/).first()).toBeVisible();
  await shot(page, "switch-persona-approver-role-notice");

  await switchTo(page, "Platform admin (admin)");
  await expect(page).toHaveURL(/\/domains$/);
  await expectNavEntries(page, ["Overview", "Domains and projects", "Catalogue", "Governance", "Releases", "Playground"]);
  expect(claimsOf(await idToken(page))["cognito:username"]).toBe("admin");
  await expect(page.getByTestId("create-project")).toBeVisible();

  monitor.assertClean();
  await context.close();
});
