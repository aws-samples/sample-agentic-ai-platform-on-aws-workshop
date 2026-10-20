"""Who is calling: the Principal built from the Cognito ID-token claims the HTTP API JWT authorizer verified.

Claims live at `event.requestContext.authorizer.jwt.claims`. In HTTP API events `cognito:groups` arrives as a STRING such as
"[platform-admins]" or "[platform-admins approvers]" (verified 8 Oct 2026), occasionally as a list: `parse_groups` handles both.
Roles: `platform-admins` -> admin, `agent-developers` -> developer, `approvers` -> approver (same strings as `platform_sdk.auth`).
"""

from __future__ import annotations

from dataclasses import dataclass

from . import errors

ADMIN, DEVELOPER, APPROVER = "admin", "developer", "approver"
GROUP_ROLES = {"platform-admins": ADMIN, "agent-developers": DEVELOPER, "approvers": APPROVER}
# Route-table markers: PUBLIC needs no token, ANY needs a verified token (any group, even none)
PUBLIC = "public"
ANY = "any"


@dataclass(frozen=True)
class Principal:
    username: str
    email: str | None
    groups: tuple[str, ...]
    role: str | None  # the display role (admin > developer > approver); authorization uses every role of `roles`

    @property
    def roles(self) -> tuple[str, ...]:
        """Every platform role the user's groups grant (a developer who is also an approver may approve)."""
        return tuple(dict.fromkeys(GROUP_ROLES[g] for g in self.groups if g in GROUP_ROLES))

    def to_dict(self) -> dict:
        return {
            "username": self.username,
            "email": self.email,
            "groups": list(self.groups),
            "role": self.role,
            "roles": list(self.roles),
        }


def parse_groups(value) -> list[str]:
    """`cognito:groups` as a list: accepts a list, "[a b]", "[a, b]", "a,b" or None."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(g).strip() for g in value if str(g).strip()]
    text = str(value).strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return [g.strip().strip("\"'") for g in text.replace(",", " ").split() if g.strip().strip("\"'")]


def role_for(groups) -> str | None:
    """The platform role of a user in these groups; admin wins over developer over approver when several apply."""
    for group in ("platform-admins", "agent-developers", "approvers"):
        if group in groups:
            return GROUP_ROLES[group]
    return None


def principal_from_claims(claims: dict | None) -> Principal | None:
    if not claims:
        return None
    groups = parse_groups(claims.get("cognito:groups"))
    username = claims.get("cognito:username") or claims.get("username") or claims.get("sub") or ""
    return Principal(username=str(username), email=claims.get("email"), groups=tuple(groups), role=role_for(groups))


def principal_from_event(event: dict) -> Principal | None:
    """Principal of an HTTP API v2 event, or None when the route had no authorizer (GET /health, OPTIONS)."""
    authorizer = (event.get("requestContext") or {}).get("authorizer") or {}
    jwt = authorizer.get("jwt") or {}
    return principal_from_claims(jwt.get("claims"))


def require(principal: Principal | None, roles) -> Principal:
    """Raise 401 without a principal, 403 when none of the principal's roles is in `roles` (`ANY` accepts every signed-in user)."""
    if roles == PUBLIC:
        return principal  # type: ignore[return-value]
    if principal is None:
        raise errors.unauthorized()
    if roles == ANY:
        return principal
    allowed = (roles,) if isinstance(roles, str) else tuple(roles)
    if set(principal.roles) & set(allowed):
        return principal
    raise errors.forbidden(
        f"this route requires the role {' or '.join(allowed)}; {principal.username or 'the caller'} has "
        f"{'role ' + ' + '.join(principal.roles) if principal.roles else 'no platform role'} (groups {list(principal.groups)})"
    )
