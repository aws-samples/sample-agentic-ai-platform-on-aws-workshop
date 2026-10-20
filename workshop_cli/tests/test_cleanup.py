from types import SimpleNamespace

from platform_sdk import names
from workshop_cli import cleanup


def _names(project_id, finance="invoice-helper"):
    """The name sets Cleanup derives, without AWS: mirrors Cleanup.__init__."""
    by_project = {pid: names.for_project(pid) for pid in (project_id, finance)}
    env_names = [n for by_env in by_project.values() for n in by_env.values()]
    return SimpleNamespace(
        agentcore_prefixes=tuple(n.policy("") for n in env_names),
        key_prefixes=tuple(n.key_alias("") for n in env_names),
        role_prefixes=tuple(f"{names.ROLE_PREFIX}-{n.project_id}-{n.env}-" for n in env_names),
        harness_names=sorted(n.harness for n in env_names),
        obo_harness=by_project[project_id]["dev"].obo_harness,
        obo_gateway=by_project[project_id]["dev"].obo_gateway,
    )


def test_prefixes_are_anchored_on_the_environment_segment():
    ours = cleanup.Cleanup.ours
    n = _names("hr-assistant")
    assert ours(None, "hr_assistant_dev_allow_get_leave_balance", n.agentcore_prefixes)
    assert ours(None, "hr_assistant_prod_online_eval", n.agentcore_prefixes)
    assert not ours(None, "hr_assistant_2_dev_allow_x", n.agentcore_prefixes)  # a project whose id merely extends ours
    assert ours(None, "hr-assistant-dev-claude-sonnet-5-5", n.key_prefixes)
    assert not ours(None, "hr-assistant-2-dev-claude-sonnet-5-5", n.key_prefixes)
    assert ours(None, "agent-platform-proj-invoice-helper-dev-agent", n.role_prefixes)
    assert n.harness_names == ["hr_assistant_dev", "hr_assistant_prod", "invoice_helper_dev", "invoice_helper_prod"]


def test_print_inventory_counts_only_our_objects(capsys):
    inv = {
        "ours": {"harnesses": ["a (READY)"], "policies": ["p1", "p2"], "logGroups": []},
        "shared": {"policyEngine": "pe-1", "otherPolicies": ["x"]},
        "other": {"projects": []},
    }
    assert cleanup.Cleanup.print_inventory(inv) == 3
    out = capsys.readouterr().out
    assert "harnesses            1: a (READY)" in out and "logGroups            none" in out


def test_module_8_objects_are_part_of_the_cleanup_names():
    """Cleanup.__init__ adds the OBO harness and gateway, the two OAuth2 providers and the demo workload identity (cleanup.py)."""
    import inspect

    from platform_sdk import names as n

    source = inspect.getsource(cleanup.Cleanup.__init__)
    names_ = _names("hr-assistant")
    assert "dev.obo_harness" in source and "dev.obo_gateway" in source
    assert names_.obo_harness == "hr_assistant_dev_obo" and names_.obo_gateway == "hr-assistant-dev-gw-obo"
    assert "names.OBO_PROVIDER_AGENT" in source and "names.OBO_PROVIDER_GATEWAY" in source
    assert "names.OBO_DEMO_WORKLOAD" in source and n.OBO_DEMO_WORKLOAD == "workshop_obo_demo"
    assert "harness_{dev.obo_harness}-" in source  # the OBO harness log group prefix
