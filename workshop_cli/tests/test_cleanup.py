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
    assert ours(None, "hr-assistant-dev-claude-sonnet-5", n.key_prefixes)
    assert not ours(None, "hr-assistant-2-dev-claude-sonnet-5", n.key_prefixes)
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


class _FakeEc2:
    """describe_network_interfaces pages served from a queue, one list per call."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0
        self.last_filters = None

    def get_paginator(self, name):
        assert name == "describe_network_interfaces"
        fake = self

        class Paginator:
            def paginate(self, Filters):
                fake.calls += 1
                fake.last_filters = Filters
                enis = fake.responses.pop(0) if fake.responses else []
                return [{"NetworkInterfaces": enis}]

        return Paginator()


def _eni(eni_id, description="AgentCore runtime", managed=True):
    return {"NetworkInterfaceId": eni_id, "Status": "in-use", "Description": description, "RequesterManaged": managed}


def _cfg(network):
    return SimpleNamespace(harness_network=lambda: network)


def test_wait_for_vpc_interfaces_waits_for_the_harness_security_group_then_the_subnets(monkeypatch):
    monkeypatch.setattr(cleanup.time, "sleep", lambda _s: None)
    network = {"subnets": ["subnet-1", "subnet-2", "subnet-3"], "securityGroups": ["sg-harness"]}
    ec2 = _FakeEc2([_eni("eni-a"), _eni("eni-b")], [_eni("eni-a")], [])
    assert cleanup.wait_for_vpc_interfaces(_cfg(network), "us-east-1", ec2=ec2, every=0) is True
    assert ec2.calls == 3 and ec2.last_filters == [{"Name": "group-id", "Values": ["sg-harness"]}]
    # after a DELETE_FAILED the wait covers every interface left in the harness subnets (the relay Lambda's as well)
    ec2 = _FakeEc2([_eni("eni-relay", "AWS Lambda VPC ENI")], [])
    assert cleanup.wait_for_vpc_interfaces(_cfg(network), "us-east-1", by_subnet=True, ec2=ec2, every=0) is True
    assert ec2.last_filters == [{"Name": "subnet-id", "Values": ["subnet-1", "subnet-2", "subnet-3"]}]


def test_wait_for_vpc_interfaces_gives_up_after_the_timeout_and_skips_public_networks(monkeypatch):
    monkeypatch.setattr(cleanup.time, "sleep", lambda _s: None)
    network = {"subnets": ["subnet-1"], "securityGroups": ["sg-harness"]}
    ec2 = _FakeEc2(*([[_eni("eni-stuck")]] * 5))
    assert cleanup.wait_for_vpc_interfaces(_cfg(network), "us-east-1", ec2=ec2, timeout=0, every=0) is False
    assert ec2.calls == 1
    # a public landing zone (no harness network in SSM) and a missing configuration: nothing to wait for, no EC2 call
    assert cleanup.wait_for_vpc_interfaces(_cfg(None), "us-east-1", ec2=_FakeEc2()) is True
    assert cleanup.wait_for_vpc_interfaces(None, "us-east-1", ec2=_FakeEc2()) is True
    assert cleanup.vpc_interfaces(_FakeEc2(), None, [], by_subnet=False) == []
