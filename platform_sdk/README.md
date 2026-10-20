# platform_sdk

Shared Python package of the workshop "Build an Agentic AI Platform with Amazon Bedrock AgentCore". The `workshop` command
imports it directly (`uv` workspace member) and `scripts/build_backend.py` vendors it into the backend Lambda bundle. Interfaces follow
the workshop build contracts (team-internal design document, section 5); nothing in here depends on a particular account (region
and account id come from the environment and SSM).

## Module map

| Module | Purpose |
| --- | --- |
| `config` | `load()` reads every `/agent-platform/*` SSM parameter once and exposes typed accessors (`api_url`, `client_id`, `model_gateway_url`, `approved_models`, ...). `PLATFORM_CONFIG_JSON` overrides SSM for tests and Lambdas. |
| `names` | `ProjectNames(project_id, env)`: gateway, memory, harness, roles, Cedar principal, LiteLLM team and key aliases, credential provider names; `harness_log_group`, `harness_service_name`, `harness_metric_name`. |
| `auth` | `login("admin" \| "developer" \| "approver")` -> `Tokens` (ID token, decoded claims, groups, role); `login(username, password)` for any other user; `cfg=` overrides the configuration loaded from SSM; `personas(cfg)` reads the users secret. |
| `api` | `PlatformClient(cfg, id_token)` with one method per route (`health`, `me`, `create_project`, `register_resource`, `create_blueprint`, `deploy`, `wait_for_job`, `submit_evaluation`, `promote_release`, `spend`, ...). Errors: `PlatformApiError(code, message, status)`. |
| `blueprint` | `load(path | dict)` -> `Blueprint` (validated, prompt resolved); `from_stored(spec, prompt, project_id, version)` rebuilds a stored version; `render(bp, env, project, resources, cfg, credential_provider_arn=None)` -> `CreateHarness` kwargs (the key ARN defaults to the registered `credential_provider` resource for the model); `for_update(spec)` -> `UpdateHarness` kwargs (tools, allowedTools and hooks sent explicitly); `tags_for(bp, env)`. Unresolved references raise `BlueprintError(code="BLUEPRINT_UNRESOLVED")` naming the step that creates the resource. |
| `agentcore` | `wait_for`, `ensure_gateway`, `ensure_lambda_target`, `ensure_kb_target`, `set_gateway_policy_mode`, `ensure_policy_engine`, `ensure_policy`, `ensure_memory`, `create_or_update_harness`, `ensure_endpoint`, `ensure_online_evaluation`, `invoke` -> `InvokeResult`, `is_budget_exceeded`, deleters for `workshop cleanup`. |
| `litellm` | `LiteLLMAdmin(url, master_key)`: teams, service-account keys, key info/update/block, spend logs, `team_spend_summary`, `chat_completion` smoke test. Errors: `LiteLLMError(status, message, body, type)` with `budget_exceeded`. |
| `identity` | `store_api_key(name, key)` -> credential provider ARN (update when it exists), `get_provider_arn`, `delete_provider`. |
| `evaluation` | `run_dataset(...)` -> `RunSummary` (with `failed_scenarios`); `summarize(result, evaluator_ids)` -> scores and per-scenario results (failed scenarios count as 0 and block the release); `apply_thresholds(scores, thresholds, failed_scenarios=None)` / `RunSummary.verdict(thresholds)` -> `Verdict`. `bedrock_agentcore` is imported lazily. |
| `export` | `to_strands(harness, cfg)` and `to_langgraph(harness, cfg)` -> Python source of a Strands Agents or LangGraph module mirroring a harness configuration (model alias and gateway, system prompt, gateway tools through `mcp`, agent role, limits and memory as comments); `summarize(harness)` is the normalised view they work from (`workshop export`, module 6). |
| `metrics` | `harness_metrics`, `policy_metrics`, `evaluation_metrics` over CloudWatch `GetMetricData` (dimension sets discovered with `ListMetrics`). |
| `state` | `WorkshopState` JSON store under `.workshop/state/<project>.json` with dotted keys (`PLATFORM_STATE_DIR` overrides the folder). |
| `templates` | `agent_role.json`, `gateway_role.json` and `templates.roles.ensure_project_roles(cfg, project_id, env, allowed_tool_arns, kb_ids)`. |

## Usage

Sign in as a persona and make an API call:

```python
from platform_sdk import api, auth, config

cfg = config.load()
tokens = auth.login("developer")
client = api.PlatformClient(cfg, tokens.id_token)
print(client.me())
client.register_resource(
    "hr-assistant",
    "dev",
    "gateway",
    "hr-assistant-dev-gw",
    gateway["gatewayArn"],
    id=gateway["gatewayId"],
    url=gateway["gatewayUrl"],
)
```

Render a blueprint and create the Dev harness:

```python
from platform_sdk import agentcore, blueprint, identity, names

bp = blueprint.load("scenario/blueprint/blueprint.yaml")
project = client.get_project(bp.project_id)
resources = client.list_resources(bp.project_id, env="dev")
provider_arn = identity.get_provider_arn(names.ProjectNames(bp.project_id, "dev").credential_provider(bp.model_alias))
spec = blueprint.render(bp, "dev", project, resources, cfg, provider_arn)  # exact CreateHarness kwargs
harness = agentcore.create_or_update_harness(
    spec["harnessName"], spec, tags=blueprint.tags_for(bp, "dev", managed_by="workshop")
)
result = agentcore.invoke(harness["arn"], "What is my leave balance? My employee id is E1001.")
print(result.text, result.tools, result.usage)
```

Run the evaluation dataset against the Dev harness and submit the result to a release:

```python
from platform_sdk import evaluation

summary = evaluation.run_dataset(
    "scenario/evaluation/hr-assistant.json",
    harness["arn"],
    agentcore.harness_log_group(harness, "DEFAULT"),
    bp.evaluator_ids(),  # ["Builtin.Correctness", "Builtin.GoalSuccessRate", ...] from the thresholds
)
verdict = summary.verdict(bp.thresholds)  # apply_thresholds(summary.scores, bp.thresholds, failed_scenarios=...)
print(verdict.status, verdict.failures)
client.submit_evaluation(release["releaseId"], summary)
```

Create the release with `auto_run=False` for this path. By default `client.create_release(project_id, version)` lets the platform
run it: the worker deploys the version to Dev when the Dev harness does not run it yet, runs the same dataset from the dataset
bucket and records the verdict; the release is `EVALUATING` meanwhile (`release["pipeline"]` has the stage and the job ids) and
`client.run_release(release_id)` starts the run again for a DRAFT or BLOCKED release.

## Tests

`uv run pytest platform_sdk/tests -q` (no AWS calls: botocore Stubber, moto and mocked `requests`) and `uv run ruff check platform_sdk`.

## Facts verified against the live services (8 Oct 2026)

- `Builtin.Harmfulness` scores **1.0 = Not Harmful, 0.0 = Harmful** (GetEvaluator); `apply_thresholds` therefore treats a Harmfulness threshold as an
  upper bound on the harm rate `1 - mean` (`Harmfulness: 0.0` = no harmful answer tolerated). All other thresholds are floors on the mean.
- `CreateHarness` / `UpdateHarness` answer `{"harness": {...}}`; `UpdateHarness` rejects `tags` and wants `memory: {"optionalValue": ...}` (`blueprint.for_update`).
- The endpoint log group is `/aws/bedrock-agentcore/runtimes/<agentRuntimeId>-<endpoint>` where `agentRuntimeId` comes from
  `GetHarness.environment.agentCoreRuntimeEnvironment.agentRuntimeId` (`agentcore.harness_log_group`), not the harness id.
- A gateway with a policy engine attached needs `bedrock-agentcore:GetPolicyEngine`, `AuthorizeAction` and `PartiallyAuthorizeActions` on its execution
  role (`templates/gateway_role.json`); create the `permit` for a tool before a `forbid` on the same tool or the analyzer rejects it as "Overly Restrictive".
- Built-in harness metrics are published under four equivalent dimension sets; `metrics.harness_metrics` queries only the smallest one so totals are not multiplied.
- Timings from the dry runs (8 Oct 2026): policy engine 12 s, gateway 6-7 s, Lambda target 6 s, knowledge-base target 6 s, policy 7-14 s, memory with
  two strategies about 150 s, harness create 14-24 s, harness update 14 s, endpoint create or repoint 4 s. Invocations: the harness's own work is
  4-8 s (spans: `POST /invocations`, LiteLLM model calls 1.2-2.6 s each, memory events about 1.5 s per turn), but a **new runtime session costs
  about 25-35 s extra** whenever no warm microVM is available (first invocation after a new version, and most new sessions of a burst such as a
  dataset run); further turns in the same session take about 5 s. Keep one session id per conversation. A 10-scenario dataset run with the 180 s
  telemetry delay takes 5.5-6 minutes.
- `create_or_update_harness` waits for the version returned by Create/UpdateHarness to be READY (GetHarness can still report the previous version
  as READY right after an update).
