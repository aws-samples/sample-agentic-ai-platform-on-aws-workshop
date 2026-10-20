"""Modules 1 and 2: the learners deploy the landing zone and create the personas; the control plane was deployed before the day.

Module 1 (`workshop apply 1`): cdk deploy of Network, SharedServices and ModelGateway, then a look at what appeared (stacks, the
private VPC with its endpoints and no internet gateway, the model gateway answering through the relay, the SSM parameters, the
doctor). Module 2 (`workshop apply 2`): the three Cognito groups and persona users in the pool the Auth stack deployed, their
passwords in the users secret, then the sign-ins, the 403 and the agent role template.
"""

from __future__ import annotations

import json
import secrets
import string
import subprocess
import sys
import time

import boto3
from botocore.exceptions import ClientError

from platform_sdk import api, auth, litellm, names

from . import cdk
from .context import Context, WorkshopError, head, look, ok, say, warn


# ---- module 1 ---------------------------------------------------------------------------------------------------------------
def landing_zone(ctx: Context, run_doctor: bool = True, deploy: bool = True) -> None:
    if deploy:
        cdk.deploy(ctx.root, "landing-zone", region=ctx.region_override)
    cfg = ctx.cfg
    ec2 = boto3.client("ec2", region_name=cfg.region)

    head("the platform stacks, two chains (module 2 adds the corporate IdP stack)")
    statuses = cdk.print_statuses(cfg.region)
    missing = [n for n, s in statuses.items() if s is None]
    if missing:
        warn(f"not deployed: {missing}")
    else:
        ok("landing zone (yours, module 1) and control plane with sample data (deployed before the day)")
    look("CloudFormation console", ctx.console("cloudformation/home#/stacks?filteringText=AgentPlatform"))

    head("the one VPC: private, endpoints only (no internet gateway, no NAT gateway, no public IP)")
    vpcs = ec2.describe_vpcs(
        Filters=[{"Name": "tag:aws:cloudformation:stack-name", "Values": ["AgentPlatform-Network"]}]
    )["Vpcs"]
    if not vpcs:
        warn("no VPC tagged with the AgentPlatform-Network stack found")
    for vpc in vpcs:
        vpc_id = vpc["VpcId"]
        facts = vpc_facts(ec2, vpc_id)
        groups = ", ".join(f"{len(ids)} {group}" for group, ids in sorted(facts["subnets"].items()))
        say(f"  {vpc_id}  cidr {vpc['CidrBlock']}  subnets: {groups}  AZs {facts['zones']}")
        say(
            "  app subnets: the LiteLLM tasks, the harness network interfaces, the relay Lambda and the interface endpoints;"
        )
        say(
            "  data subnets: the database. Both are isolated: their route tables know the VPC and the S3 endpoint, nothing else"
        )
        if facts["internet_gateways"] or facts["nat_gateways"] or facts["public_ip_subnets"]:
            warn(
                f"a public path exists: {len(facts['internet_gateways'])} internet gateway(s), {len(facts['nat_gateways'])} NAT "
                f"gateway(s), {len(facts['public_ip_subnets'])} subnet(s) with public IPs; the workshop network expects none"
            )
        else:
            ok(
                "private posture: no internet gateway, no NAT gateway, no public IP; AWS services are reached over PrivateLink"
            )
        say(f"  {len(facts['interface_endpoints'])} interface endpoints: {', '.join(facts['interface_endpoints'])}")
        say(f"  {len(facts['gateway_endpoints'])} gateway endpoint(s): {', '.join(facts['gateway_endpoints'])}")
        look("VPC console", ctx.console(f"vpcconsole/home#VpcDetails:VpcId={vpc_id}"))
        look("Endpoints", ctx.console("vpcconsole/home#Endpoints:"))

    head("the model gateway answers (its load balancer is internal: the call goes through the relay Lambda)")
    url = cfg.get("model-gateway/url")
    if url:
        started = time.monotonic()
        try:
            admin = litellm.admin_client(cfg, litellm.read_master_key(cfg), timeout=20)
            body = admin.health()
            say(
                f"  GET {url.rstrip('/')}/health/liveliness via {admin.transport} -> {str(body)[:40]!r} in {time.monotonic() - started:.1f} s"
            )
            if admin.via_relay:
                say(
                    "  nothing outside the VPC can open a connection to the proxy; the relay (a Lambda in the app subnets) forwarded "
                    "this request and returned the answer, which is how the platform API, this CLI and the doctor reach it"
                )
            ok(
                f"approved aliases (SSM): {', '.join(cfg.model_aliases)}; guardrail {cfg.guardrail_id} v{cfg.guardrail_version} bound to each"
            )
        except (litellm.LiteLLMError, OSError) as exc:
            warn(
                f"the model gateway did not answer yet ({type(exc).__name__}: {str(exc)[:120]}); the Express service needs a "
                "minute after the stack completes"
            )
    else:
        warn("no model gateway URL in SSM: the ModelGateway stack is not deployed")

    head("what the platform published (SSM /agent-platform/*)")
    rows = [
        ("Web UI", cfg.get("ui/url")),
        ("Platform API", cfg.get("api/url")),
        ("Model gateway (OpenAI base)", (url.rstrip("/") + "/v1") if url else "-"),
        ("Model gateway reachability", "private: inside the VPC only" if cfg.model_gateway_private else "public"),
        ("Model gateway relay", cfg.model_gateway_relay_function_name or "- (no relay: public gateway)"),
        ("Network", "private (endpoints only)" if cfg.network_private else "public"),
        ("Baseline guardrail", f"{cfg.get('guardrail/id')} version {cfg.get('guardrail/version')}"),
        ("HR policy knowledge base", cfg.get("scenario/kb-id")),
        ("HR tools Lambda", cfg.get("scenario/tools/hr-lambda-arn")),
    ]
    for label, value in rows:
        say(f"  {label:<30} {value}")
    say(f"  {len(cfg.params)} parameters under /agent-platform/")

    if run_doctor:
        head("scripts/doctor.py (read-only checks of every deployed layer)")
        doctor = ctx.root / "scripts" / "doctor.py"
        if doctor.exists():
            completed = subprocess.run([sys.executable, str(doctor)], cwd=str(ctx.root), text=True, capture_output=True)
            say(completed.stdout.rstrip())
            if completed.returncode != 0:
                warn(f"doctor exited {completed.returncode}; read the FAIL rows above")
                if completed.stderr.strip():
                    say(completed.stderr.strip()[-1500:])
        else:
            warn("scripts/doctor.py not found")
    ctx.state.set("verified", {"account": cfg.account_id, "region": cfg.region})


def vpc_facts(ec2, vpc_id: str) -> dict:
    """What module 1 looks at in the VPC: subnets by CDK group name, AZs, internet and NAT gateways, public-IP subnets, endpoints.

    Endpoint service names are shortened to their service part (`com.amazonaws.us-east-1.ecr.api` -> `ecr.api`). Pure
    describe calls, so the doctor and the tests can reuse it.
    """
    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]
    by_group: dict[str, list[str]] = {}
    for subnet in subnets:
        tags = {t["Key"]: t["Value"] for t in subnet.get("Tags") or []}
        by_group.setdefault(tags.get("aws-cdk:subnet-name", "other"), []).append(subnet["SubnetId"])
    endpoints = ec2.describe_vpc_endpoints(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["VpcEndpoints"]
    return {
        "subnets": by_group,
        "zones": sorted({s["AvailabilityZone"] for s in subnets}),
        "public_ip_subnets": [s["SubnetId"] for s in subnets if s.get("MapPublicIpOnLaunch")],
        "internet_gateways": [
            g["InternetGatewayId"]
            for g in ec2.describe_internet_gateways(Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}])[
                "InternetGateways"
            ]
        ],
        "nat_gateways": [
            n["NatGatewayId"]
            for n in ec2.describe_nat_gateways(
                Filters=[{"Name": "vpc-id", "Values": [vpc_id]}, {"Name": "state", "Values": ["available", "pending"]}]
            )["NatGateways"]
        ],
        "interface_endpoints": sorted(
            endpoint_service(e["ServiceName"]) for e in endpoints if e.get("VpcEndpointType") == "Interface"
        ),
        "gateway_endpoints": sorted(
            endpoint_service(e["ServiceName"]) for e in endpoints if e.get("VpcEndpointType") == "Gateway"
        ),
    }


def endpoint_service(service_name: str) -> str:
    """`com.amazonaws.us-east-1.bedrock-agentcore.gateway` -> `bedrock-agentcore.gateway` (the part after the region)."""
    parts = service_name.split(".", 3)
    return parts[3] if len(parts) == 4 else service_name


# ---- module 2 ---------------------------------------------------------------------------------------------------------------
def new_password() -> str:
    """17 characters: letters, digits and one symbol (the pool policy wants 12+, upper, lower, digit, symbol)."""
    chars = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
    ]
    chars += [secrets.choice(string.ascii_letters + string.digits) for _ in range(13)]
    secrets.SystemRandom().shuffle(chars)
    chars.insert(secrets.randbelow(len(chars) + 1), secrets.choice("!@#$%^&*"))
    return "".join(chars)


def ensure_personas(cfg) -> dict:
    """Groups, users, permanent passwords and the users secret, idempotently; existing passwords are kept."""
    idp = boto3.client("cognito-idp", region_name=cfg.region)
    secrets_manager = boto3.client("secretsmanager", region_name=cfg.region)
    pool = cfg.user_pool_id
    try:
        current = json.loads(
            secrets_manager.get_secret_value(SecretId=cfg.users_secret_arn).get("SecretString") or "{}"
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":  # an empty secret has no value yet
            raise
        current = {}
    for group, description in auth.GROUP_DESCRIPTIONS.items():
        try:
            idp.create_group(GroupName=group, UserPoolId=pool, Description=description)
            state = "created"
        except idp.exceptions.GroupExistsException:
            state = "exists "
        say(f"  group {group:<17} {state}  -> API role {auth.GROUP_ROLES[group]}")
    users = {}
    for persona, spec in auth.PERSONA_USERS.items():
        try:
            idp.admin_create_user(
                UserPoolId=pool,
                Username=spec["username"],
                UserAttributes=[{"Name": "email", "Value": spec["email"]}, {"Name": "email_verified", "Value": "true"}],
                MessageAction="SUPPRESS",
            )
            state = "created"
        except idp.exceptions.UsernameExistsException:
            state = "exists "
        password = (current.get(persona) or {}).get("password") or new_password()
        idp.admin_set_user_password(UserPoolId=pool, Username=spec["username"], Password=password, Permanent=True)
        idp.admin_add_user_to_group(UserPoolId=pool, Username=spec["username"], GroupName=spec["group"])
        users[persona] = {"username": spec["username"], "password": password, "email": spec["email"]}
        say(f"  user  {spec['username']:<17} {state}  in {spec['group']} ({spec['email']})")
    secrets_manager.put_secret_value(SecretId=cfg.users_secret_arn, SecretString=json.dumps({**current, **users}))
    say(
        f"  passwords in the users secret {cfg.users_secret_arn.split(':')[-1]}: `uv run python scripts/doctor.py --show-passwords` prints them; sign in once as any persona, then Switch persona in the user menu"
    )
    return users


def identity(ctx: Context, deploy: bool = True) -> None:
    cfg = ctx.cfg
    head("the people side of identity: three groups and three users in the pool the control plane brought")
    say(
        f"  user pool {cfg.user_pool_id}, app client {cfg.client_id} (deployed before the day: the API's login needs them)"
    )
    ensure_personas(cfg)
    ok("groups are the roles: platform-admins -> admin, agent-developers -> developer, approvers -> approver")
    look("Cognito console", ctx.console(f"cognito/v2/idp/user-pools/{cfg.user_pool_id}/users"))

    head("three personas sign in (Cognito), the API reads the group")
    anonymous = api.PlatformClient(cfg)
    try:
        anonymous.me()
        warn("GET /me without a token was accepted (unexpected)")
    except api.PlatformApiError as exc:
        ok(f"GET /me without a token -> {exc.status} {exc.code}")
    roles = {}
    deadline = time.monotonic() + 30
    for persona in ("admin", "developer", "approver"):
        while True:
            try:
                tokens = ctx.tokens(persona)
                break
            except WorkshopError:
                if time.monotonic() > deadline:  # a user created a second ago can still refuse the first sign-in
                    raise
                time.sleep(3)
        me = ctx.api(persona).me()
        roles[persona] = me.get("role")
        say(f"  {persona:<10} groups {tokens.groups!s:<22} API role {me.get('role')!s:<10} email {me.get('email')}")
    if roles != {"admin": "admin", "developer": "developer", "approver": "approver"}:
        raise WorkshopError(f"the personas do not map to the expected roles: {roles}")

    head("an Agent developer tries a Platform admin action")
    try:
        ctx.developer.create_domain(ctx.domain_id, "should be refused", "", "nobody")
        warn("POST /domains as developer was accepted (unexpected)")
    except api.PlatformApiError as exc:
        ok(f"POST /domains as developer -> {exc.status} {exc.code}: {exc.message[:80]}")
    look(
        "Platform UI",
        f"{ctx.ui_url}  (sign in once as any persona, then Switch persona in the user menu; passwords: `uv run python scripts/doctor.py --show-passwords`)",
    )

    head("the agent role template: one role pair per project and environment (module 3 stamps them)")
    template = ctx.root / "platform_sdk" / "platform_sdk" / "templates" / "agent_role.json"
    document = json.loads(template.read_text())
    gateway_lines = []
    for statement in (document.get("policy") or document).get("Statement", []):
        actions = statement.get("Action")
        actions = actions if isinstance(actions, list) else [actions]
        if any("InvokeGateway" in str(a) for a in actions):
            resources = statement.get("Resource")
            gateway_lines += resources if isinstance(resources, list) else [resources]
    say(f"  {template.relative_to(ctx.root)}")
    for line in gateway_lines:
        say(f"  InvokeGateway on {line}")
    n = ctx.names("dev")
    say(f"  rendered for {ctx.project_id}/dev: role {n.agent_role}, gateway {n.gateway}*")
    say(f"  Cedar principal the policies will see: {n.cedar_principal(cfg.account_id)}")
    ok(
        "the gateway name carries the boundary; the role has no bedrock:InvokeModel (models come through the model gateway key)"
    )
    say(f"\nshared names: policy engine {names.POLICY_ENGINE_NAME}, registry {names.REGISTRY_NAME}")

    # The corporate IdP for the on-behalf-of chain (design: .claude/docs/keycloak-obo-design.md): Keycloak, the employee, the
    # two OAuth2 credential providers. Modules 4 and 6 add the gateway and the harness that use them.
    from . import obo_build

    obo_build.module_2(ctx, deploy=deploy)
