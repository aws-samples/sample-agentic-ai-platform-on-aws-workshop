"""foundation/azs.py: zone names for the AgentCore-supported zone ids from the context cache, else a lookup, else the fallback.

No AWS call is ever made: the EC2 client is a fake handed in through `client_factory`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from aws_cdk import App
from botocore.exceptions import NoCredentialsError

FOUNDATION = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FOUNDATION))

from foundation import azs, config  # noqa: E402

ACCOUNT, REGION = "123456789012", "us-east-1"
KEY = f"agent-platform:harness-azs:{ACCOUNT}:{REGION}"
# One account's mapping: the supported ids use1-az1/az2/az4 carry names that are not simply a, b, c there
MAPPING = {"use1-az4": "us-east-1a", "use1-az1": "us-east-1f", "use1-az2": "us-east-1b", "use1-az6": "us-east-1c"}


class FakeEc2:
    """Stands in for the boto3 EC2 client: records the calls and answers from a zone id -> name mapping."""

    def __init__(self, mapping: dict[str, str], states: dict[str, str] | None = None) -> None:
        self.mapping, self.states, self.calls = mapping, states or {}, []

    def describe_availability_zones(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "AvailabilityZones": [
                {"ZoneId": zone_id, "ZoneName": name, "State": self.states.get(zone_id, "available")}
                for zone_id, name in self.mapping.items()
                if zone_id in kwargs["ZoneIds"]
            ]
        }


def no_lookup():
    raise AssertionError("no lookup expected when the context has the entry")


def test_context_entry_wins_without_any_call(tmp_path) -> None:
    app = App(context={KEY: ["us-east-1c", "us-east-1a", "us-east-1f"]})
    names = azs.resolve_harness_azs(app, ACCOUNT, REGION, client_factory=no_lookup, context_file=tmp_path / "c.json")
    assert names == ["us-east-1c", "us-east-1a", "us-east-1f"]


def test_context_entry_may_be_a_comma_separated_string(tmp_path) -> None:
    """`cdk synth -c agent-platform:harness-azs:...=a,b,c` hands the value over as one string."""
    app = App(context={KEY: "us-east-1c, us-east-1a,us-east-1f"})
    names = azs.resolve_harness_azs(app, ACCOUNT, REGION, client_factory=no_lookup, context_file=tmp_path / "c.json")
    assert names == ["us-east-1c", "us-east-1a", "us-east-1f"]


def test_lookup_orders_names_by_zone_id_and_caches_next_to_the_cli_entries(tmp_path) -> None:
    context_file = tmp_path / "cdk.context.json"
    cli_key = f"availability-zones:account={ACCOUNT}:region={REGION}"
    context_file.write_text(json.dumps({cli_key: ["us-east-1a", "us-east-1b"]}))
    fake = FakeEc2(MAPPING)

    names = azs.resolve_harness_azs(App(), ACCOUNT, REGION, client_factory=lambda: fake, context_file=context_file)

    assert config.HARNESS_AZ_IDS == ["use1-az1", "use1-az2", "use1-az4"]
    assert names == ["us-east-1f", "us-east-1b", "us-east-1a"]  # the order of config.HARNESS_AZ_IDS, not of the answer
    assert fake.calls == [{"ZoneIds": config.HARNESS_AZ_IDS}]
    cached = json.loads(context_file.read_text())
    assert cached == {cli_key: ["us-east-1a", "us-east-1b"], KEY: names}  # merged, the CLI's own entry kept


def test_cached_entry_is_used_by_the_next_synth(tmp_path) -> None:
    """What the CLI does with cdk.context.json: its content becomes the App's context on the next run."""
    context_file = tmp_path / "cdk.context.json"
    fake = FakeEc2(MAPPING)
    first = azs.resolve_harness_azs(App(), ACCOUNT, REGION, client_factory=lambda: fake, context_file=context_file)
    app = App(context=json.loads(context_file.read_text()))
    second = azs.resolve_harness_azs(app, ACCOUNT, REGION, client_factory=no_lookup, context_file=context_file)
    assert first == second and len(fake.calls) == 1


def test_without_credentials_the_fallback_is_used_and_nothing_is_cached(tmp_path, capsys) -> None:
    def no_credentials():
        raise NoCredentialsError()

    context_file = tmp_path / "cdk.context.json"
    names = azs.resolve_harness_azs(App(), ACCOUNT, REGION, client_factory=no_credentials, context_file=context_file)
    assert names == config.FALLBACK_AZS == ["us-east-1a", "us-east-1b", "us-east-1c"]
    assert names is not config.FALLBACK_AZS  # a copy: callers may not mutate the constant
    assert not context_file.exists()
    assert "NoCredentialsError" in capsys.readouterr().err


def test_an_unavailable_zone_means_the_fallback(tmp_path) -> None:
    fake = FakeEc2(MAPPING, states={"use1-az4": "impaired"})
    names = azs.resolve_harness_azs(
        App(), ACCOUNT, REGION, client_factory=lambda: fake, context_file=tmp_path / "c.json"
    )
    assert names == config.FALLBACK_AZS
    assert not (tmp_path / "c.json").exists()


def test_unknown_account_looks_up_but_does_not_cache(tmp_path) -> None:
    """No CDK_DEFAULT_ACCOUNT (env-agnostic synth): the key would be meaningless, the names are still the real ones."""
    fake = FakeEc2(MAPPING)
    names = azs.resolve_harness_azs(App(), None, REGION, client_factory=lambda: fake, context_file=tmp_path / "c.json")
    assert names == ["us-east-1f", "us-east-1b", "us-east-1a"]
    assert not (tmp_path / "c.json").exists()


def test_constants_agree() -> None:
    assert len(config.HARNESS_AZ_IDS) == len(config.FALLBACK_AZS) == 3
    assert 1 <= config.ENDPOINT_AZ_COUNT <= len(config.HARNESS_AZ_IDS)
    assert azs.context_key(ACCOUNT, REGION) == KEY
