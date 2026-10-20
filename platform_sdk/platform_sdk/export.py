"""Export a harness configuration to a code skeleton: Strands Agents or LangGraph (`workshop export`, module 6).

    source = export.to_strands(harness, cfg)      # `harness` = GetHarness output or the render payload (CreateHarness kwargs)
    source = export.to_langgraph(harness, cfg)    # the same configuration as a LangGraph ReAct agent (langchain-openai + langgraph)
    Path("exports/hr_assistant_dev_strands_agent.py").write_text(source)

The generated module mirrors what the harness configuration says, in code: the model alias and the model gateway as a Strands
`OpenAIModel` (the virtual key comes from an environment variable; the harness reads it from the token vault provider named in a
comment), the system prompt, the environment's gateway tools through `platform_sdk.mcp` (every tool `tools/list` returns, filtered
by the harness's `allowedTools`, each wrapped as a Strands tool whose call is `tools/call` signed with SigV4), the agent role the
harness runs as (assumed by default, so the Cedar policies see the same principal) and the limits and the memory as comments. It is
a starting point for the pro-code path, not a product: no AgentCore Memory, no spans, no managed runtime, no release gate.

The model gateway is private (tier 3 landing zone: an internal load balancer, reachable only from inside the VPC), so the
generated module reads `MODEL_GATEWAY_BASE` from the environment variable of the same name when it is set and falls back to the
harness's `apiBase` otherwise: `workshop export --run` starts a localhost relay proxy (`platform_sdk.litellm.LocalRelayProxy`)
and runs the module with `MODEL_GATEWAY_BASE=http://127.0.0.1:<port>/v1`; code deployed inside the VPC uses the real URL.

`summarize(harness)` returns the normalised view the generator works from (model, prompt, gateway, allowed tools, memory, limits).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from . import names

DEFAULT_API_KEY_ENV = "MODEL_GATEWAY_API_KEY"
BASE_URL_ENV = "MODEL_GATEWAY_BASE"  # overrides the harness's apiBase: the private gateway needs a relay from a laptop
GATEWAY_TOOL_TYPE = "agentcore_gateway"
LIMIT_KEYS = ("maxIterations", "maxTokens", "timeoutSeconds")
LIMIT_MEANING = {
    "maxIterations": "event-loop cycles per invocation",
    "maxTokens": "output tokens per invocation, across all model calls",
    "timeoutSeconds": "wall time per invocation",
}


def _unwrap(harness_config: dict) -> dict:
    """GetHarness answers {"harness": {...}}; the render payload and CreateHarness kwargs are the configuration itself."""
    if not isinstance(harness_config, dict):
        raise ValueError(f"to_strands expects a harness configuration dict, got {type(harness_config).__name__}")
    inner = harness_config.get("harness")
    return inner if isinstance(inner, dict) and "model" in inner else harness_config


def gateway_url_from_arn(gateway_arn: str) -> str:
    """`arn:aws:bedrock-agentcore:<region>:<account>:gateway/<id>` -> the gateway's MCP URL (the shape GetGateway returns)."""
    parts = gateway_arn.split(":")
    if len(parts) < 6 or not parts[5].startswith("gateway/"):
        raise ValueError(f"not a gateway ARN: {gateway_arn!r}")
    region = parts[3]
    gateway_id = parts[5].split("/", 1)[1]
    return f"https://{gateway_id}.gateway.bedrock-agentcore.{region}.amazonaws.com/mcp"


def summarize(harness_config: dict, cfg=None) -> dict:
    """Normalised view of a harness configuration (GetHarness output or the render payload) for the generator and `workshop export`.

    Keys: name, version, executionRoleArn, region, model {alias, modelId, apiBase, apiKeyArn, maxTokens, temperature?, topP?},
    systemPrompt, gateway {name, arn, id, url} | None, allowedTools (None = every tool of the gateway), memoryArn, limits.
    `cfg` (a PlatformConfig) supplies the model gateway base URL and the region when the configuration lacks them.
    """
    config = _unwrap(harness_config)
    model_cfg = (config.get("model") or {}).get("liteLlmModelConfig") or {}
    model_id = model_cfg.get("modelId")
    if not model_id:
        raise ValueError(
            "the harness configuration has no model.liteLlmModelConfig.modelId: pass GetHarness output or GET .../render"
        )
    alias = model_id.split("/", 1)[1] if "/" in model_id else model_id
    api_base = model_cfg.get("apiBase") or (cfg.model_gateway_openai_base if cfg is not None else None)
    if not api_base:
        raise ValueError("the harness configuration has no model.liteLlmModelConfig.apiBase and no cfg was given")
    model: dict[str, Any] = {
        "alias": alias,
        "modelId": model_id,
        "apiBase": api_base,
        "apiKeyArn": model_cfg.get("apiKeyArn"),
        "maxTokens": int(model_cfg.get("maxTokens") or 2048),
    }
    for key in ("temperature", "topP"):
        if key in model_cfg:
            model[key] = model_cfg[key]

    prompt_blocks = config.get("systemPrompt") or []
    if isinstance(prompt_blocks, str):
        system_prompt = prompt_blocks
    else:
        system_prompt = "\n".join(str(b.get("text", "")) for b in prompt_blocks if isinstance(b, dict))

    gateway: dict | None = None
    for tool in config.get("tools") or []:
        if tool.get("type") != GATEWAY_TOOL_TYPE:
            continue
        gateway_arn = ((tool.get("config") or {}).get("agentCoreGateway") or {}).get("gatewayArn")
        if gateway_arn:
            gateway = {
                "name": tool.get("name") or names.GATEWAY_TOOL_NAME,
                "arn": gateway_arn,
                "id": gateway_arn.rsplit("/", 1)[-1],
                "url": gateway_url_from_arn(gateway_arn),
            }
            break
    allowed_tools: list[str] | None = None
    if gateway:
        entries = [str(a) for a in config.get("allowedTools") or []]
        source = f"@{gateway['name']}"
        if entries and source not in entries:
            allowed_tools = [a[len(source) + 1 :] for a in entries if a.startswith(source + "/")]

    memory = config.get("memory") or {}
    if "optionalValue" in memory:
        memory = memory["optionalValue"] or {}
    memory_arn = (memory.get("agentCoreMemoryConfiguration") or {}).get("arn")

    limits = {k: config[k] for k in LIMIT_KEYS if config.get(k) is not None}
    region = None
    if gateway:
        region = gateway["arn"].split(":")[3]
    elif cfg is not None:
        region = cfg.region
    return {
        "name": config.get("harnessName") or config.get("name"),
        "version": config.get("harnessVersion"),
        "executionRoleArn": config.get("executionRoleArn"),
        "region": region or "us-east-1",
        "model": model,
        "systemPrompt": system_prompt,
        "gateway": gateway,
        "allowedTools": allowed_tools,
        "memoryArn": memory_arn,
        "limits": limits,
    }


def _literal(value) -> str:
    """A Python literal with double-quoted strings (the style ruff formats to), for the generated constants."""
    if value is None:
        return "None"
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_literal(v) for v in value) + "]"
    return repr(value)


def _triple_quoted(text: str) -> str:
    """A triple-quoted literal that evaluates to exactly `text` (backslashes, triple quotes and a trailing quote escaped).

    Backslashes are doubled first, then every run of three double quotes is written as three escaped quotes; a final quote is
    escaped only when it is not already escaped by that step (an even number of backslashes before it), so prompts ending in
    three quotes, or in a backslash and three quotes, round-trip too. The literal opens with a line continuation so the text
    starts on its own line.
    """
    escaped = text.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    if escaped.endswith('"'):
        body = escaped[:-1]
        preceding_backslashes = len(body) - len(body.rstrip("\\"))
        if preceding_backslashes % 2 == 0:  # still a plain quote: it would merge with the closing triple quote
            escaped = body + '\\"'
    return '"""\\\n' + escaped + '"""'


def to_strands(
    harness_config: dict,
    cfg=None,
    api_key_env: str = DEFAULT_API_KEY_ENV,
    generated_at: datetime | None = None,
) -> str:
    """Python source of a Strands Agents module mirroring `harness_config` (GetHarness output or the render payload).

    The module imports and compiles on its own; running it needs the virtual key in `api_key_env` and AWS credentials that may
    assume the harness's agent role (or `--own-identity`). `cfg` fills in the model gateway base URL when the configuration has
    none; `generated_at` pins the timestamp in the header (tests).
    """
    summary = summarize(harness_config, cfg)
    stamp = (generated_at or datetime.now(UTC)).replace(microsecond=0).isoformat()
    name = summary["name"] or "harness"
    version = f" (version {summary['version']})" if summary.get("version") else ""
    model, gateway = summary["model"], summary["gateway"]
    limits = summary["limits"]
    limits_text = (
        ", ".join(f"{k} {v} ({LIMIT_MEANING.get(k, k)})" for k, v in limits.items())
        if limits
        else "none set on the harness"
    )
    memory_text = (
        f"AgentCore Memory {summary['memoryArn']} (events per session, long-term records per actor)"
        if summary["memoryArn"]
        else "disabled on the harness"
    )
    model_params = ['"max_tokens": MODEL_MAX_TOKENS']
    for key, param in (("temperature", "temperature"), ("topP", "top_p")):
        if key in model:
            model_params.append(f'"{param}": {model[key]!r}')

    lines: list[str] = [
        f'"""Strands Agents skeleton generated from the AgentCore harness `{name}`{version}.',
        "",
        f"Generated by platform_sdk.export.to_strands at {stamp} from the harness configuration. A starting point for the pro-code",
        "path, not a product: it mirrors the model, the system prompt, the gateway tools and the agent identity of the harness; memory,",
        "tracing, the managed runtime and the platform's release gate are yours to add (see the comments below).",
        "",
        f"    export {api_key_env}=sk-...   # a virtual key of the project's team in the model gateway (module 5, or `workshop export --run`)",
        f'    uv run python {name}_agent.py "What is my leave balance? My employee id is E1001."',
        "",
        f"The model gateway is private: from a laptop, `workshop export --run` provides a localhost relay and sets {BASE_URL_ENV};",
        "inside the VPC the harness's own apiBase (the default below) is reachable directly.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "import argparse",
        "import json",
        "import os",
        "import sys",
        "",
        "import boto3",
        "from strands import Agent",
        "from strands.models.openai import OpenAIModel",
        "from strands.tools.tools import PythonAgentTool",
        "",
        "from platform_sdk import mcp",
        "",
        "# ---- model: the model gateway alias of the harness (model.liteLlmModelConfig) ----------------------------------------",
        f"MODEL_ALIAS = {_literal(model['alias'])}  # modelId {model['modelId']}: a LiteLLM alias, never a Bedrock model id (guardrail and spend attribution come with it)",
        "# apiBase: the OpenAI-compatible base URL of the model gateway. The gateway is private (reachable only from inside the",
        "# VPC): `workshop export --run` starts a localhost relay proxy and sets MODEL_GATEWAY_BASE to it; in the VPC leave it unset.",
        f"MODEL_GATEWAY_BASE = os.environ.get({_literal(BASE_URL_ENV)}, {_literal(model['apiBase'])})",
        f"MODEL_MAX_TOKENS = {model['maxTokens']}  # maxTokens per model call",
        f"API_KEY_ENV = {_literal(api_key_env)}",
        "# The harness fetches its key at run time from the AgentCore Identity provider below (apiKeyArn); code cannot, so the key",
        "# comes from the environment variable above: create one like module 5 does (platform_sdk.litellm) and never commit it.",
        f"CREDENTIAL_PROVIDER_ARN = {_literal(model['apiKeyArn'])}",
        "",
        "# ---- tools: the environment's gateway (tools[].config.agentCoreGateway), called with SigV4 through platform_sdk.mcp -----",
    ]
    if gateway:
        lines += [
            f"GATEWAY_ARN = {_literal(gateway['arn'])}",
            f"GATEWAY_URL = {_literal(gateway['url'])}  # derived from the ARN; GetGateway shows the same gatewayUrl",
            f"ALLOWED_TOOLS = {_literal(summary['allowedTools'])}  # allowedTools {_literal(list(_unwrap(harness_config).get('allowedTools') or []))}: None = every tool the gateway lists for the caller",
        ]
    else:
        lines += [
            "GATEWAY_ARN = None  # the harness has no agentcore_gateway tool: no tools are wired up below",
            "GATEWAY_URL = None",
            "ALLOWED_TOOLS = []",
        ]
    lines += [
        f"AGENT_ROLE_ARN = {_literal(summary['executionRoleArn'])}  # executionRoleArn: the Cedar principal the gateway's policies permit",
        f"REGION = {_literal(summary['region'])}",
        "",
        "# ---- what the harness gave you for free and this skeleton does not -----------------------------------------------------",
        f"# memory: {memory_text}; in code, add a Strands session manager backed by AgentCore Memory (bedrock-agentcore SDK)",
        f"# limits: {limits_text}; Strands has no direct equivalents, enforce them in your runtime",
        "# observability: the harness emits OpenTelemetry spans (gen_ai.*) that GenAI Observability and the evaluations read; add the",
        "#   strands OTel exporter (strands.telemetry) and CloudWatch Transaction Search to get the same from this code",
        "# runtime and release gate: package this module for AgentCore Runtime (agentcore CLI or CDK) so that the platform's dataset",
        "#   evaluation and the online evaluation see its spans, and give it its own key, role and policies like the harness",
        "",
        "SYSTEM_PROMPT = " + _triple_quoted(summary["systemPrompt"]),
        "",
        "",
        "def _caller(name: str, session: boto3.Session | None):",
        '    """A Strands tool function: tools/call `name` on the gateway with the tool input the model produced."""',
        "",
        "    def call(tool_use, **_):",
        "        try:",
        '            result = mcp.call_tool(GATEWAY_URL, name, tool_use.get("input") or {}, session=session, region=REGION)',
        "        except (mcp.McpError, mcp.McpHttpError) as exc:  # a policy denial or an IAM refusal: tell the model",
        '            return {"toolUseId": tool_use["toolUseId"], "status": "error", "content": [{"text": str(exc)}]}',
        '        status = "error" if result.is_error else "success"',
        '        return {"toolUseId": tool_use["toolUseId"], "status": status, "content": [{"text": result.text}]}',
        "",
        "    return call",
        "",
        "",
        "def gateway_tools(session: boto3.Session | None = None) -> list[PythonAgentTool]:",
        '    """One Strands tool per gateway tool that tools/list returns for the caller, filtered by ALLOWED_TOOLS."""',
        "    if not GATEWAY_URL:",
        "        return []",
        "    tools = []",
        "    for spec in mcp.list_tools(GATEWAY_URL, session=session, region=REGION):",
        '        name = spec["name"]',
        "        if ALLOWED_TOOLS is not None and name not in ALLOWED_TOOLS:",
        "            continue",
        "        tool_spec = {",
        '            "name": name,',
        '            "description": spec.get("description") or name,',
        '            "inputSchema": {"json": spec.get("inputSchema") or {"type": "object", "properties": {}}},',
        "        }",
        "        tools.append(PythonAgentTool(name, tool_spec, _caller(name, session)))",
        "    return tools",
        "",
        "",
        "def build_agent(api_key: str, session: boto3.Session | None = None) -> Agent:",
        '    """The agent: OpenAIModel against the model gateway, the gateway tools, the harness\'s system prompt."""',
        "    model = OpenAIModel(",
        '        client_args={"api_key": api_key, "base_url": MODEL_GATEWAY_BASE},',
        "        model_id=MODEL_ALIAS,",
        "        params={" + ", ".join(model_params) + "},",
        "    )",
        "    return Agent(model=model, tools=gateway_tools(session), system_prompt=SYSTEM_PROMPT, callback_handler=None)",
        "",
        "",
        "def main(argv: list[str] | None = None) -> int:",
        "    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)",
        '    parser.add_argument("prompt", nargs="+", help="one or more user messages (one conversation)")',
        "    parser.add_argument(",
        '        "--own-identity",',
        '        action="store_true",',
        '        help="call the gateway with your own AWS identity instead of assuming the harness\'s agent role",',
        "    )",
        "    args = parser.parse_args(argv)",
        "    api_key = os.environ.get(API_KEY_ENV)",
        "    if not api_key:",
        '        print(f"set {API_KEY_ENV} to a virtual key of the model gateway (see the module docstring)", file=sys.stderr)',
        "        return 2",
        "    session = None",
        "    if AGENT_ROLE_ARN and not args.own_identity:",
        '        session = mcp.assume_role_session(AGENT_ROLE_ARN, session_name="strands-export", region=REGION)',
        "    agent = build_agent(api_key, session)",
        "    for prompt in args.prompt:",
        "        result = agent(prompt)",
        "        print(str(result).strip())",
        "        tools = {name: m.call_count for name, m in result.metrics.tool_metrics.items()}",
        "        usage = dict(result.metrics.accumulated_usage)",
        '        print(json.dumps({"stopReason": result.stop_reason, "usage": usage, "tools": tools}), file=sys.stderr)',
        "    return 0",
        "",
        "",
        'if __name__ == "__main__":',
        "    sys.exit(main())",
        "",
    ]
    return "\n".join(lines)


def to_langgraph(
    harness_config: dict,
    cfg=None,
    api_key_env: str = DEFAULT_API_KEY_ENV,
    generated_at: datetime | None = None,
) -> str:
    """Python source of a LangGraph module mirroring `harness_config`: LangChain 1.x `create_agent` (a compiled LangGraph graph) over `ChatOpenAI` pointed at the
    model gateway, one `StructuredTool` per gateway tool (the gateway's JSON schema as is, the call signed with SigV4 through
    `platform_sdk.mcp`), the harness's system prompt and agent role. Same inputs and limits as `to_strands`; needs the `langgraph`
    dependency group (langchain, langgraph, langchain-openai, langchain-core)."""
    summary = summarize(harness_config, cfg)
    stamp = (generated_at or datetime.now(UTC)).replace(microsecond=0).isoformat()
    name = summary["name"] or "harness"
    version = f" (version {summary['version']})" if summary.get("version") else ""
    model, gateway = summary["model"], summary["gateway"]
    limits = summary["limits"]
    limits_text = (
        ", ".join(f"{k} {v} ({LIMIT_MEANING.get(k, k)})" for k, v in limits.items())
        if limits
        else "none set on the harness"
    )
    memory_text = (
        f"AgentCore Memory {summary['memoryArn']} (events per session, long-term records per actor)"
        if summary["memoryArn"]
        else "disabled on the harness"
    )
    model_kwargs = ["max_tokens=MODEL_MAX_TOKENS"]
    for key, param in (("temperature", "temperature"), ("topP", "top_p")):
        if key in model:
            model_kwargs.append(f"{param}={model[key]!r}")

    lines: list[str] = [
        f'"""LangGraph skeleton generated from the AgentCore harness `{name}`{version}.',
        "",
        f"Generated by platform_sdk.export.to_langgraph at {stamp} from the harness configuration. A starting point for the pro-code",
        "path, not a product: it mirrors the model, the system prompt, the gateway tools and the agent identity of the harness; memory,",
        "tracing, the managed runtime and the platform's release gate are yours to add (see the comments below).",
        "",
        "    uv sync --group langgraph         # langchain 1.x, langgraph, langchain-openai, langchain-core",
        f"    export {api_key_env}=sk-...   # a virtual key of the project's team in the model gateway (module 5)",
        f'    uv run python {name}_langgraph_agent.py "What is my leave balance? My employee id is E1001."',
        "",
        f"The model gateway is private: from a laptop, `workshop export --run` provides a localhost relay and sets {BASE_URL_ENV};",
        "inside the VPC the harness's own apiBase (the default below) is reachable directly.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "import argparse",
        "import json",
        "import os",
        "import sys",
        "",
        "import boto3",
        "from langchain_core.messages import AIMessage, HumanMessage",
        "from langchain_core.tools import StructuredTool",
        "from langchain.agents import create_agent  # LangChain 1.x: a compiled LangGraph graph (model and tools nodes)",
        "from langchain_openai import ChatOpenAI",
        "",
        "from platform_sdk import mcp",
        "",
        "# ---- model: the model gateway alias of the harness (model.liteLlmModelConfig) ----------------------------------------",
        f"MODEL_ALIAS = {_literal(model['alias'])}  # modelId {model['modelId']}: a LiteLLM alias, never a Bedrock model id (guardrail and spend attribution come with it)",
        "# apiBase: the OpenAI-compatible base URL of the model gateway. The gateway is private (reachable only from inside the",
        "# VPC): `workshop export --run` starts a localhost relay proxy and sets MODEL_GATEWAY_BASE to it; in the VPC leave it unset.",
        f"MODEL_GATEWAY_BASE = os.environ.get({_literal(BASE_URL_ENV)}, {_literal(model['apiBase'])})",
        f"MODEL_MAX_TOKENS = {model['maxTokens']}  # maxTokens per model call",
        f"API_KEY_ENV = {_literal(api_key_env)}",
        "# The harness fetches its key at run time from the AgentCore Identity provider below (apiKeyArn); code cannot, so the key",
        "# comes from the environment variable above: a virtual key of the project's team (module 5, platform_sdk.litellm); never commit it.",
        f"CREDENTIAL_PROVIDER_ARN = {_literal(model['apiKeyArn'])}",
        "",
        "# ---- tools: the environment's gateway (tools[].config.agentCoreGateway), called with SigV4 through platform_sdk.mcp -----",
    ]
    if gateway:
        lines += [
            f"GATEWAY_ARN = {_literal(gateway['arn'])}",
            f"GATEWAY_URL = {_literal(gateway['url'])}  # derived from the ARN; GetGateway shows the same gatewayUrl",
            f"ALLOWED_TOOLS = {_literal(summary['allowedTools'])}  # allowedTools {_literal(list(_unwrap(harness_config).get('allowedTools') or []))}: None = every tool the gateway lists for the caller",
        ]
    else:
        lines += [
            "GATEWAY_ARN = None  # the harness has no agentcore_gateway tool: no tools are wired up below",
            "GATEWAY_URL = None",
            "ALLOWED_TOOLS = []",
        ]
    lines += [
        f"AGENT_ROLE_ARN = {_literal(summary['executionRoleArn'])}  # executionRoleArn: the Cedar principal the gateway's policies permit",
        f"REGION = {_literal(summary['region'])}",
        "",
        "# ---- what the harness gave you for free and this skeleton does not -----------------------------------------------------",
        f"# memory: {memory_text}; in code, add a LangGraph checkpointer for the session and AgentCore Memory for the long-term records",
        f"# limits: {limits_text}; LangGraph has `recursion_limit` for the loop, the rest is your runtime's job",
        "# observability: the harness emits OpenTelemetry spans (gen_ai.*) that GenAI Observability and the evaluations read; add an",
        "#   OTel exporter (or LangSmith) and CloudWatch Transaction Search to get the same from this code",
        "# runtime and release gate: package this module for AgentCore Runtime (agentcore CLI or CDK) so that the platform's dataset",
        "#   evaluation and the online evaluation see its spans, and give it its own key, role and policies like the harness",
        "",
        "SYSTEM_PROMPT = " + _triple_quoted(summary["systemPrompt"]),
        "",
        "",
        "def _caller(name: str, session: boto3.Session | None):",
        '    """The function behind one StructuredTool: tools/call `name` on the gateway with the arguments the model produced."""',
        "",
        "    def call(**arguments):",
        "        try:",
        "            result = mcp.call_tool(GATEWAY_URL, name, arguments, session=session, region=REGION)",
        "        except (mcp.McpError, mcp.McpHttpError) as exc:  # a policy denial or an IAM refusal: tell the model, do not crash",
        '            return f"error: {exc}"',
        '        return f"error: {result.text}" if result.is_error else result.text',
        "",
        "    return call",
        "",
        "",
        "def gateway_tools(session: boto3.Session | None = None) -> list[StructuredTool]:",
        '    """One StructuredTool per gateway tool that tools/list returns for the caller, filtered by ALLOWED_TOOLS."""',
        "    if not GATEWAY_URL:",
        "        return []",
        "    tools = []",
        "    for spec in mcp.list_tools(GATEWAY_URL, session=session, region=REGION):",
        '        name = spec["name"]',
        "        if ALLOWED_TOOLS is not None and name not in ALLOWED_TOOLS:",
        "            continue",
        "        tools.append(",
        "            StructuredTool.from_function(",
        "                func=_caller(name, session),",
        "                name=name,",
        '                description=spec.get("description") or name,',
        '                args_schema=spec.get("inputSchema") or {"type": "object", "properties": {}},  # the gateway\'s JSON schema, as is',
        "            )",
        "        )",
        "    return tools",
        "",
        "",
        "def build_agent(api_key: str, session: boto3.Session | None = None):",
        '    """The agent: a LangGraph graph (create_agent) over ChatOpenAI pointed at the model gateway, the gateway tools and the prompt."""',
        "    model = ChatOpenAI(model=MODEL_ALIAS, api_key=api_key, base_url=MODEL_GATEWAY_BASE, "
        + ", ".join(model_kwargs)
        + ")",
        "    return create_agent(model, gateway_tools(session), system_prompt=SYSTEM_PROMPT)",
        "",
        "",
        "def main(argv: list[str] | None = None) -> int:",
        "    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)",
        '    parser.add_argument("prompt", nargs="+", help="one or more user messages (one conversation)")',
        "    parser.add_argument(",
        '        "--own-identity",',
        '        action="store_true",',
        '        help="call the gateway with your own AWS identity instead of assuming the harness\'s agent role",',
        "    )",
        "    args = parser.parse_args(argv)",
        "    api_key = os.environ.get(API_KEY_ENV)",
        "    if not api_key:",
        '        print(f"set {API_KEY_ENV} to a virtual key of the model gateway (see the module docstring)", file=sys.stderr)',
        "        return 2",
        "    session = None",
        "    if AGENT_ROLE_ARN and not args.own_identity:",
        '        session = mcp.assume_role_session(AGENT_ROLE_ARN, session_name="langgraph-export", region=REGION)',
        "    agent = build_agent(api_key, session)",
        "    for prompt in args.prompt:",
        '        state = agent.invoke({"messages": [HumanMessage(prompt)]})',
        '        messages = state["messages"]',
        "        print(str(messages[-1].content).strip())",
        '        tools = [call["name"] for m in messages if isinstance(m, AIMessage) for call in (m.tool_calls or [])]',
        "        usage: dict[str, int] = {}",
        "        for m in messages:",
        '            for key, value in (getattr(m, "usage_metadata", None) or {}).items():',
        "                if isinstance(value, int):",
        "                    usage[key] = usage.get(key, 0) + value",
        '        print(json.dumps({"usage": usage, "tools": tools}), file=sys.stderr)',
        "    return 0",
        "",
        "",
        'if __name__ == "__main__":',
        "    sys.exit(main())",
        "",
    ]
    return "\n".join(lines)
