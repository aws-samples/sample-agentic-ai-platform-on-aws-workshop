/**
 * Role gating (build contract section 4 and ui-design.md). The API enforces authorization in its route table; the UI mirrors
 * it so that buttons and navigation entries only appear for roles that can use them. Gate on `roles[]` (a user in several
 * Cognito groups holds every role), never on the single display `role`.
 */
export type Role = "admin" | "developer" | "approver";

export const ROLES: ReadonlyArray<Role> = ["admin", "developer", "approver"];

export type Screen = "overview" | "domains" | "catalogue" | "governance" | "releases" | "playground";

export type Action =
  | "domains:write" // POST / DELETE /domains
  | "projects:write" // POST / PATCH / DELETE /projects
  | "resources:write" // PUT / DELETE resources
  | "keys:create" // POST /projects/{id}/keys
  | "keys:manage" // PATCH /projects/{id}/keys/{env}/{model} (raise cap, suspend, resume)
  | "blueprints:create" // POST blueprints
  | "deploy" // POST /projects/{id}/deploy
  | "releases:create" // POST releases, POST evaluation
  | "releases:decide" // approve / reject (approver only: admins get 403, the module 6 teaching point)
  | "releases:promote" // promote (approver only)
  | "catalogue:decide" // POST /catalogue/{id}/status
  | "governance:write" // POST /governance/gateways/{id}/mode
  | "settings:write" // PUT /settings
  | "invoke" // POST /invoke
  | "feedback"; // POST /feedback

const ALL: ReadonlyArray<Role> = ROLES;

/** Navigation entries per role (ui-design.md): approvers see Overview, Releases and Playground only. */
export const SCREEN_ROLES: Record<Screen, ReadonlyArray<Role>> = {
  overview: ALL,
  domains: ["admin", "developer"],
  catalogue: ["admin", "developer"],
  governance: ["admin"],
  releases: ALL,
  playground: ALL,
};

/** Mirror of backend/src/platform_api/router.py ROUTES. */
export const ACTION_ROLES: Record<Action, ReadonlyArray<Role>> = {
  "domains:write": ["admin"],
  "projects:write": ["admin"],
  "resources:write": ["developer", "admin"],
  "keys:create": ["developer", "admin"],
  "keys:manage": ["admin"],
  "blueprints:create": ["developer", "admin"],
  deploy: ["developer", "admin"],
  "releases:create": ["developer", "admin"],
  "releases:decide": ["approver"],
  "releases:promote": ["approver"],
  "catalogue:decide": ["admin"],
  "governance:write": ["admin"],
  "settings:write": ["admin"],
  invoke: ALL,
  feedback: ALL,
};

export interface MeLike {
  role?: string | null;
  roles?: ReadonlyArray<string> | null;
}

function isRole(value: unknown): value is Role {
  return typeof value === "string" && (ROLES as ReadonlyArray<string>).includes(value);
}

/** Every role of the signed-in user: `roles[]` from GET /me, falling back to the display `role` for older backends. */
export function rolesOf(me: MeLike | null | undefined): Role[] {
  if (!me) {
    return [];
  }
  const fromList = (me.roles ?? []).filter(isRole);
  if (fromList.length) {
    return Array.from(new Set(fromList));
  }
  return isRole(me.role) ? [me.role] : [];
}

export function hasAnyRole(roles: ReadonlyArray<Role>, allowed: ReadonlyArray<Role>): boolean {
  return roles.some((role) => allowed.includes(role));
}

export function canAccessScreen(roles: ReadonlyArray<Role>, screen: Screen): boolean {
  return hasAnyRole(roles, SCREEN_ROLES[screen]);
}

export function can(roles: ReadonlyArray<Role>, action: Action): boolean {
  return hasAnyRole(roles, ACTION_ROLES[action]);
}

/** The display role: admin > developer > approver (same precedence as the backend's `role`). */
export function displayRole(me: MeLike | null | undefined): Role | null {
  const roles = rolesOf(me);
  for (const role of ROLES) {
    if (roles.includes(role)) {
      return role;
    }
  }
  return null;
}

export const ROLE_LABELS: Record<Role, string> = {
  admin: "Platform admin",
  developer: "Agent developer",
  approver: "Domain approver",
};

export function roleLabel(role: Role | null | undefined): string {
  return role ? ROLE_LABELS[role] : "No platform role";
}
