"""platform_sdk.export: the Strands skeleton generated from a harness configuration (no network: mcp is monkeypatched)."""

from __future__ import annotations

import importlib.util
import py_compile
from datetime import UTC, datetime

import pytest

from platform_sdk import export, mcp

ACCOUNT = "123456789012"
PROMPT = '# AnyCompany HR Assistant\n\nUse the tools. Quote the policy ("according to ...") and keep answers short.\n'
GATEWAY_ARN = f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT}:gateway/hr-assistant-dev-gw-tprllkjtys"
PROVIDER_ARN = (
    f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT}:token-vault/default/apikeycredentialprovider/"
    "litellm-hr-assistant-dev-claude-sonnet-5"
)
MEMORY_ARN = f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT}:memory/hr_assistant_dev_memory-enx4hYBx2S"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/agent-platform-proj-hr-assistant-dev-agent"

GET_HARNESS = {  # the shape GetHarness returns (verified live 9 Oct 2026)
    "harness": {
        "harnessId": "hr_assistant_dev-322gsLlVkN",
        "harnessName": "hr_assistant_dev",
        "arn": f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT}:harness/hr_assistant_dev-322gsLlVkN",
        "status": "READY",
        "harnessVersion": "6",
        "executionRoleArn": ROLE_ARN,
        "model": {
            "liteLlmModelConfig": {
                "modelId": "openai/claude-sonnet-5",
                "apiKeyArn": PROVIDER_ARN,
                "apiBase": "https://llm.example.test/v1",
                "maxTokens": 2048,
            }
        },
        "systemPrompt": [{"text": PROMPT}],
        "tools": [
            {
                "type": "agentcore_gateway",
                "name": "gateway",
                "config": {"agentCoreGateway": {"gatewayArn": GATEWAY_ARN, "outboundAuth": {"awsIam": {}}}},
            }
        ],
        "skills": [],
        "allowedTools": ["@gateway"],
        "memory": {"agentCoreMemoryConfiguration": {"arn": MEMORY_ARN, "retrievalConfig": {}}},
        "hooks": [],
        "maxIterations": 30,
        "maxTokens": 4000,
        "timeoutSeconds": 120,
    }
}

RENDER = {  # GET /projects/{id}/blueprints/{v}/render (CreateHarness kwargs + tags), explicit allow list, no memory
    "harnessName": "hr_assistant_dev",
    "executionRoleArn": ROLE_ARN,
    "model": {
        "liteLlmModelConfig": {
            "modelId": "openai/gpt-6-1-sol",
            "apiBase": "https://llm.example.test/v1",
            "apiKeyArn": PROVIDER_ARN,
            "maxTokens": 1024,
            "temperature": 0.2,
        }
    },
    "systemPrompt": [{"text": "Be brief."}],
    "tools": [
        {
            "type": "agentcore_gateway",
            "name": "gateway",
            "config": {"agentCoreGateway": {"gatewayArn": GATEWAY_ARN, "outboundAuth": {"awsIam": {}}}},
        }
    ],
    "allowedTools": ["@gateway/hrtools___get_leave_balance", "@gateway/hrkb___Retrieve"],
    "memory": {"optionalValue": {"disabled": {}}},
    "maxIterations": 10,
    "tags": {"ManagedBy": "platform"},
}

GATEWAY_TOOLS = [
    {
        "name": "hrtools___get_leave_balance",
        "description": "Remaining leave of an employee",
        "inputSchema": {
            "type": "object",
            "properties": {"employee_id": {"type": "string"}},
            "required": ["employee_id"],
        },
    },
    {
        "name": "hrtools___submit_leave_request",
        "description": "File a leave request",
        "inputSchema": {"type": "object", "properties": {"employee_id": {"type": "string"}}},
    },
    {"name": "hrkb___Retrieve", "description": "Search the HR policies"},
]


def load_generated(source: str, tmp_path, filename: str = "hr_assistant_dev_agent.py"):
    """Write the generated source, byte-compile it and import it as a module (`workshop export` does the same)."""
    path = tmp_path / filename
    path.write_text(source)
    py_compile.compile(str(path), doraise=True)
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_summarize_reads_the_get_harness_shape(cfg):
    summary = export.summarize(GET_HARNESS, cfg)
    assert summary["name"] == "hr_assistant_dev" and summary["version"] == "6"
    assert summary["executionRoleArn"] == ROLE_ARN and summary["region"] == "us-east-1"
    assert summary["model"] == {
        "alias": "claude-sonnet-5",
        "modelId": "openai/claude-sonnet-5",
        "apiBase": "https://llm.example.test/v1",
        "apiKeyArn": PROVIDER_ARN,
        "maxTokens": 2048,
    }
    assert summary["systemPrompt"] == PROMPT
    assert summary["gateway"] == {
        "name": "gateway",
        "arn": GATEWAY_ARN,
        "id": "hr-assistant-dev-gw-tprllkjtys",
        "url": "https://hr-assistant-dev-gw-tprllkjtys.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
    }
    assert summary["allowedTools"] is None  # "@gateway" = every tool
    assert summary["memoryArn"] == MEMORY_ARN
    assert summary["limits"] == {"maxIterations": 30, "maxTokens": 4000, "timeoutSeconds": 120}


def test_summarize_reads_the_render_payload_with_an_explicit_allow_list(cfg):
    summary = export.summarize(RENDER, cfg)
    assert summary["version"] is None and summary["model"]["alias"] == "gpt-6-1-sol"
    assert summary["model"]["temperature"] == 0.2 and summary["model"]["maxTokens"] == 1024
    assert summary["allowedTools"] == ["hrtools___get_leave_balance", "hrkb___Retrieve"]
    assert summary["memoryArn"] is None and summary["limits"] == {"maxIterations": 10}


def test_summarize_falls_back_to_the_platform_config_and_rejects_a_config_without_model(cfg):
    without_base = {**RENDER, "model": {"liteLlmModelConfig": {"modelId": "openai/nova-2-lite"}}}
    assert export.summarize(without_base, cfg)["model"]["apiBase"] == "https://llm.example.test/v1"
    with pytest.raises(ValueError, match="apiBase"):
        export.summarize(without_base)
    with pytest.raises(ValueError, match="modelId"):
        export.summarize({"harnessName": "x", "systemPrompt": [{"text": "hi"}]}, cfg)
    with pytest.raises(ValueError, match="configuration dict"):
        export.summarize("not a dict", cfg)
    with pytest.raises(ValueError, match="gateway ARN"):
        export.gateway_url_from_arn("arn:aws:bedrock-agentcore:us-east-1:123:memory/m")


def test_to_strands_compiles_imports_and_mirrors_the_configuration(cfg, tmp_path, monkeypatch):
    source = export.to_strands(GET_HARNESS, cfg, generated_at=datetime(2026, 10, 9, 5, 0, tzinfo=UTC))
    assert source.startswith(
        '"""Strands Agents skeleton generated from the AgentCore harness `hr_assistant_dev` (version 6).'
    )
    assert "2026-10-09T05:00:00+00:00" in source
    module = load_generated(source, tmp_path)
    assert module.MODEL_ALIAS == "claude-sonnet-5"
    assert module.MODEL_GATEWAY_BASE == "https://llm.example.test/v1" and module.MODEL_MAX_TOKENS == 2048
    assert module.API_KEY_ENV == "MODEL_GATEWAY_API_KEY" and module.CREDENTIAL_PROVIDER_ARN == PROVIDER_ARN
    assert module.GATEWAY_ARN == GATEWAY_ARN and module.ALLOWED_TOOLS is None
    assert (
        module.GATEWAY_URL
        == "https://hr-assistant-dev-gw-tprllkjtys.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
    )
    assert module.AGENT_ROLE_ARN == ROLE_ARN and module.REGION == "us-east-1"
    assert module.SYSTEM_PROMPT == PROMPT  # exact round trip, quotes and newlines included
    assert MEMORY_ARN in source and "maxIterations 30" in source and "timeoutSeconds 120" in source  # as comments

    calls: list[tuple] = []

    def fake_list_tools(url, session=None, region=None, **kw):
        calls.append(("list", url, region))
        return GATEWAY_TOOLS

    def fake_call_tool(url, name, arguments=None, session=None, region=None, **kw):
        calls.append(("call", url, name, arguments))
        if name == "hrtools___submit_leave_request":
            raise mcp.McpError(-32002, "Tool Execution Denied: Tool call not allowed due to policy enforcement")
        return mcp.ToolResult(name=name, content=[{"type": "text", "text": '{"annual_leave_balance": 12}'}])

    monkeypatch.setattr(module.mcp, "list_tools", fake_list_tools)
    monkeypatch.setattr(module.mcp, "call_tool", fake_call_tool)
    tools = module.gateway_tools()
    assert [t.tool_name for t in tools] == [
        "hrtools___get_leave_balance",
        "hrtools___submit_leave_request",
        "hrkb___Retrieve",
    ]
    assert tools[0].tool_spec == {
        "name": "hrtools___get_leave_balance",
        "description": "Remaining leave of an employee",
        "inputSchema": {"json": GATEWAY_TOOLS[0]["inputSchema"]},
    }
    assert tools[2].tool_spec["inputSchema"] == {"json": {"type": "object", "properties": {}}}  # schema-less tool
    balance = tools[0]._tool_func({"toolUseId": "t1", "name": tools[0].tool_name, "input": {"employee_id": "E1001"}})
    assert balance == {"toolUseId": "t1", "status": "success", "content": [{"text": '{"annual_leave_balance": 12}'}]}
    denied = tools[1]._tool_func({"toolUseId": "t2", "name": tools[1].tool_name, "input": {"employee_id": "E1012"}})
    assert denied["status"] == "error" and "policy enforcement" in denied["content"][0]["text"]
    assert calls[0] == ("list", module.GATEWAY_URL, "us-east-1")
    assert calls[1] == ("call", module.GATEWAY_URL, "hrtools___get_leave_balance", {"employee_id": "E1001"})
    assert module.main(["hello"]) == 2  # no key in the environment: says so, no network


@pytest.mark.parametrize("generator", [export.to_strands, export.to_langgraph])
def test_generated_module_reads_model_gateway_base_from_the_environment(cfg, tmp_path, monkeypatch, generator):
    """The gateway is private: `workshop export --run` points MODEL_GATEWAY_BASE at a localhost relay proxy; unset, the module
    keeps the harness's apiBase (code deployed inside the VPC reaches it directly)."""
    source = generator(GET_HARNESS, cfg)
    assert 'MODEL_GATEWAY_BASE = os.environ.get("MODEL_GATEWAY_BASE", "https://llm.example.test/v1")' in source
    assert "private" in source and "workshop export --run" in source  # the docstring says why
    monkeypatch.delenv(export.BASE_URL_ENV, raising=False)
    module = load_generated(source, tmp_path, f"unset_{generator.__name__}.py")
    assert module.MODEL_GATEWAY_BASE == "https://llm.example.test/v1"
    monkeypatch.setenv(export.BASE_URL_ENV, "http://127.0.0.1:43123/v1")
    module = load_generated(source, tmp_path, f"set_{generator.__name__}.py")
    assert module.MODEL_GATEWAY_BASE == "http://127.0.0.1:43123/v1"


def test_to_strands_filters_the_allow_list_and_passes_model_parameters(cfg, tmp_path, monkeypatch):
    module = load_generated(export.to_strands(RENDER, cfg), tmp_path)
    assert module.ALLOWED_TOOLS == ["hrtools___get_leave_balance", "hrkb___Retrieve"]
    assert module.MODEL_ALIAS == "gpt-6-1-sol" and module.MODEL_MAX_TOKENS == 1024
    monkeypatch.setattr(module.mcp, "list_tools", lambda url, session=None, region=None, **kw: GATEWAY_TOOLS)
    assert [t.tool_name for t in module.gateway_tools()] == ["hrtools___get_leave_balance", "hrkb___Retrieve"]
    assert "disabled on the harness" in export.to_strands(RENDER, cfg)  # memory comment
    assert '"temperature": 0.2' in export.to_strands(RENDER, cfg)


@pytest.mark.parametrize(
    "text",
    [
        'Say """hello""" and C:\\path\\ then end with a quote"',
        'x"""',  # ends in triple quotes: the final quote is already escaped by the triple-quote rewrite
        'x\\"""',  # a backslash before the triple quotes
        "ends with a backslash\\",
        'ends with an escaped quote \\"',
        "trailing newline\n",
        "",
    ],
)
def test_triple_quoted_round_trips_awkward_endings(text):
    literal = export._triple_quoted(text)
    assert literal.startswith('"""\\\n') and literal.endswith('"""')
    assert eval(literal) == text  # noqa: S307 - the literal is the thing under test


def test_to_strands_without_a_gateway_tool_and_with_an_awkward_prompt(cfg, tmp_path):
    awkward = 'Say """hello""" and C:\\path\\ then end with a quote"'
    config = {
        **RENDER,
        "tools": [],
        "allowedTools": [],
        "systemPrompt": [{"text": awkward}],
        "memory": {"disabled": {}},
    }
    module = load_generated(export.to_strands(config, cfg), tmp_path)
    assert module.SYSTEM_PROMPT == awkward
    assert module.GATEWAY_URL is None and module.gateway_tools() == []


def test_to_langgraph_compiles_imports_and_mirrors_the_configuration(cfg, tmp_path, monkeypatch):
    source = export.to_langgraph(GET_HARNESS, cfg, generated_at=datetime(2026, 10, 14, 5, 0, tzinfo=UTC))
    assert source.startswith(
        '"""LangGraph skeleton generated from the AgentCore harness `hr_assistant_dev` (version 6).'
    )
    assert "2026-10-14T05:00:00+00:00" in source and "create_agent" in source
    module = load_generated(source, tmp_path, "hr_assistant_dev_langgraph_agent.py")
    assert module.MODEL_ALIAS == "claude-sonnet-5"
    assert module.MODEL_GATEWAY_BASE == "https://llm.example.test/v1" and module.MODEL_MAX_TOKENS == 2048
    assert module.API_KEY_ENV == "MODEL_GATEWAY_API_KEY" and module.CREDENTIAL_PROVIDER_ARN == PROVIDER_ARN
    assert module.GATEWAY_ARN == GATEWAY_ARN and module.ALLOWED_TOOLS is None
    assert module.AGENT_ROLE_ARN == ROLE_ARN and module.REGION == "us-east-1"
    assert module.SYSTEM_PROMPT == PROMPT
    assert MEMORY_ARN in source and "maxIterations 30" in source  # as comments

    calls: list[tuple] = []

    def fake_list_tools(url, session=None, region=None, **kw):
        calls.append(("list", url, region))
        return GATEWAY_TOOLS

    def fake_call_tool(url, name, arguments=None, session=None, region=None, **kw):
        calls.append(("call", url, name, arguments))
        if name == "hrtools___submit_leave_request":
            raise mcp.McpError(-32002, "Tool Execution Denied: Tool call not allowed due to policy enforcement")
        return mcp.ToolResult(name=name, content=[{"type": "text", "text": '{"annual_leave_balance": 12}'}])

    monkeypatch.setattr(module.mcp, "list_tools", fake_list_tools)
    monkeypatch.setattr(module.mcp, "call_tool", fake_call_tool)
    tools = module.gateway_tools()
    assert [t.name for t in tools] == [
        "hrtools___get_leave_balance",
        "hrtools___submit_leave_request",
        "hrkb___Retrieve",
    ]
    assert tools[0].args == {"employee_id": {"type": "string"}}  # the gateway's JSON schema drives the tool's arguments
    assert tools[0].invoke({"employee_id": "E1001"}) == '{"annual_leave_balance": 12}'
    denied = tools[1].invoke({"employee_id": "E1012"})
    assert denied.startswith("error:") and "policy enforcement" in denied
    assert calls[0] == ("list", module.GATEWAY_URL, "us-east-1")
    assert calls[1] == ("call", module.GATEWAY_URL, "hrtools___get_leave_balance", {"employee_id": "E1001"})
    agent = module.build_agent("sk-test")  # compiles the LangGraph graph without any network call
    nodes = set(agent.get_graph().nodes)
    assert {"model", "tools"} <= nodes, nodes
    assert module.main(["hello"]) == 2  # no key in the environment: says so, no network


def test_to_langgraph_filters_the_allow_list_and_passes_model_parameters(cfg, tmp_path, monkeypatch):
    source = export.to_langgraph(RENDER, cfg)
    module = load_generated(source, tmp_path, "render_langgraph_agent.py")
    assert module.ALLOWED_TOOLS == ["hrtools___get_leave_balance", "hrkb___Retrieve"]
    assert module.MODEL_ALIAS == "gpt-6-1-sol" and module.MODEL_MAX_TOKENS == 1024
    monkeypatch.setattr(module.mcp, "list_tools", lambda url, session=None, region=None, **kw: GATEWAY_TOOLS)
    assert [t.name for t in module.gateway_tools()] == ["hrtools___get_leave_balance", "hrkb___Retrieve"]
    assert "disabled on the harness" in source and "temperature=0.2" in source
    without_gateway = {**RENDER, "tools": [], "allowedTools": [], "memory": {"disabled": {}}}
    bare = load_generated(export.to_langgraph(without_gateway, cfg), tmp_path, "bare_langgraph_agent.py")
    assert bare.GATEWAY_URL is None and bare.gateway_tools() == []
