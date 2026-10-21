"""`workshop cleanup`: remove what the modules built, in dependency order, then (with --stacks) the eight stacks and the sweep.

Everything is found by name (`names.ProjectNames` of the record's project and the fixed Finance project),
never from the state file alone, so the inventory is right after any interruption. Every step looks before it deletes and tolerates
"already gone", so a half-finished run can be run again. Prefixes are anchored on the environment segment (hr_assistant_dev_,
hr-assistant-dev-, ...): a project whose id merely extends ours never matches.

Order (the reverse of the build): online evaluation configs; the `live` endpoint and the harnesses (slow, awaited later); gateways
and their targets; the Cedar policies, then the shared policy engine when nothing else uses it; memories; catalogue records, then
the shared registry under the same rule; credential providers (API keys and the two OAuth2 providers of the on-behalf-of chain) and the demo
workload identity, the projects (the platform removes keys, teams, roles and every
stored item), the domains, the settings; the harness and evaluation log groups (twice: late log delivery recreates them); the
leave requests the agent filed; the state file (.workshop/state/), the evaluation runs next to it and the exports (exports/).
Stacks and the account sweep are `cdk destroy --all` and scripts/cleanup.py.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time

import boto3
from botocore.exceptions import ClientError

from platform_sdk import agentcore, api, identity, litellm, names

from . import cdk
from .context import Context, WorkshopError, head, ok, say, warn

HARNESS_GONE_TIMEOUT = 900  # seconds; 2-8 minutes measured, longer with many versions
MEMORY_GONE_TIMEOUT = 600  # about 2.5 minutes measured


def _list_all(operation, key, **kwargs):
    items, token = [], None
    while True:
        page = operation(**kwargs, **({"nextToken": token} if token else {}))
        items.extend(page.get(key, []))
        token = page.get("nextToken")
        if not token or not page.get(key):
            return items


def _wait_gone(check, label, timeout, every=5):
    started = time.monotonic()
    while check():
        if time.monotonic() - started > timeout:
            raise agentcore.AgentCoreError(
                f"{label} still present after {timeout:.0f} s; run `workshop cleanup` again, it only waits"
            )
        time.sleep(every)
    say(f"  {label}: gone after {time.monotonic() - started:.0f} s")


class Cleanup:
    """What the modules built, by name, and how to delete it."""

    def __init__(self, ctx: Context, keep_shared: bool = False, keep_state: bool = False):
        self.ctx = ctx
        self.cfg = cfg = ctx.cfg
        self.keep_shared = keep_shared
        self.keep_state = keep_state
        self.projects = [ctx.project_id, ctx.finance["project"]]
        self.domains = [ctx.domain_id, ctx.finance["domain"]]
        self.by_project = {pid: names.for_project(pid) for pid in self.projects}
        env_names = [n for by_env in self.by_project.values() for n in by_env.values()]
        dev = self.by_project[ctx.project_id]["dev"]
        # the on-behalf-of chain (modules 2, 4, 6) adds a JWT harness and gateway next to the Dev ones, two OAuth2 providers and a scratch workload identity
        self.harness_names = sorted({n.harness for n in env_names} | {dev.obo_harness})
        self.gateway_names = sorted({n.gateway for n in env_names} | {dev.obo_gateway})
        self.oauth_provider_names = [names.OBO_PROVIDER_AGENT, names.OBO_PROVIDER_GATEWAY]
        self.workload_identity_names = [names.OBO_DEMO_WORKLOAD]
        self.memory_names = sorted(n.memory for n in env_names)
        self.agentcore_prefixes = tuple(
            n.policy("") for n in env_names
        )  # <snake>_<env>_: policies, online evals, memories
        self.provider_prefixes = tuple(n.credential_provider("") for n in env_names)  # litellm-<project>-<env>-
        self.role_prefixes = tuple(f"{names.ROLE_PREFIX}-{n.project_id}-{n.env}-" for n in env_names)
        self.key_prefixes = tuple(n.key_alias("") for n in env_names)  # <project>-<env>-
        self.log_group_prefixes = (
            tuple(f"/aws/bedrock-agentcore/runtimes/harness_{n.harness}-" for n in env_names)
            + (f"/aws/bedrock-agentcore/runtimes/harness_{dev.obo_harness}-",)
            + tuple(f"/aws/bedrock-agentcore/evaluations/results/{n.online_evaluation}" for n in env_names)
        )
        aliases = (
            cfg.model_aliases if cfg.get("model-gateway/models") else []
        )  # no model gateway stack: no model records
        self.record_names = (
            {n.registry_record_tools for n in env_names} | {"kb-hr-policies"} | {f"model-{a}" for a in aliases}
        )
        self.control = agentcore.control_client(cfg.region)
        self.registry_control = boto3.client("agent-registry-control", region_name=cfg.region)
        self.logs = boto3.client("logs", region_name=cfg.region)
        self.iam = boto3.client("iam", region_name=cfg.region)
        self.admin = ctx.admin
        self.proxy: litellm.LiteLLMAdmin | None = None
        if cfg.get("model-gateway/url") and cfg.get("model-gateway/master-key-secret-arn"):
            try:
                # through the relay on the private landing zone (the proxy's load balancer is internal), direct otherwise
                self.proxy = litellm.admin_client(cfg, litellm.read_master_key(cfg))
            except Exception as exc:  # the model gateway is gone already
                warn(f"model gateway not reachable ({type(exc).__name__}); its teams and keys are skipped")
        else:
            say("  model gateway not deployed: its teams and keys are skipped")
        self.engine_deleted = self.registry_deleted = False
        self.kept_domains: list[str] = []

    # ---- helpers -------------------------------------------------------------------------------------------------------
    def ours(self, name, prefixes) -> bool:
        return any(str(name).startswith(prefix) for prefix in prefixes)

    def find_registry(self):
        return next(
            (
                r
                for r in _list_all(self.registry_control.list_registries, "registries")
                if r.get("name") == names.REGISTRY_NAME
            ),
            None,
        )

    def is_our_record(self, record) -> bool:
        # "platform": the AGENT record the promote job writes for a Prod agent (platform_api.services.registry.register_agent)
        tags = self.registry_control.list_tags_for_resource(resourceArn=record["recordArn"]).get("tags") or {}
        return tags.get("ManagedBy") in ("workshop", "notebook", "platform") or record["name"] in self.record_names

    def log_groups(self) -> list[str]:
        return [
            g["logGroupName"]
            for prefix in self.log_group_prefixes
            for page in self.logs.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=prefix)
            for g in page["logGroups"]
        ]

    def online_evaluations(self) -> list[dict]:
        return [
            o
            for o in agentcore._paginate(self.control, "list_online_evaluation_configs", "onlineEvaluationConfigs")
            if self.ours(o["onlineEvaluationConfigName"], self.agentcore_prefixes)
        ]

    def non_default_endpoints(self, harness_id: str) -> list[dict]:
        try:
            endpoints = agentcore._paginate(self.control, "list_harness_endpoints", "endpoints", harnessId=harness_id)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                return []
            raise
        return [e for e in endpoints if (e.get("endpointName") or e.get("name")) != "DEFAULT"]

    def other_projects(self) -> list[str]:
        return [p["projectId"] for p in self.admin.list_projects() if p["projectId"] not in self.projects]

    # ---- inventory -----------------------------------------------------------------------------------------------------
    def inventory(self) -> dict:
        """Everything with our names, grouped: `ours` must be empty at the end; `shared` may stay; `other` is context."""
        ours, shared, other = {}, {}, {}
        control, cfg = self.control, self.cfg
        ours["harnesses"] = [
            f"{h['harnessName']} ({h.get('status')})"
            for h in agentcore._paginate(control, "list_harnesses", "harnesses")
            if h["harnessName"] in self.harness_names
        ]
        ours["onlineEvaluations"] = [
            f"{o['onlineEvaluationConfigName']} ({o.get('status')})" for o in self.online_evaluations()
        ]
        ours["gateways"] = [
            f"{g['name']} ({g.get('status')})"
            for g in agentcore._paginate(control, "list_gateways", "items")
            if g["name"] in self.gateway_names
        ]
        engine = agentcore.find_policy_engine(names.POLICY_ENGINE_NAME, cfg.region)
        policies = (
            agentcore._paginate(control, "list_policies", "policies", policyEngineId=engine["policyEngineId"])
            if engine
            else []
        )
        ours["policies"] = [p["name"] for p in policies if self.ours(p["name"], self.agentcore_prefixes)]
        shared["policyEngine"] = engine["policyEngineId"] if engine else None
        shared["otherPolicies"] = [p["name"] for p in policies if not self.ours(p["name"], self.agentcore_prefixes)]
        ours["memories"] = [
            f"{m['id']} ({m.get('status')})"
            for m in agentcore._paginate(control, "list_memories", "memories")
            if self.ours(m["id"], self.agentcore_prefixes)
        ]
        ours["credentialProviders"] = [
            p["name"] for p in identity.list_providers(cfg.region) if self.ours(p["name"], self.provider_prefixes)
        ]
        ours["oauth2Providers"] = [
            p["name"] for p in identity.list_oauth2_providers(cfg.region) if p["name"] in self.oauth_provider_names
        ]
        ours["workloadIdentities"] = [
            w["name"]
            for w in agentcore._paginate(control, "list_workload_identities", "workloadIdentities")
            if w.get("name") in self.workload_identity_names
        ]
        ours["roles"] = [
            r["RoleName"]
            for page in self.iam.get_paginator("list_roles").paginate()
            for r in page["Roles"]
            if self.ours(r["RoleName"], self.role_prefixes)
        ]
        projects = self.admin.list_projects()
        ours["projects"] = [p["projectId"] for p in projects if p["projectId"] in self.projects]
        other["projects"] = [p["projectId"] for p in projects if p["projectId"] not in self.projects]
        ours["domains"] = [d["domainId"] for d in self.admin.list_domains() if d["domainId"] in self.domains]
        if self.proxy:
            ours["modelGatewayTeams"] = [pid for pid in self.projects if self.proxy.find_team(pid)]
            ours["modelGatewayKeys"] = [
                k.get("key_alias")
                for k in self.proxy.list_keys()
                if self.ours(k.get("key_alias") or "", self.key_prefixes)
            ]
        registry = self.find_registry()
        shared["registry"] = registry["registryId"] if registry else None
        records = (
            _list_all(self.registry_control.list_registry_records, "registryRecords", registryId=registry["registryId"])
            if registry
            else []
        )
        ours["registryRecords"] = [f"{r['name']} ({r['status']})" for r in records if self.is_our_record(r)]
        shared["otherRecords"] = [r["name"] for r in records if not self.is_our_record(r)]
        settings = self.admin.get_settings()
        shared["settings"] = {k: settings.get(k) for k in ("registryId", "policyEngineId")}
        ours["logGroups"] = self.log_groups()
        return {"ours": ours, "shared": shared, "other": other}

    @staticmethod
    def print_inventory(inv: dict) -> int:
        count = 0
        for group, items in inv.items():
            say(f"  [{group}]")
            for key, value in items.items():
                if isinstance(value, list):
                    shown = f"{len(value)}: {', '.join(map(str, value))}" if value else "none"
                    if group == "ours":
                        count += len(value)
                else:
                    shown = value
                say(f"    {key:<20} {shown}")
        return count

    # ---- the deletions -------------------------------------------------------------------------------------------------
    def run(self) -> dict:
        started = time.monotonic()
        control, cfg = self.control, self.cfg

        head("1  online evaluation configs, the live endpoint, the harnesses (slow: issued now, awaited in step 4)")
        for config_ in self.online_evaluations():
            if config_.get("status") != "DELETING":
                control.delete_online_evaluation_config(onlineEvaluationConfigId=config_["onlineEvaluationConfigId"])
            say(f"  online evaluation {config_['onlineEvaluationConfigName']}: delete issued")
        _wait_gone(self.online_evaluations, "online evaluation configs", 300)
        pending_endpoints: dict[str, str] = {}
        for name in self.harness_names:
            harness = agentcore.find_harness(name, cfg.region)
            if not harness:
                say(f"  harness {name}: not found")
                continue
            harness_id = harness["harnessId"]
            endpoints = self.non_default_endpoints(harness_id)
            for endpoint in endpoints:
                endpoint_name = endpoint.get("endpointName") or endpoint.get("name")
                if endpoint.get("status") != "DELETING":
                    control.delete_harness_endpoint(harnessId=harness_id, endpointName=endpoint_name)
                say(f"  harness {name}: deleting endpoint {endpoint_name} (DeleteHarness waits for it)")
            if endpoints:
                pending_endpoints[name] = harness_id
            elif harness.get("status") != "DELETING":
                control.delete_harness(harnessId=harness_id)
                say(f"  harness {name} ({harness.get('harnessVersion')} version(s)): DeleteHarness issued")
            else:
                say(f"  harness {name}: already DELETING")

        head("2  gateways and their targets; the Cedar policies; the shared policy engine")
        for name in self.gateway_names:
            t0 = time.monotonic()
            if agentcore.delete_gateway(name, cfg.region):
                say(f"  gateway {name}: targets and gateway deleted ({time.monotonic() - t0:.0f} s)")
            else:
                say(f"  gateway {name}: not found")
        _wait_gone(lambda: [n for n in self.gateway_names if agentcore.find_gateway(n, cfg.region)], "gateways", 600)
        engine = agentcore.find_policy_engine(names.POLICY_ENGINE_NAME, cfg.region)
        if engine:
            engine_id = engine["policyEngineId"]
            policies = agentcore._paginate(control, "list_policies", "policies", policyEngineId=engine_id)
            ours = [p for p in policies if self.ours(p["name"], self.agentcore_prefixes)]
            for policy in ours:
                control.delete_policy(policyEngineId=engine_id, policyId=policy["policyId"])
            say(f"  {len(ours)} policies: delete issued")
            _wait_gone(
                lambda: [
                    p
                    for p in agentcore._paginate(control, "list_policies", "policies", policyEngineId=engine_id)
                    if self.ours(p["name"], self.agentcore_prefixes)
                ],
                "policies",
                300,
            )
            remaining = [
                p["name"] for p in agentcore._paginate(control, "list_policies", "policies", policyEngineId=engine_id)
            ]
            attached_elsewhere = [
                g["name"]
                for g in agentcore._paginate(control, "list_gateways", "items")
                if (
                    (agentcore._get_or_none(control.get_gateway, gatewayIdentifier=g["gatewayId"]) or {}).get(
                        "policyEngineConfiguration"
                    )
                    or {}
                ).get("arn")
                == engine["policyEngineArn"]
            ]
            reasons = []
            if attached_elsewhere:
                reasons.append(f"gateways still attached: {attached_elsewhere}")
            if remaining:
                reasons.append(f"policies of other projects: {remaining}")
            if self.keep_shared:
                reasons.append("--keep-shared")
            elif others := self.other_projects():
                reasons.append(f"other projects exist in the platform: {others}")
            if reasons:
                say(f"  policy engine {engine_id} kept: " + "; ".join(reasons))
            else:
                control.delete_policy_engine(policyEngineId=engine_id)
                _wait_gone(
                    lambda: agentcore.find_policy_engine(names.POLICY_ENGINE_NAME, cfg.region),
                    f"policy engine {engine_id}",
                    300,
                )
                self.engine_deleted = True
        else:
            say("  policy engine: not found")

        head("3  memories (about 2.5 minutes to disappear; awaited in step 4)")
        for name in self.memory_names:
            say(f"  memory {name}: {'delete issued' if agentcore.delete_memory(name, cfg.region) else 'not found'}")

        head("4  wait for AgentCore: endpoints, harnesses, memories")
        for name, harness_id in pending_endpoints.items():
            _wait_gone(
                lambda hid=harness_id: self.non_default_endpoints(hid),
                f"harness {name} endpoints",
                HARNESS_GONE_TIMEOUT,
            )
            current = agentcore.find_harness(name, cfg.region)
            if current and current.get("status") != "DELETING":
                control.delete_harness(harnessId=harness_id)
                say(f"  harness {name}: DeleteHarness issued")
        _wait_gone(
            lambda: [n for n in self.harness_names if agentcore.find_harness(n, cfg.region)],
            "harnesses",
            HARNESS_GONE_TIMEOUT,
            every=10,
        )
        _wait_gone(
            lambda: [n for n in self.memory_names if agentcore.find_memory(n, cfg.region)],
            "memories",
            MEMORY_GONE_TIMEOUT,
            every=10,
        )

        head("5  catalogue records; the shared registry")
        registry = self.find_registry()
        if registry:
            registry_id = registry["registryId"]
            records = _list_all(self.registry_control.list_registry_records, "registryRecords", registryId=registry_id)
            ours = [r for r in records if self.is_our_record(r)]
            for record in ours:
                self.registry_control.delete_registry_record(registryId=registry_id, recordId=record["recordId"])
                say(f"  record {record['name']}: delete issued")
            our_ids = {r["recordId"] for r in ours}
            _wait_gone(
                lambda: [
                    r
                    for r in _list_all(
                        self.registry_control.list_registry_records, "registryRecords", registryId=registry_id
                    )
                    if r["recordId"] in our_ids
                ],
                f"{len(ours)} records",
                300,
            )
            remaining = [
                r["name"]
                for r in _list_all(
                    self.registry_control.list_registry_records, "registryRecords", registryId=registry_id
                )
            ]
            others = self.other_projects()
            foreign_rows = [
                f"{pid}/{r['name']}"
                for pid in others
                for r in self.admin.list_resources(pid)
                if r["type"] == "registry_record"
            ]
            reasons = []
            if remaining:
                reasons.append(f"records of others: {remaining}")
            if foreign_rows:
                reasons.append(f"registry_record resources of other projects: {foreign_rows}")
            if self.keep_shared:
                reasons.append("--keep-shared")
            elif others:
                reasons.append(f"other projects exist in the platform: {others}")
            if reasons:
                say(f"  registry {registry_id} kept: " + "; ".join(reasons))
            else:
                self.registry_control.delete_registry(registryId=registry_id)
                _wait_gone(self.find_registry, f"registry {registry_id}", 300)
                self.registry_deleted = True
        else:
            say("  registry: not found")

        head("6  credential providers, projects (keys, teams, roles, stored items), domains, settings")
        for provider in identity.list_providers(cfg.region):
            if self.ours(provider["name"], self.provider_prefixes):
                identity.delete_provider(provider["name"], region=cfg.region)
                say(f"  credential provider {provider['name']}: deleted")
        for name in self.oauth_provider_names:
            if identity.delete_oauth2_provider(name, region=cfg.region):
                say(f"  OAuth2 credential provider {name}: deleted")
        for name in self.workload_identity_names:
            if identity.delete_workload_identity(name, region=cfg.region):
                say(f"  workload identity {name}: deleted")
        failures = {}
        for pid in self.projects:
            try:
                outcome = self.admin.delete_project(pid, force=True)
                say(
                    f"  DELETE /projects/{pid}?force=true -> keys {[k.get('alias') for k in outcome.get('keys', [])]}, "
                    f"team {outcome.get('team')}, roles {outcome.get('roles')}, items {outcome.get('items')}"
                )
            except api.PlatformApiError as exc:
                if exc.status == 404:
                    say(f"  DELETE /projects/{pid}: not found")
                else:
                    failures[f"DELETE /projects/{pid}"] = f"{exc.code} ({exc.status}): {exc.message[:200]}"
                    warn(f"DELETE /projects/{pid}?force=true failed: {exc.code} ({exc.status}); sweeping by hand")
        if self.proxy:
            for key_row in self.proxy.list_keys():
                alias = key_row.get("key_alias") or ""
                if self.ours(alias, self.key_prefixes):
                    self.proxy.delete_key(key_row.get("token") or key_row.get("token_id"))
                    say(f"  model gateway key {alias}: deleted by the sweep")
            for pid in self.projects:
                if self.proxy.find_team(pid):
                    self.proxy.delete_team(pid)
                    say(f"  model gateway team {pid}: deleted by the sweep")
        for role_name in sorted(
            r["RoleName"]
            for page in self.iam.get_paginator("list_roles").paginate()
            for r in page["Roles"]
            if self.ours(r["RoleName"], self.role_prefixes)
        ):
            for policy_name in self.iam.list_role_policies(RoleName=role_name)["PolicyNames"]:
                self.iam.delete_role_policy(RoleName=role_name, PolicyName=policy_name)
            for attached in self.iam.list_attached_role_policies(RoleName=role_name)["AttachedPolicies"]:
                self.iam.detach_role_policy(RoleName=role_name, PolicyArn=attached["PolicyArn"])
            self.iam.delete_role(RoleName=role_name)
            say(f"  role {role_name}: deleted by the sweep")
        for domain_id in self.domains:
            try:
                self.admin.delete_domain(domain_id)
                say(f"  DELETE /domains/{domain_id} -> deleted")
            except api.PlatformApiError as exc:
                if exc.status == 404:
                    say(f"  DELETE /domains/{domain_id}: not found")
                elif exc.status == 409:
                    self.kept_domains.append(domain_id)
                    say(f"  DELETE /domains/{domain_id} -> 409: another project lives there; the domain stays")
                else:
                    failures[f"DELETE /domains/{domain_id}"] = f"{exc.code} ({exc.status}): {exc.message[:200]}"
                    warn(f"DELETE /domains/{domain_id} failed: {exc.code} ({exc.status})")
        settings_now = self.admin.get_settings()
        reset = {}
        if self.engine_deleted or (
            settings_now.get("policyEngineId")
            and not agentcore.find_policy_engine(names.POLICY_ENGINE_NAME, cfg.region)
        ):
            reset.update(policyEngineId=None, policyEngineArn=None)
        if self.registry_deleted or (settings_now.get("registryId") and not self.find_registry()):
            reset.update(registryId=None, registryArn=None)
        if reset:
            self.admin.put_settings(**reset)
            say(f"  PUT /settings {json.dumps(reset)}")
        else:
            say("  settings untouched (the shared objects were kept)")

        head("7  log groups of the harnesses and the online evaluation (twice: late log delivery recreates them)")
        for sweep in (1, 2):
            found = self.log_groups()
            for group_name in found:
                try:
                    self.logs.delete_log_group(logGroupName=group_name)
                    say(f"  log group {group_name}: deleted (sweep {sweep})")
                except ClientError as exc:
                    if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                        raise
            if not found:
                say(f"  sweep {sweep}: no log groups left")
                break
            if sweep == 1:
                time.sleep(60)

        head("8  the leave requests the agent filed (the seeded rows stay); the state file")
        table_name = cfg.get("scenario/tables/leave-requests")
        if table_name:
            table = boto3.resource("dynamodb", region_name=cfg.region).Table(table_name)
            seeded = {
                row["request_id"]
                for row in json.loads((self.ctx.scenario_dir / "data" / "leave_requests.json").read_text())
            }
            rows, scan = [], table.scan(ProjectionExpression="request_id")
            rows.extend(scan.get("Items", []))
            while "LastEvaluatedKey" in scan:
                scan = table.scan(ProjectionExpression="request_id", ExclusiveStartKey=scan["LastEvaluatedKey"])
                rows.extend(scan.get("Items", []))
            added = sorted(r["request_id"] for r in rows if r["request_id"] not in seeded)
            with table.batch_writer() as batch:
                for request_id in added:
                    batch.delete_item(Key={"request_id": request_id})
            say(f"  {table.name}: {len(rows)} rows, {len(seeded)} seeded kept, {len(added)} filed by the agent removed")
        else:
            say("  scenario table not deployed: no leave requests to sweep")
        if self.keep_state:
            say(f"  state file kept: {self.ctx.state.path}")
        else:
            removed = []
            runs_dir = self.ctx.state.path.parent / "evaluations"
            for path in sorted(runs_dir.glob(f"{self.ctx.project_id}-v*-run-*.json")) if runs_dir.is_dir() else []:
                path.unlink()
                removed.append(path.name)
            exports_dir = self.ctx.root / "exports"
            dev_harness = self.by_project[self.ctx.project_id]["dev"].harness
            for path in sorted(exports_dir.glob(f"{dev_harness}_*agent.py")) if exports_dir.is_dir() else []:
                path.unlink()
                removed.append(path.name)
            if (exports_dir / "__pycache__").is_dir():
                shutil.rmtree(exports_dir / "__pycache__", ignore_errors=True)
            if self.ctx.state.path.exists():
                self.ctx.state.path.unlink()
                removed.append(self.ctx.state.path.name)
            say(f"  removed on disk: {removed or 'nothing'}")

        head("9  what is left with our names")
        after = self.inventory()
        self.print_inventory(after)
        leftovers = {k: v for k, v in after["ours"].items() if v and k != "logGroups"}
        if leftovers.get("domains"):
            leftovers["domains"] = [d for d in leftovers["domains"] if d not in self.kept_domains]
            if not leftovers["domains"]:
                del leftovers["domains"]
        minutes = (time.monotonic() - started) / 60
        if leftovers or failures:
            warn(f"left: {leftovers or ''} {failures or ''}; run `workshop cleanup` again (every step is idempotent)")
        else:
            reappeared = after["ours"]["logGroups"]
            ok(
                f"everything the modules built is gone ({minutes:.1f} min)"
                + (f"; log groups reappeared, run again later: {reappeared}" if reappeared else "")
            )
        return {
            "leftovers": leftovers,
            "failures": failures,
            "minutes": round(minutes, 1),
            "engineDeleted": self.engine_deleted,
            "registryDeleted": self.registry_deleted,
        }


def confirm(question: str, yes: bool) -> bool:
    if yes:
        return True
    return input(f"\n{question} Type yes to continue: ").strip().lower() == "yes"


VPC_INTERFACE_TIMEOUT = (
    1800  # seconds; AgentCore releases a deleted harness's VPC network interfaces 15-30 minutes later
)
VPC_INTERFACE_POLL = 20


def vpc_interfaces(ec2, security_group: str | None, subnets: list[str], by_subnet: bool = False) -> list[dict]:
    """The network interfaces still in the harness network: those on the harness security group (AgentCore Runtime's, released in
    the background after a harness is deleted) or, with `by_subnet`, every interface left in the harness subnets (the relay
    Lambda's go while the ModelGateway stack is deleted). A missing group or subnet counts as empty."""
    if by_subnet and subnets:
        filters = [{"Name": "subnet-id", "Values": list(subnets)}]
    elif security_group:
        filters = [{"Name": "group-id", "Values": [security_group]}]
    else:
        return []
    found: dict[str, dict] = {}
    try:
        for page in ec2.get_paginator("describe_network_interfaces").paginate(Filters=filters):
            for eni in page.get("NetworkInterfaces", []):
                found[eni["NetworkInterfaceId"]] = {
                    "id": eni["NetworkInterfaceId"],
                    "status": eni.get("Status"),
                    "description": (eni.get("Description") or "")[:80],
                    "managed": bool(eni.get("RequesterManaged")),
                }
    except ClientError as exc:  # the group or a subnet is already gone: nothing holds the stack
        say(
            f"  describe-network-interfaces: {exc.response.get('Error', {}).get('Code')}; treating the network as clear"
        )
        return []
    return sorted(found.values(), key=lambda e: e["id"])


def wait_for_vpc_interfaces(
    cfg,
    region: str,
    by_subnet: bool = False,
    ec2=None,
    timeout: float = VPC_INTERFACE_TIMEOUT,
    every: float = VPC_INTERFACE_POLL,
) -> bool:
    """Wait until the harness network holds no network interface: a Network stack with interfaces left cannot delete its app
    subnets or the harness security group (DELETE_FAILED "has dependencies", "has a dependent object"). True when clear, False
    after `timeout` (the caller destroys anyway and says what is left). A public landing zone has nothing to wait for."""
    network = cfg.harness_network() if cfg is not None else None
    if not network:
        return True
    ec2 = ec2 or boto3.client("ec2", region_name=region)
    security_group = network["securityGroups"][0]
    where = "the harness subnets" if by_subnet else f"security group {security_group}"
    started = time.monotonic()
    last_report = None
    while True:
        left = vpc_interfaces(ec2, security_group, network["subnets"], by_subnet)
        elapsed = time.monotonic() - started
        if not left:
            say(f"  harness network: no network interface left in {where} (after {elapsed:.0f} s)")
            return True
        if last_report is None or elapsed - last_report >= 60:
            ids = ", ".join(e["id"] for e in left[:4]) + (" ..." if len(left) > 4 else "")
            say(
                f"  {len(left)} network interface(s) still in {where} after {elapsed / 60:.0f} min: {ids}. AgentCore and Lambda "
                "release them in the background, usually within 15-30 minutes; waiting"
            )
            last_report = elapsed
        if elapsed > timeout:
            warn(
                f"the harness network still holds {len(left)} interface(s) after {timeout / 60:.0f} min; trying anyway"
            )
            return False
        time.sleep(every)


def _config_or_none(ctx: Context):
    """The platform configuration from SSM, or None when the parameters are gone (the Network stack is already destroyed)."""
    try:
        return ctx.cfg
    except Exception as exc:  # noqa: BLE001 - without SSM there is no network to wait for
        say(f"  platform configuration not readable ({exc}); skipping the network-interface wait")
        return None


def destroy_stacks(ctx: Context, region: str | None) -> bool:
    """Wait for the harness network to be free of network interfaces, then `cdk destroy --all --force` (about 13 minutes: the
    model gateway drains, RDS and CloudFront take their time; the relay Lambda's interfaces can hold the Network stack for up to
    about 20 more), retry once after a DELETE_FAILED, then the sweep (scripts/cleanup.py --yes). The ECR repository with the
    LiteLLM image copy is emptied by the stack. True when no AgentPlatform stack is left and the sweep succeeded."""
    account, region = cdk.identity(region)
    env = cdk.app_env(account, region)
    cfg = _config_or_none(ctx)
    head(
        "harness network: wait for AgentCore to release the deleted harnesses' network interfaces (15-30 min after deletion)"
    )
    wait_for_vpc_interfaces(cfg, region)
    head("stacks: cdk destroy --all (about 13 minutes; the relay Lambda's network interfaces can add up to 20)")
    command = [*cdk.cdk_command(ctx.root), "destroy", "--all", "--force", "--concurrency", "4"]
    say(f"  $ (cd foundation && npx cdk {' '.join(command[2:])})")
    started = time.monotonic()
    completed = subprocess.run(command, cwd=str(ctx.root / "foundation"), env=env, check=False)
    if completed.returncode != 0:
        statuses = cdk.stack_statuses(region, cdk.EVERYTHING)
        failed = [name for name, status in statuses.items() if status and status.endswith("DELETE_FAILED")]
        if failed:
            warn(
                f"{', '.join(failed)}: DELETE_FAILED, usually app subnets or the harness security group still holding network "
                "interfaces; waiting for them, then destroying once more (CloudFormation retries only the failed resources)"
            )
            wait_for_vpc_interfaces(cfg, region, by_subnet=True)
            completed = subprocess.run(command, cwd=str(ctx.root / "foundation"), env=env, check=False)
    if completed.returncode != 0:
        raise WorkshopError(
            f"cdk destroy exited {completed.returncode} after {(time.monotonic() - started) / 60:.1f} min; read the events "
            "above. A Network stack in DELETE_FAILED usually still holds network interfaces (aws ec2 describe-network-interfaces "
            "--filters Name=vpc-id,Values=<vpc id> shows whose); a leftover harness runtime (aws bedrock-agentcore-control "
            "list-agent-runtimes) holds them too. Wait for them to go, then run `workshop cleanup --stacks` again"
        )
    ok(f"stacks destroyed in {(time.monotonic() - started) / 60:.1f} min")
    head("sweep: scripts/cleanup.py --yes (secrets in their recovery window, recreated log groups, orphan buckets)")
    sweep = subprocess.run(
        [sys.executable, str(ctx.root / "scripts" / "cleanup.py"), "--yes"], cwd=str(ctx.root), env=env, check=False
    )
    say("  stacks after:")
    statuses = cdk.print_statuses(region)
    if sweep.returncode != 0 or any(statuses.values()):
        warn("not clean yet: read the sweep report above; `workshop cleanup --stacks` can run again")
        return False
    ok("the account holds only the CDK bootstrap stack and the account-level Transaction Search setting")
    return True


def run(
    ctx: Context,
    dry_run: bool = False,
    yes: bool = False,
    stacks: bool = False,
    keep_shared: bool = False,
    keep_state: bool = False,
) -> int:
    region = cdk.identity(ctx.region_override)[1]
    if cdk.stack_statuses(region, ["AgentPlatform-Platform"])["AgentPlatform-Platform"] is None:
        say("the control plane is not deployed, so the platform API cannot delete the modules' objects from here.")
        say(
            "Objects with your names in AgentCore, the registry and IAM would stay: check the AgentCore console if you built any."
        )
        if (
            stacks
            and not dry_run
            and confirm("Destroy every AgentPlatform stack that is left (cdk destroy --all)?", yes)
        ):
            destroy_stacks(ctx, region)
        return 0
    cleaner = Cleanup(ctx, keep_shared=keep_shared, keep_state=keep_state)
    head("inventory: what the modules built, by name")
    count = cleaner.print_inventory(cleaner.inventory())
    if dry_run:
        say(f"\ndry run: {count} object(s) would be deleted" + ("; then the eight stacks" if stacks else ""))
        return 0
    if count == 0:
        say(
            "\nnothing of the modules is left in the account; the local state and the filed leave requests are swept below"
        )
    elif not confirm(f"Delete these {count} object(s)?", yes):
        say("nothing deleted")
        return 1
    result = cleaner.run()  # every step is a no-op for what no longer exists
    if keep_state:
        ctx.state.set("workshop.cleanup", result)
    if stacks:
        if not confirm("Destroy the eight stacks too (the whole platform and the IdP, about 13 minutes)?", yes):
            say("stacks kept")
            return 0
        destroy_stacks(ctx, region)
    else:
        say(
            "\nthe stacks are still running (about 0.28 USD per hour, the interface endpoints included): "
            "`workshop cleanup --stacks` removes them, or deploy the modules again"
        )
    return 0
