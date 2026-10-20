"""Cognito sign-in for the three personas (build contract section 2.1).

`login("admin")` reads the persona's username and password from the users secret and signs in with
`USER_PASSWORD_AUTH` against the SPA app client (`login("someone@example.com", "password")` for any other user; the
platform configuration is loaded from SSM unless `cfg=` is given). The ID token claims are decoded locally without
signature verification: they are for display in module 2 (the platform API verifies the token itself).
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field

import boto3
from botocore.exceptions import ClientError

from .config import PlatformConfig
from .config import load as load_config

PERSONAS = ("admin", "developer", "approver")
GROUP_ROLES = {"platform-admins": "admin", "agent-developers": "developer", "approvers": "approver"}
# The canonical persona table. `workshop apply 2` (module 2 of the learning path) creates these groups and users in the pool the
# Auth stack deployed and stores the passwords in the users secret; foundation/config.py points here.
GROUP_DESCRIPTIONS = {
    "platform-admins": "Platform administrators: domains, projects, governance, catalogue approval",
    "agent-developers": "Agent developers: blueprints, deployments, releases",
    "approvers": "Domain approvers: approve, reject and promote releases",
}
PERSONA_USERS = {
    "admin": {"username": "admin", "email": "admin@anycompany.example", "group": "platform-admins"},
    "developer": {"username": "developer", "email": "developer@anycompany.example", "group": "agent-developers"},
    "approver": {"username": "approver", "email": "approver@anycompany.example", "group": "approvers"},
}


class AuthError(Exception):
    """Sign-in failed; the message says what to check."""


@dataclass
class Tokens:
    """Result of a Cognito sign-in. Send `id_token` as `Authorization: Bearer <id_token>` to the platform API."""

    id_token: str
    access_token: str
    refresh_token: str | None
    claims: dict
    username: str
    groups: list[str] = field(default_factory=list)
    expires_in: int = 3600

    @property
    def email(self) -> str | None:
        return self.claims.get("email")

    @property
    def role(self) -> str | None:
        """Platform role derived from the Cognito groups (admin, developer or approver); None when in no known group."""
        for group in self.groups:
            if group in GROUP_ROLES:
                return GROUP_ROLES[group]
        return None

    @property
    def auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.id_token}"}

    def summary(self) -> dict:
        """Compact, printable view of the token for module 2 (no secrets)."""
        return {
            "username": self.username,
            "email": self.email,
            "groups": self.groups,
            "role": self.role,
            "issuer": self.claims.get("iss"),
            "audience": self.claims.get("aud"),
            "expires": self.claims.get("exp"),
            "token_use": self.claims.get("token_use"),
        }


def decode_claims(jwt: str) -> dict:
    """Decode the payload of a JWT without verifying the signature (display only)."""
    try:
        payload = jwt.split(".")[1]
    except (AttributeError, IndexError) as exc:
        raise AuthError("not a JWT: expected three dot-separated base64url segments") from exc
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))


def _groups(claims: dict) -> list[str]:
    groups = claims.get("cognito:groups") or []
    if isinstance(groups, str):  # some decoders hand the claim over as "[a b]" or "a,b"
        groups = [g for g in groups.strip("[]").replace(",", " ").split() if g]
    return list(groups)


def personas(cfg: PlatformConfig, secrets_client=None) -> dict:
    """The users secret as a dict: {"admin": {"username", "password", "email"}, "developer": {...}, "approver": {...}}."""
    client = secrets_client or boto3.client("secretsmanager", region_name=cfg.region)
    try:
        secret = client.get_secret_value(SecretId=cfg.users_secret_arn)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            # a secret created without a value (the Auth stack ships it empty) has no AWSCURRENT version yet
            raise AuthError(
                f"the users secret {cfg.users_secret_arn} is empty or missing; `workshop apply 2` creates the personas and "
                "fills it (module 2 of the learning path)"
            ) from exc
        raise
    raw = secret.get("SecretString")
    if not raw:
        raise AuthError(
            f"the users secret {cfg.users_secret_arn} is empty; `workshop apply 2` creates the personas and fills it "
            "(module 2 of the learning path)"
        )
    return json.loads(raw)


def login(
    persona_or_username: str,
    password: str | None = None,
    cfg: PlatformConfig | None = None,
    cognito_client=None,
    secrets_client=None,
) -> Tokens:
    """Sign in as a persona ("admin" | "developer" | "approver") or as an explicit username with a password.

    `cfg` defaults to `config.load()` (build contract section 5: `auth.login("developer")` in the `workshop` command).
    """
    if isinstance(persona_or_username, PlatformConfig):
        raise TypeError(
            "login(cfg, persona) is now login(persona, cfg=cfg); the configuration is loaded from SSM by default"
        )
    cfg = cfg or load_config()
    if password is None:
        users = personas(cfg, secrets_client=secrets_client)
        if persona_or_username not in users:
            raise AuthError(
                f"unknown persona {persona_or_username!r}; known personas: {sorted(users)}. "
                "Pass password=... to sign in with an arbitrary username."
            )
        username = users[persona_or_username]["username"]
        password = users[persona_or_username]["password"]
    else:
        username = persona_or_username

    client = cognito_client or boto3.client("cognito-idp", region_name=cfg.region)
    try:
        response = client.initiate_auth(
            AuthFlow="USER_PASSWORD_AUTH",
            ClientId=cfg.client_id,
            AuthParameters={"USERNAME": username, "PASSWORD": password},
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        message = exc.response.get("Error", {}).get("Message", "")
        if code == "NotAuthorizedException":
            raise AuthError(
                f"Cognito refused the sign-in for {username!r} ({message}). Check the password in the users secret "
                f"({cfg.users_secret_arn}) and that the user is enabled in pool {cfg.user_pool_id}."
            ) from exc
        if code == "UserNotFoundException":
            raise AuthError(
                f"user {username!r} does not exist in pool {cfg.user_pool_id}; the Auth stack creates admin, developer and approver."
            ) from exc
        if code == "InvalidParameterException" and "USER_PASSWORD_AUTH" in message:
            raise AuthError(
                f"the app client {cfg.client_id} does not allow USER_PASSWORD_AUTH; enable ALLOW_USER_PASSWORD_AUTH on it."
            ) from exc
        raise
    if "ChallengeName" in response:
        raise AuthError(
            f"Cognito returned challenge {response['ChallengeName']} for {username!r} (for example NEW_PASSWORD_REQUIRED); "
            "set a permanent password with admin_set_user_password and retry."
        )
    result = response["AuthenticationResult"]
    claims = decode_claims(result["IdToken"])
    return Tokens(
        id_token=result["IdToken"],
        access_token=result.get("AccessToken", ""),
        refresh_token=result.get("RefreshToken"),
        claims=claims,
        username=claims.get("cognito:username") or username,
        groups=_groups(claims),
        expires_in=int(result.get("ExpiresIn", 3600)),
    )
