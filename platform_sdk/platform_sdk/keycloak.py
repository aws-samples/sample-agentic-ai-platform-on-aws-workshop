"""Keycloak as the corporate identity provider of module 2: realm set-up through the admin REST API, user logins, raw token
exchange and JWT inspection.

Why a module of its own: the workshop runs Keycloak (26.8.1, `start-dev`, H2 file database) on one Fargate task without a
custom image. The realm (clients, audience mappers, the token-exchange switch) is generated here into
foundation/keycloak/realm.json, which an init container writes into the import directory so Keycloak imports it at every
start; `workshop apply 2` then adds the employee of the record through the admin API, hardens the master realm and reads the
client secrets from Secrets Manager. Those clients, mappers and the switch are what makes an on-behalf-of (OBO) chain possible.

Facts the set-up rests on (keycloak.org docs for 26.8.1, read 2026-10-16):
- Standard Token Exchange (RFC 8693, feature `token-exchange-standard:v2`) is supported and on by default, same realm only. The
  requesting client must be confidential and carry the client attribute `standard.token.exchange.enabled = true`.
- The subject token's `aud` must already contain the requesting client, so the client that issues the user token carries an
  Audience mapper naming the exchanging client. The `audience` request parameter only narrows the audiences the requester can
  mint, which come from its own Audience mappers (`included.client.audience`).
- `subject_token_type` must be `urn:ietf:params:oauth:token-type:access_token`. Plain V2 exchange yields no `act` claim; the
  requester is visible as `azp`.
- Keycloak 24+ drops unmanaged user attributes unless the realm's user profile declares them, hence `ensure_user_profile`.
- Default access tokens carry no `aud` for the client they were issued to; every audience in this design comes from a mapper.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from . import names

TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"
TIMEOUT = 30


class KeycloakError(Exception):
    """A Keycloak call failed; the message carries the HTTP status and body."""


def decode_claims(token: str) -> dict:
    """The payload of a JWT without verifying the signature (for display and tests; the services verify)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception as exc:  # not a JWT
        raise KeycloakError(f"not a JWT: {exc}") from exc


def _check(response: requests.Response, what: str) -> requests.Response:
    if response.status_code >= 400:
        raise KeycloakError(f"{what}: HTTP {response.status_code} {response.text[:400]}")
    return response


def wait_ready(base_url: str, timeout: float = 600, every: float = 10) -> float:
    """Poll `GET /realms/master` until Keycloak answers 200; returns the seconds waited."""
    started = time.monotonic()
    last = ""
    while time.monotonic() - started < timeout:
        try:
            response = requests.get(f"{base_url}/realms/master", timeout=TIMEOUT)
            if response.status_code == 200:
                return time.monotonic() - started
            last = f"HTTP {response.status_code}"
        except requests.RequestException as exc:
            last = type(exc).__name__
        time.sleep(every)
    raise KeycloakError(f"Keycloak at {base_url} not ready after {timeout:.0f} s ({last})")


def password_grant(
    base_url: str,
    realm: str,
    client_id: str,
    username: str,
    password: str,
    scope: str = "openid",
    client_secret: str | None = None,
) -> dict:
    """Resource Owner Password grant (Direct Access Grants in Keycloak terms): the way the demo signs the employee in without a
    browser. Returns the token response (access_token, id_token, refresh_token, expires_in)."""
    data = {
        "grant_type": "password",
        "client_id": client_id,
        "username": username,
        "password": password,
        "scope": scope,
    }
    if client_secret:
        data["client_secret"] = client_secret
    response = requests.post(f"{base_url}/realms/{realm}/protocol/openid-connect/token", data=data, timeout=TIMEOUT)
    return _check(response, f"password grant for {username} on {client_id}").json()


def token_exchange(
    base_url: str,
    realm: str,
    client_id: str,
    client_secret: str,
    subject_token: str,
    audience: str | None = None,
    scope: str | None = None,
) -> dict:
    """RFC 8693 standard token exchange, exactly the request AgentCore Identity sends (minus its own client handling).

    `client_id`/`client_secret` are the exchanging (requester) client; `subject_token` the user's access token; `audience` the
    target client whose token is wanted. Returns the token response.
    """
    data = {
        "grant_type": TOKEN_EXCHANGE_GRANT,
        "client_id": client_id,
        "client_secret": client_secret,
        "subject_token": subject_token,
        "subject_token_type": ACCESS_TOKEN_TYPE,
        "requested_token_type": ACCESS_TOKEN_TYPE,
    }
    if audience:
        data["audience"] = audience
    if scope:
        data["scope"] = scope
    response = requests.post(f"{base_url}/realms/{realm}/protocol/openid-connect/token", data=data, timeout=TIMEOUT)
    return _check(response, f"token exchange by {client_id} for audience {audience}").json()


def discovery_url(base_url: str, realm: str) -> str:
    return f"{base_url}/realms/{realm}/.well-known/openid-configuration"


def issuer(base_url: str, realm: str) -> str:
    return f"{base_url}/realms/{realm}"


# ---- admin REST API ------------------------------------------------------------------------------------------------------
def audience_mapper(target_client_id: str) -> dict:
    """Protocol mapper adding `target_client_id` to the access token's `aud` claim."""
    return {
        "name": f"aud-{target_client_id}",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-audience-mapper",
        "consentRequired": False,
        "config": {
            "included.client.audience": target_client_id,
            "id.token.claim": "false",
            "access.token.claim": "true",
            "introspection.token.claim": "true",
        },
    }


def attribute_mapper(attribute: str, claim: str | None = None) -> dict:
    """Protocol mapper copying a user attribute into the tokens as `claim` (default: the attribute name)."""
    return {
        "name": claim or attribute,
        "protocol": "openid-connect",
        "protocolMapper": "oidc-usermodel-attribute-mapper",
        "consentRequired": False,
        "config": {
            "user.attribute": attribute,
            "claim.name": claim or attribute,
            "jsonType.label": "String",
            "id.token.claim": "true",
            "access.token.claim": "true",
            "userinfo.token.claim": "true",
            "introspection.token.claim": "true",
        },
    }


@dataclass
class ClientSpec:
    """What `Admin.ensure_client` makes of a client. `audiences` become Audience mappers; `claims` user-attribute mappers."""

    client_id: str
    name: str
    description: str = ""
    public: bool = False
    token_exchange: bool = False
    direct_access_grants: bool = False
    standard_flow: bool = False
    service_account: bool = False
    audiences: list[str] = field(default_factory=list)
    claims: list[str] = field(default_factory=list)
    redirect_uris: list[str] = field(default_factory=list)

    def representation(self) -> dict:
        mappers = [audience_mapper(a) for a in self.audiences] + [attribute_mapper(c) for c in self.claims]
        return {
            "clientId": self.client_id,
            "name": self.name,
            "description": self.description,
            "protocol": "openid-connect",
            "enabled": True,
            "publicClient": self.public,
            "bearerOnly": False,
            "standardFlowEnabled": self.standard_flow,
            "implicitFlowEnabled": False,
            "directAccessGrantsEnabled": self.direct_access_grants,
            "serviceAccountsEnabled": self.service_account and not self.public,
            "fullScopeAllowed": False,  # audiences come from the mappers only (and the switch is deprecated when left on)
            "redirectUris": self.redirect_uris,
            "webOrigins": [],
            "attributes": {"standard.token.exchange.enabled": "true" if self.token_exchange else "false"},
            "protocolMappers": mappers,
        }


# ---- the workshop realm (imported by Keycloak at start-up: foundation/keycloak/realm.json) -----------------------------------
REALM_FILE = (
    "foundation/keycloak/realm.json"  # relative to the repository root; regenerate with `realm_json()` (test checks)
)
SECRET_ENV = {
    names.KC_CLIENT_AGENT: "KC_SECRET_HR_AGENT",
    names.KC_CLIENT_GATEWAY: "KC_SECRET_HR_OBO_GATEWAY",
}  # client -> env var
EMPLOYEE_CLAIM = names.KC_EMPLOYEE_CLAIM


def workshop_clients(claim: str = EMPLOYEE_CLAIM) -> dict[str, ClientSpec]:
    """The four clients of the realm. The audience mappers are what makes each exchange legal in Keycloak: the subject token's
    `aud` must contain the exchanging client, and a client may only request audiences its own mappers can mint.

    Deliberately narrow: the portal has only the password grant the demo uses (no code flow, no redirect URIs, so no code
    interception surface) and the exchanging clients have no service account (a leaked client secret alone cannot mint a token the
    gateway or the HR API accepts; standard token exchange needs a user's token as the subject)."""
    return {
        "portal": ClientSpec(
            names.KC_CLIENT_PORTAL,
            "HR portal",
            "The employee-facing application: signs the employee in and receives the token the harness accepts",
            public=True,
            direct_access_grants=True,
            audiences=[names.KC_CLIENT_AGENT],
            claims=[claim],
        ),
        "agent": ClientSpec(
            names.KC_CLIENT_AGENT,
            "HR assistant (agent)",
            "AgentCore Identity authenticates as this client to exchange the employee's token for a gateway token",
            token_exchange=True,
            audiences=[names.KC_CLIENT_GATEWAY],
            claims=[claim],
        ),
        "gateway": ClientSpec(
            names.KC_CLIENT_GATEWAY,
            "AgentCore gateway (OBO)",
            "AgentCore Identity authenticates as this client to exchange the gateway token for an HR API token",
            token_exchange=True,
            audiences=[names.KC_CLIENT_HR_API],
            claims=[claim],
        ),
        "api": ClientSpec(
            names.KC_CLIENT_HR_API,
            "HR tools API",
            "An audience only: API Gateway accepts tokens minted for this client",
        ),
    }


def user_profile_config(extra_attributes: list[tuple[str, str]]) -> dict:
    """Keycloak's default declarative user profile plus the workshop attributes (Keycloak 24+ drops attributes it does not know)."""

    def attribute(name: str, display: str, required: bool = False, validations: dict | None = None) -> dict:
        entry: dict[str, Any] = {
            "name": name,
            "displayName": display,
            "permissions": {"view": ["admin", "user"], "edit": ["admin", "user"]},
            "multivalued": False,
        }
        if validations:
            entry["validations"] = validations
        if required:
            entry["required"] = {"roles": ["user"]}
        return entry

    attributes = [
        attribute(
            "username",
            "${username}",
            validations={
                "length": {"min": 3, "max": 255},
                "username-prohibited-characters": {},
                "up-username-not-idn-homograph": {},
            },
        ),
        attribute("email", "${email}", required=True, validations={"email": {}, "length": {"max": 255}}),
        attribute(
            "firstName",
            "${firstName}",
            required=True,
            validations={"length": {"max": 255}, "person-name-prohibited-characters": {}},
        ),
        attribute(
            "lastName",
            "${lastName}",
            required=True,
            validations={"length": {"max": 255}, "person-name-prohibited-characters": {}},
        ),
    ]
    for name, display in extra_attributes:
        entry = attribute(name, display)
        entry["permissions"] = {"view": ["admin", "user"], "edit": ["admin"]}
        attributes.append(entry)
    return {
        "attributes": attributes,
        "groups": [
            {
                "name": "user-metadata",
                "displayHeader": "User metadata",  # UPGroup fields: name, displayHeader, displayDescription, annotations
                "displayDescription": "Attributes, which refer to user metadata",
            }
        ],
        "unmanagedAttributePolicy": "ENABLED",
    }


def realm_representation(
    realm: str = names.KC_REALM,
    display_name: str = "AnyCompany",
    clients: dict[str, ClientSpec] | None = None,
    secret_env: dict[str, str] | None = None,
    claim: str = EMPLOYEE_CLAIM,
    access_token_lifespan: int = 900,
) -> dict:
    """The realm Keycloak imports at start-up (`--import-realm`): clients, mappers, the token-exchange switch, the user profile.

    Client secrets are `${ENV_VAR}` placeholders that Keycloak resolves from the container's environment at import time (the
    import/export guide: placeholders may reference any environment variable), so the secrets live in Secrets Manager and the
    realm file in the repository carries none. Users are not part of it: `workshop apply 2` creates the employee of the record.
    """
    clients = clients or workshop_clients(claim)
    secret_env = SECRET_ENV if secret_env is None else secret_env
    representations = []
    for spec in clients.values():
        rep = spec.representation()
        if spec.client_id in secret_env:
            rep["secret"] = "${" + secret_env[spec.client_id] + "}"
        representations.append(rep)
    return {
        "realm": realm,
        "displayName": display_name,
        "enabled": True,
        "sslRequired": "external",
        "accessTokenLifespan": access_token_lifespan,
        # the token endpoint is on the internet: lock accounts after repeated failures and keep an audit trail
        "bruteForceProtected": True,
        "permanentLockout": False,
        "failureFactor": 10,
        "eventsEnabled": True,
        "eventsExpiration": 7 * 24 * 3600,
        "adminEventsEnabled": True,
        "adminEventsDetailsEnabled": False,
        "clients": representations,
        "components": {
            "org.keycloak.userprofile.UserProfileProvider": [
                {
                    "providerId": "declarative-user-profile",
                    "subComponents": {},
                    "config": {
                        "kc.user.profile.config": [
                            json.dumps(user_profile_config([(claim, "Employee id"), ("department", "Department")]))
                        ]
                    },
                }
            ]
        },
    }


def realm_json(**kwargs) -> str:
    """`realm_representation` as the indented JSON stored in foundation/keycloak/realm.json."""
    return json.dumps(realm_representation(**kwargs), indent=2) + "\n"


class Admin:
    """A thin client of the Keycloak admin REST API, signed in as the bootstrap admin of the master realm."""

    def __init__(self, base_url: str, username: str, password: str, timeout: float = TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        response = self._session.post(
            f"{self.base_url}/realms/master/protocol/openid-connect/token",
            data={"grant_type": "password", "client_id": "admin-cli", "username": username, "password": password},
            timeout=timeout,
        )
        self._session.headers["Authorization"] = f"Bearer {_check(response, 'admin login').json()['access_token']}"

    # -- plumbing ----------------------------------------------------------------------------------------------------
    def _url(self, path: str) -> str:
        return f"{self.base_url}/admin/realms{path}"

    def get(self, path: str, **params) -> Any:
        response = self._session.get(self._url(path), params=params or None, timeout=self.timeout)
        if response.status_code == 404:
            return None
        return _check(response, f"GET {path}").json() if response.text else None

    def post(self, path: str, body: dict) -> requests.Response:
        return _check(self._session.post(self._url(path), json=body, timeout=self.timeout), f"POST {path}")

    def put(self, path: str, body: dict) -> requests.Response:
        return _check(self._session.put(self._url(path), json=body, timeout=self.timeout), f"PUT {path}")

    def delete(self, path: str) -> None:
        response = self._session.delete(self._url(path), timeout=self.timeout)
        if response.status_code != 404:
            _check(response, f"DELETE {path}")

    # -- realm -------------------------------------------------------------------------------------------------------
    def ensure_realm(self, realm: str, display_name: str, access_token_lifespan: int = 900) -> dict:
        """Create the realm when missing (enabled, 15-minute access tokens so exchanged tokens stay short-lived)."""
        existing = self.get(f"/{realm}")
        if existing:
            return existing
        self.post(
            "",
            {
                "realm": realm,
                "enabled": True,
                "displayName": display_name,
                "accessTokenLifespan": access_token_lifespan,
                "sslRequired": "external",
            },
        )
        return self.get(f"/{realm}")

    def ensure_user_profile_attribute(self, realm: str, attribute: str, display_name: str) -> bool:
        """Declare a custom user attribute in the realm's user profile (Keycloak 24+ drops undeclared attributes). True when added."""
        profile = self.get(f"/{realm}/users/profile") or {"attributes": []}
        if any(a.get("name") == attribute for a in profile.get("attributes", [])):
            return False
        profile.setdefault("attributes", []).append(
            {
                "name": attribute,
                "displayName": display_name,
                "permissions": {"view": ["admin", "user"], "edit": ["admin"]},
                "multivalued": False,
            }
        )
        self.put(f"/{realm}/users/profile", profile)
        return True

    # -- users -------------------------------------------------------------------------------------------------------
    def find_user(self, realm: str, username: str) -> dict | None:
        found = self.get(f"/{realm}/users", username=username, exact="true") or []
        return found[0] if found else None

    def ensure_user(
        self,
        realm: str,
        username: str,
        email: str,
        first_name: str,
        last_name: str,
        attributes: dict[str, str],
        password: str,
    ) -> dict:
        """Create or update the user (enabled, verified email, attributes) and set a permanent password."""
        body = {
            "username": username,
            "enabled": True,
            "email": email,
            "emailVerified": True,
            "firstName": first_name,
            "lastName": last_name,
            "attributes": {k: [v] for k, v in attributes.items()},
        }
        existing = self.find_user(realm, username)
        if existing:
            self.put(f"/{realm}/users/{existing['id']}", {**existing, **body})
            user = existing
        else:
            self.post(f"/{realm}/users", body)
            user = self.find_user(realm, username)
            assert user is not None
        self.put(
            f"/{realm}/users/{user['id']}/reset-password", {"type": "password", "value": password, "temporary": False}
        )
        return user

    def harden_master(self) -> bool:
        """Brute-force protection and events on the master realm (its admin console is on the internet); True when changed."""
        master = self.get("/master") or {}
        wanted = {"bruteForceProtected": True, "failureFactor": 10, "eventsEnabled": True, "adminEventsEnabled": True}
        if all(master.get(k) == v for k, v in wanted.items()):
            return False
        self.put("/master", {**master, **wanted})
        return True

    def delete_realm(self, realm: str) -> None:
        self.delete(f"/{realm}")
