"""The on-behalf-of chain of the learning path, built in three modules (design: .claude/docs/keycloak-obo-design.md).

  module 2  the corporate IdP: stack AgentPlatform-Idp (Keycloak on Fargate behind API Gateway, the HR API), the realm check, the
            employee of the record, two AgentCore Identity OAuth2 credential providers with on-behalf-of token exchange
  module 4  the on-behalf-of gateway (CUSTOM_JWT from the realm) with the HR API as an OpenAPI target (OAUTH TOKEN_EXCHANGE)
  module 6  the on-behalf-of harness: the Dev render with a JWT authorizer and the gateway tool exchanging the employee's token

Every step is idempotent and reconciles what exists (a moved IdP URL updates the gateway authorizer and the target schema). The Dev
and Prod objects of modules 1 to 7 are untouched: the OBO objects sit next to them with the `_obo` / `-obo` suffix (platform_sdk.names).
`workshop obo` walks whatever part of the chain exists.
"""

from __future__ import annotations

import json
import time

import boto3
import requests
from botocore.exceptions import ClientError

from platform_sdk import agentcore, blueprint, hrapi, identity, keycloak, names
from platform_sdk import config as sdk_config

from .apply_foundation import new_password
from .context import Context, WorkshopError, head, look, ok, say, warn

RECORD_TAGS = {
    "Project": "workshop-agent-platform",
    "ManagedBy": "workshop",
    "Module": "6",
}  # the OBO harness: attendee-built, like apply_4
HARNESS_WAIT_NOTE = (
    "a new harness version is READY in 20 to 60 s; the first VPC-mode deploy of the day can take 4 to 5 min"
)


# ---- 1  the stack -------------------------------------------------------------------------------------------------------------
def deploy_idp(ctx: Context, deploy: bool) -> None:
    from . import cdk

    if not deploy:
        say("  --no-deploy: the stack is not deployed; using whatever AgentPlatform-Idp publishes already")
        return
    seconds = cdk.deploy(ctx.root, "idp", region=ctx.region_override)
    ctx.mark("idp_deploy", seconds=round(seconds))
    sdk_config.reset_cache()
    ctx._cfg = None  # re-read SSM: idp/* and hr-api/* are new


def require(cfg, key: str) -> str:
    value = cfg.get(key)
    if not value:
        raise WorkshopError(
            f"SSM /agent-platform/{key} is missing: deploy the Idp stack first (`workshop apply 2` without --no-deploy)"
        )
    return value


# ---- 2  the realm -----------------------------------------------------------------------------------------------------------
def employee_record(ctx: Context, employee_id: str) -> dict:
    rows = json.loads((ctx.scenario_dir / "data" / "employees.json").read_text(encoding="utf-8"))
    for row in rows:
        if row.get("employee_id") == employee_id:
            return row
    raise WorkshopError(f"module_2.inputs.employee {employee_id!r} is not in scenario/data/employees.json")


def ensure_realm(ctx: Context, cfg, employee: dict) -> dict:
    """Check the imported realm, read the client secrets, create the employee; returns what the later steps need."""
    base = require(cfg, "idp/url")
    realm = cfg.get("idp/realm") or names.KC_REALM
    secrets_manager = boto3.client("secretsmanager", region_name=cfg.region)
    waited = keycloak.wait_ready(base)
    say(f"  Keycloak answers at {base} ({waited:.0f} s waited)")
    public = requests.get(f"{base}/realms/{realm}", timeout=30)
    if public.status_code != 200:
        raise WorkshopError(
            f"realm {realm} is missing (HTTP {public.status_code}): the Keycloak task imports foundation/keycloak/realm.json at "
            "start; read the realm-init and keycloak streams of the /agent-platform/keycloak log group"
        )
    specs = keycloak.workshop_clients(names.KC_EMPLOYEE_CLAIM)
    clients = {key: spec.client_id for key, spec in specs.items()}
    for spec in specs.values():
        flags = [
            "public" if spec.public else "confidential",
            *(["token exchange"] if spec.token_exchange else []),
            *(["direct access grants"] if spec.direct_access_grants else []),
        ]
        aud = f" -> aud {spec.audiences}" if spec.audiences else " (audience only)"
        say(f"  client {spec.client_id:<16} {', '.join(flags):<40}{aud}")
    secrets = {
        "agent": secrets_manager.get_secret_value(SecretId=require(cfg, "idp/secret-hr-agent-arn"))["SecretString"],
        "gateway": secrets_manager.get_secret_value(SecretId=require(cfg, "idp/secret-hr-obo-gateway-arn"))[
            "SecretString"
        ],
    }
    say("  client secrets: Secrets Manager (Keycloak resolved the same values when it imported the realm)")

    admin_password = secrets_manager.get_secret_value(SecretId=require(cfg, "idp/admin-secret-arn"))["SecretString"]
    admin = keycloak.Admin(base, cfg.get("idp/admin-username") or "admin", admin_password)
    if admin.harden_master():
        say(
            "  master realm: brute-force protection and event logging switched on (the admin console is on the internet)"
        )
    username = employee["email"].split("@")[0]
    first, _, last = employee["name"].partition(" ")
    users_secret_arn = cfg.users_secret_arn
    try:
        current = json.loads(secrets_manager.get_secret_value(SecretId=users_secret_arn).get("SecretString") or "{}")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise
        current = {}
    stored = current.get("employee") or {}
    password = stored.get("password") if stored.get("username") == username else None
    password = password or new_password()
    admin.ensure_user(
        realm,
        username,
        employee["email"],
        first,
        last or "",
        {names.KC_EMPLOYEE_CLAIM: employee["employee_id"], "department": employee.get("department", "")},
        password,
    )
    record = {
        "username": username,
        "password": password,
        "email": employee["email"],
        "employee_id": employee["employee_id"],
        "realm": realm,
        "client_id": names.KC_CLIENT_PORTAL,
    }
    secrets_manager.put_secret_value(
        SecretId=users_secret_arn, SecretString=json.dumps({**current, "employee": record})
    )
    say(
        f"  user {username} ({employee['employee_id']}, {employee['name']}): password in the users secret under `employee`"
    )
    return {
        "base": base,
        "realm": realm,
        "issuer": keycloak.issuer(base, realm),
        "discovery": keycloak.discovery_url(base, realm),
        "clients": clients,
        "secrets": secrets,
        "employee": record,
    }


# ---- 3  the credential providers ------------------------------------------------------------------------------------------
def ensure_providers(ctx: Context, kc: dict) -> dict:
    providers = {}
    for key, name in (("agent", names.OBO_PROVIDER_AGENT), ("gateway", names.OBO_PROVIDER_GATEWAY)):
        started = time.monotonic()
        provider = identity.ensure_oauth2_provider(
            name,
            kc["discovery"],
            kc["clients"][key],
            kc["secrets"][key],
            tags={"ManagedBy": "workshop", "Module": "2"},
            region=ctx.region,
        )
        providers[key] = provider
        say(
            f"  {name:<18} client {kc['clients'][key]:<16} TOKEN_EXCHANGE, actor token NONE  {time.monotonic() - started:4.1f} s"
        )
    return providers


# ---- 4  the role policies ------------------------------------------------------------------------------------------------------
def obo_policy(cfg, workload_pattern: str, provider: dict, api_key: bool = False) -> dict:
    """What a harness or gateway needs for OAuth outbound through one provider (harness-security.html and gateway-outbound-auth.html,
    read 2026-10-16): workload tokens on its own workload identity, the exchange on the provider, the provider's secret.

    `api_key` (harness only): the LiteLLM key of the model is fetched under the same workload identity, and the role template only
    names the Dev harness's identity `harness_<snake>_<env>-*`, which the `_obo` name does not match (denied live, 2026-10-16)."""
    arn = f"arn:aws:bedrock-agentcore:{cfg.region}:{cfg.account_id}"
    statements = [
        {
            "Sid": "WorkloadTokens",
            "Effect": "Allow",
            "Action": [
                "bedrock-agentcore:GetWorkloadAccessToken",
                "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                *(["bedrock-agentcore:GetResourceApiKey"] if api_key else []),
            ],
            "Resource": [
                f"{arn}:workload-identity-directory/default",
                f"{arn}:workload-identity-directory/default/workload-identity/{workload_pattern}",
            ],
        },
        {
            "Sid": "OnBehalfOfExchange",
            "Effect": "Allow",
            "Action": ["bedrock-agentcore:GetResourceOauth2Token"],
            "Resource": [
                f"{arn}:workload-identity-directory/default",
                f"{arn}:workload-identity-directory/default/workload-identity/{workload_pattern}",
                f"{arn}:token-vault/default",
                provider["arn"],
            ],
        },
    ]
    if provider.get("secretArn"):
        statements.append(
            {
                "Sid": "ProviderSecret",
                "Effect": "Allow",
                "Action": ["secretsmanager:GetSecretValue"],
                "Resource": provider["secretArn"],
            }
        )
    return {"Version": "2012-10-17", "Statement": statements}


def ensure_gateway_role_policy(ctx: Context, project: dict, provider: dict) -> None:
    """The Dev gateway role may fetch its workload token and exchange through the gateway's provider (module 4)."""
    _put_role_policy(
        ctx, project["roles"]["dev"]["gatewayRoleArn"], f"{ctx.names('dev').obo_gateway}-*", provider, "gateway", False
    )


def ensure_agent_role_policy(ctx: Context, project: dict, provider: dict) -> None:
    """The Dev agent role may fetch workload tokens and the model key under the OBO harness's identity and exchange through the
    agent's provider (module 6)."""
    _put_role_policy(
        ctx,
        project["roles"]["dev"]["agentRoleArn"],
        f"harness_{ctx.names('dev').obo_harness}-*",
        provider,
        "harness",
        True,
    )


def _put_role_policy(ctx: Context, role_arn: str, pattern: str, provider: dict, who: str, api_key: bool) -> None:
    cfg = ctx.cfg
    iam = boto3.client("iam", region_name=cfg.region)
    role_name = role_arn.rsplit("/", 1)[-1]
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName=names.OBO_ROLE_POLICY,
        PolicyDocument=json.dumps(obo_policy(cfg, pattern, provider, api_key=api_key)),
    )
    say(f"  {role_name}: inline policy {names.OBO_ROLE_POLICY} ({who} -> {provider['name']})")


# ---- 5  the gateway and its OpenAPI target ----------------------------------------------------------------------------------
def ensure_obo_gateway(ctx: Context, project: dict, kc: dict, providers: dict) -> tuple[dict, dict]:
    cfg = ctx.cfg
    n = ctx.names("dev")
    started = time.monotonic()
    gateway = agentcore.ensure_gateway(
        n.obo_gateway,
        project["roles"]["dev"]["gatewayRoleArn"],
        description=f"{project['name']} tools (dev) for on-behalf-of callers: bearer JWTs from {kc['realm']}",
        tags={"ProjectId": ctx.project_id, "Environment": "dev", "Module": "4"},
        region=cfg.region,
        authorizer_type="CUSTOM_JWT",
        authorizer_configuration=agentcore.jwt_authorizer(kc["discovery"], allowed_audience=[names.KC_CLIENT_GATEWAY]),
    )
    say(
        f"  {n.obo_gateway:<28} {gateway['status']:<6} {gateway['authorizerType']} (aud {names.KC_CLIENT_GATEWAY})  {time.monotonic() - started:4.1f} s"
        + ("  (JWT authorizer updated to the current realm)" if gateway.get("reconciled") else "")
    )
    if gateway["authorizerType"] != "CUSTOM_JWT":
        raise WorkshopError(
            f"gateway {n.obo_gateway} exists with inbound auth {gateway['authorizerType']}; delete it and run again"
        )
    api_url = require(cfg, "hr-api/url")
    tools = hrapi.load_tools(ctx.scenario_dir / "tools" / "hr" / "schema.json")
    started = time.monotonic()
    target = agentcore.ensure_openapi_target(
        gateway["gatewayId"],
        "hrapi",
        hrapi.spec_json(api_url, tools),
        agentcore.oauth_credential_configuration(
            providers["gateway"]["arn"],
            custom_parameters={**identity.OBO_CUSTOM_PARAMETERS, "audience": names.KC_CLIENT_HR_API},
        ),
        description="AnyCompany HR tools behind API Gateway (JWT authorizer); called with an on-behalf-of token",
        region=cfg.region,
    )
    say(
        f"  target hrapi {target['status']:<6} OpenAPI {api_url} ({len(tools)} operations), OAUTH TOKEN_EXCHANGE -> aud {names.KC_CLIENT_HR_API}  {time.monotonic() - started:.1f} s"
        + ("  (schema and credentials updated)" if target.get("reconciled") else "")
    )
    return gateway, target


# ---- 6  the harness ----------------------------------------------------------------------------------------------------------
def latest_blueprint_version(ctx: Context) -> int:
    versions = [
        int(b["version"]) for b in ctx.developer.list_blueprints(ctx.project_id) if b.get("version") is not None
    ]
    if not versions:
        raise WorkshopError(
            "no blueprint stored for the project: run `workshop apply 6` first (the OBO harness reuses its render)"
        )
    return max(versions)


def ensure_obo_harness(ctx: Context, kc: dict, providers: dict, gateway: dict) -> dict:
    cfg = ctx.cfg
    n = ctx.names("dev")
    version = latest_blueprint_version(ctx)
    rendered = ctx.developer.render_blueprint(ctx.project_id, version, "dev")
    spec = blueprint.with_jwt_authorizer(rendered, kc["discovery"], [names.KC_CLIENT_AGENT])
    spec = blueprint.with_oauth_gateway(spec, gateway["gatewayArn"], providers["agent"]["arn"], names.KC_CLIENT_GATEWAY)
    spec["memory"] = {"disabled": {}}  # the demo is one question; memory would need an actor id per employee
    spec["harnessName"] = n.obo_harness
    say(
        f"  render of blueprint v{version} for dev, patched: customJWTAuthorizer (aud {names.KC_CLIENT_AGENT}), gateway tool -> {n.obo_gateway} with OAuth TOKEN_EXCHANGE (aud {names.KC_CLIENT_GATEWAY}), memory off"
    )
    say(f"  {HARNESS_WAIT_NOTE}")
    started = time.monotonic()
    harness = agentcore.create_or_update_harness(
        n.obo_harness, spec, tags={**RECORD_TAGS, "ProjectId": ctx.project_id, "Environment": "dev"}, region=cfg.region
    )
    say(
        f"  {n.obo_harness:<28} {harness['status']:<6} version {harness.get('harnessVersion')}  {time.monotonic() - started:.0f} s"
    )
    return {**harness, "blueprintVersion": version}


# ---- 7  registration ----------------------------------------------------------------------------------------------------------
def remember(ctx: Context, **parts) -> dict:
    """Merge `parts` into the `obo` entry of the state file (modules 2, 4 and 6 each add their piece)."""
    current = ctx.state.get("obo") or {}
    ctx.state.set("obo", {**current, **parts})
    return ctx.state.get("obo")


def register_target(ctx: Context, provider: dict, gateway: dict, target: dict) -> None:
    """Register the HR API target with the control plane (type gateway_target, kind openapi).

    The gateway itself is deliberately not registered: `blueprint.render` picks the first `gateway` resource of the environment,
    and a second one could be rendered into the Dev harness; the OBO gateway travels in the target's and harness's attributes.
    """
    n = ctx.names("dev")
    ctx.developer.register_resource(
        ctx.project_id,
        "dev",
        "gateway_target",
        "hrapi",
        id=target["targetId"],
        attributes={
            "kind": "openapi",
            "gatewayArn": gateway["gatewayArn"],
            "gatewayName": n.obo_gateway,
            "credentialProvider": provider["arn"],
            "grantType": "TOKEN_EXCHANGE",
            "audience": names.KC_CLIENT_HR_API,
            "tools": [
                f"hrapi___{t['name']}" for t in hrapi.load_tools(ctx.scenario_dir / "tools" / "hr" / "schema.json")
            ],
        },
    )


def register_harness(ctx: Context, issuer: str, provider: dict, gateway: dict, harness: dict) -> None:
    """Register the OBO harness with the control plane (type harness; its name keeps it apart from the Dev harness)."""
    n = ctx.names("dev")
    ctx.developer.register_resource(
        ctx.project_id,
        "dev",
        "harness",
        n.obo_harness,
        harness["arn"],
        id=harness["harnessId"],
        attributes={
            "version": str(harness.get("harnessVersion")),
            "blueprintVersion": harness["blueprintVersion"],
            "authorizer": "CUSTOM_JWT",
            "issuer": issuer,
            "audience": names.KC_CLIENT_AGENT,
            "credentialProvider": provider["arn"],
            "gatewayArn": gateway["gatewayArn"],
            "gatewayName": n.obo_gateway,
            "onBehalfOf": True,
        },
    )


# ---- what the later modules need from the earlier ones --------------------------------------------------------------------
def idp_facts(ctx: Context) -> dict:
    """base, realm, issuer and discovery URL of the corporate IdP (SSM, published by the Idp stack of module 2)."""
    cfg = ctx.cfg
    base = cfg.get("idp/url")
    if not base:
        raise WorkshopError(
            "the corporate IdP is not deployed (no SSM /agent-platform/idp/url): run `workshop apply 2` first"
        )
    realm = cfg.get("idp/realm") or names.KC_REALM
    return {
        "base": base,
        "realm": realm,
        "issuer": keycloak.issuer(base, realm),
        "discovery": keycloak.discovery_url(base, realm),
    }


def provider_facts(ctx: Context) -> dict:
    """The two OAuth2 credential providers of module 2 as {agent, gateway} -> {name, arn, secretArn}."""
    providers = {}
    for key, name in (("agent", names.OBO_PROVIDER_AGENT), ("gateway", names.OBO_PROVIDER_GATEWAY)):
        provider = identity.get_oauth2_provider(name, region=ctx.region)
        if not provider:
            raise WorkshopError(f"OAuth2 credential provider {name} is missing: run `workshop apply 2` first")
        providers[key] = provider
    return providers


def gateway_facts(ctx: Context) -> dict:
    """The OBO gateway of module 4 as GetGateway returns it."""
    gateway = agentcore.find_gateway(ctx.names("dev").obo_gateway, ctx.region)
    if not gateway:
        raise WorkshopError(f"gateway {ctx.names('dev').obo_gateway} is missing: run `workshop apply 4` first")
    return gateway


# ---- the three module entry points -----------------------------------------------------------------------------------------------
def module_2(ctx: Context, deploy: bool = True) -> None:
    """The corporate IdP, the employee and the two credential providers (the end of `workshop apply 2`)."""
    employee = employee_record(ctx, str(ctx.inputs(2)["employee"]))
    head("the corporate IdP: stack AgentPlatform-Idp (Keycloak on Fargate behind API Gateway; the HR API for module 4)")
    deploy_idp(ctx, deploy)
    cfg = ctx.cfg
    head(
        f"the realm {cfg.get('idp/realm') or names.KC_REALM}: the imported clients and mappers, the employee {employee['employee_id']}"
    )
    kc = ensure_realm(ctx, cfg, employee)
    look(
        "Keycloak admin console",
        f"{kc['base']}/admin/master/console/#/{kc['realm']}/clients  (user admin; password in the secret agent-platform/keycloak-admin)",
    )
    head("AgentCore Identity: two OAuth2 credential providers with on-behalf-of token exchange")
    providers = ensure_providers(ctx, kc)
    look("AgentCore console", ctx.console("bedrock-agentcore/home#/identity/oauth-clients"))
    remember(
        ctx,
        issuer=kc["issuer"],
        discovery=kc["discovery"],
        realm=kc["realm"],
        clients=kc["clients"],
        employee={k: v for k, v in kc["employee"].items() if k != "password"},
        providers={k: {"name": p["name"], "arn": p["arn"]} for k, p in providers.items()},
    )
    ok(
        "the employee can sign in at the corporate IdP; AgentCore Identity can exchange that token on the employee's behalf"
    )
    say(
        "\nnext: `workshop obo` (the employee's token, and the exchange by hand); the gateway and harness hops come in modules 4 and 6"
    )
    warn(
        "Keycloak keeps its data in the task (start-dev, no database): after a task restart the realm is imported again but the "
        "employee is gone; run `workshop apply 2` again"
    )


def module_4(ctx: Context) -> None:
    """The on-behalf-of gateway and the HR API target (the end of `workshop apply 4`)."""
    kc = idp_facts(ctx)
    providers = provider_facts(ctx)
    project = ctx.developer.get_project(ctx.project_id)
    head("the on-behalf-of gateway: inbound JWT from the realm, the HR API as an OpenAPI target with token exchange")
    ensure_gateway_role_policy(ctx, project, providers["gateway"])
    gateway, target = ensure_obo_gateway(ctx, project, kc, providers)
    register_target(ctx, providers["gateway"], gateway, target)
    remember(
        ctx,
        gateway={"id": gateway["gatewayId"], "arn": gateway["gatewayArn"], "url": gateway["gatewayUrl"]},
        target={"id": target["targetId"], "name": "hrapi"},
    )
    say("  `workshop obo` now reaches the HR API through this gateway with an exchanged token (two hops)")


def module_6(ctx: Context) -> None:
    """The on-behalf-of harness (the end of `workshop apply 6`)."""
    kc = idp_facts(ctx)
    providers = provider_facts(ctx)
    gateway = gateway_facts(ctx)
    project = ctx.developer.get_project(ctx.project_id)
    head("the on-behalf-of harness: inbound JWT from the realm, the gateway tool with token exchange")
    ensure_agent_role_policy(ctx, project, providers["agent"])
    harness = ensure_obo_harness(ctx, kc, providers, gateway)
    register_harness(ctx, kc["issuer"], providers["agent"], gateway, harness)
    remember(
        ctx, harness={"id": harness["harnessId"], "arn": harness["arn"], "version": str(harness.get("harnessVersion"))}
    )
    say("  `workshop obo` now walks the whole chain: employee token -> harness -> gateway -> HR API")
