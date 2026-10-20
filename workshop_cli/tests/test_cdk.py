import pytest
from workshop_cli.context import WorkshopError

from workshop_cli import cdk


def test_cdk_command_runs_the_pinned_cli_through_node(tmp_path, monkeypatch):
    entry = tmp_path / "node_modules" / "aws-cdk" / "bin" / "cdk"
    entry.parent.mkdir(parents=True)
    entry.write_text("#!/usr/bin/env node\n")
    monkeypatch.setattr(cdk.shutil, "which", lambda name: "/usr/bin/node" if name == "node" else None)
    assert cdk.cdk_command(tmp_path) == ["/usr/bin/node", str(entry)]


def test_cdk_command_explains_a_missing_install(tmp_path, monkeypatch):
    monkeypatch.setattr(cdk.shutil, "which", lambda name: "/usr/bin/node")
    with pytest.raises(WorkshopError, match="npm install"):
        cdk.cdk_command(tmp_path)
    monkeypatch.setattr(cdk.shutil, "which", lambda name: None)
    with pytest.raises(WorkshopError, match="Node.js"):
        cdk.cdk_command(tmp_path)


def test_frontend_dist_is_rebuilt_when_missing_or_older_than_a_source(tmp_path):
    frontend = tmp_path / "frontend"
    (frontend / "src").mkdir(parents=True)
    source = frontend / "src" / "App.tsx"
    source.write_text("export {}\n")
    assert cdk.frontend_dist_stale(tmp_path) == "frontend/dist is missing"
    dist = frontend / "dist"
    dist.mkdir()
    index = dist / "index.html"
    index.write_text("<html></html>")
    assert cdk.frontend_dist_stale(tmp_path) is None
    # a source edited after the build (the lab-era UI kept being deployed from a stale dist)
    import os

    built = index.stat().st_mtime
    os.utime(source, (built + 60, built + 60))
    assert cdk.frontend_dist_stale(tmp_path) == "frontend/dist is older than frontend/src/App.tsx"
    os.utime(index, (built + 120, built + 120))
    assert cdk.frontend_dist_stale(tmp_path) is None


def test_scopes_cover_the_eight_stacks():
    assert len(cdk.SCOPES["all"]) == 7  # the platform; the IdP stack of module 2 is deployed by `workshop apply 2`
    assert set(cdk.SCOPES["pre-day"]) | set(cdk.SCOPES["landing-zone"]) == set(cdk.SCOPES["all"])
    assert cdk.SCOPES["idp"] == ["AgentPlatform-Idp"] and len(cdk.EVERYTHING) == 8
    assert set(cdk.EVERYTHING) == set(cdk.SCOPES["all"]) | set(cdk.SCOPES["idp"])
    assert all(name in cdk.CHAIN for name in cdk.EVERYTHING)
