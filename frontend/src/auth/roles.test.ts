import { describe, expect, it } from "vitest";

import { can, canAccessScreen, displayRole, roleLabel, rolesOf } from "./roles";

describe("rolesOf", () => {
  it("prefers roles[] and de-duplicates", () => {
    expect(rolesOf({ role: "admin", roles: ["developer", "approver", "developer"] })).toEqual(["developer", "approver"]);
  });

  it("falls back to the display role and ignores unknown strings", () => {
    expect(rolesOf({ role: "approver" })).toEqual(["approver"]);
    expect(rolesOf({ role: "superuser", roles: ["guest"] })).toEqual([]);
    expect(rolesOf(null)).toEqual([]);
  });
});

describe("screen gating", () => {
  it("shows admins everything, approvers only Overview, Releases and Playground", () => {
    const admin = rolesOf({ roles: ["admin"] });
    const approver = rolesOf({ roles: ["approver"] });
    const developer = rolesOf({ roles: ["developer"] });
    expect(["overview", "domains", "catalogue", "governance", "releases", "playground"].every((s) =>
      canAccessScreen(admin, s as never),
    )).toBe(true);
    expect(canAccessScreen(approver, "governance")).toBe(false);
    expect(canAccessScreen(approver, "domains")).toBe(false);
    expect(canAccessScreen(approver, "releases")).toBe(true);
    expect(canAccessScreen(developer, "governance")).toBe(false);
    expect(canAccessScreen(developer, "catalogue")).toBe(true);
    expect(canAccessScreen([], "overview")).toBe(false);
  });
});

describe("action gating mirrors the router table", () => {
  it("keeps the release decisions approver-only (admins get 403)", () => {
    expect(can(["admin"], "releases:decide")).toBe(false);
    expect(can(["admin"], "releases:promote")).toBe(false);
    expect(can(["approver"], "releases:decide")).toBe(true);
    expect(can(["developer", "approver"], "releases:promote")).toBe(true);
  });

  it("lets developers create keys and releases but not manage keys or settings", () => {
    expect(can(["developer"], "keys:create")).toBe(true);
    expect(can(["developer"], "releases:create")).toBe(true);
    expect(can(["developer"], "keys:manage")).toBe(false);
    expect(can(["developer"], "settings:write")).toBe(false);
    expect(can(["developer"], "catalogue:decide")).toBe(false);
    expect(can(["admin"], "keys:manage")).toBe(true);
    expect(can(["admin"], "governance:write")).toBe(true);
  });

  it("lets every signed-in role invoke and send feedback", () => {
    expect(can(["approver"], "invoke")).toBe(true);
    expect(can(["approver"], "feedback")).toBe(true);
    expect(can([], "invoke")).toBe(false);
  });
});

describe("displayRole", () => {
  it("applies admin > developer > approver", () => {
    expect(displayRole({ roles: ["approver", "admin"] })).toBe("admin");
    expect(displayRole({ roles: ["approver", "developer"] })).toBe("developer");
    expect(displayRole({ roles: [] })).toBeNull();
    expect(roleLabel("approver")).toBe("Domain approver");
    expect(roleLabel(null)).toBe("No platform role");
  });
});
