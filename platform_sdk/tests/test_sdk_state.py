import json

from platform_sdk import state


def test_get_set_delete_save_roundtrip(tmp_path):
    path = tmp_path / ".workshop" / "state" / "hr-assistant.json"
    s = state.WorkshopState(path)
    assert s.get("gateway.dev.arn") is None and s.get("x", "d") == "d"
    s.set("gateway.dev.arn", "arn:gw")
    s.set("gateway.dev.id", "gw-1")
    s.set("memory", {"dev": "arn:mem"})
    assert path.exists() and json.loads(path.read_text()) == {
        "gateway": {"dev": {"arn": "arn:gw", "id": "gw-1"}},
        "memory": {"dev": "arn:mem"},
    }
    reloaded = state.WorkshopState(path)
    assert reloaded.get("gateway.dev") == {"arn": "arn:gw", "id": "gw-1"} and "memory.dev" in reloaded
    reloaded.delete("gateway.dev.id")
    reloaded.delete("missing.key")
    assert state.WorkshopState(path).get("gateway.dev") == {"arn": "arn:gw"}
    assert reloaded.require("memory.dev") == "arn:mem"
    try:
        reloaded.require("harness.dev.arn", module="6")
    except KeyError as exc:
        assert "workshop apply 6" in str(exc)
    else:
        raise AssertionError("require should fail for a missing key")


def test_default_path_follows_repository_root_and_env_override(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "scenario" / "blueprint").mkdir(parents=True)
    (repo / "platform_sdk").mkdir()
    # an explicit root never depends on the working directory
    assert state.default_path("hr-assistant", root=repo) == repo / ".workshop" / "state" / "hr-assistant.json"
    # from the root and from a nested directory the repository root is found by its scenario/ and platform_sdk/ folders
    monkeypatch.chdir(repo)
    assert state.default_path() == repo / ".workshop" / "state" / "hr-assistant.json"
    monkeypatch.chdir(repo / "scenario" / "blueprint")
    assert state.default_path("invoice-helper") == repo / ".workshop" / "state" / "invoice-helper.json"
    # outside the repository the working directory is the base
    monkeypatch.chdir(tmp_path)
    assert state.default_path() == tmp_path / ".workshop" / "state" / "hr-assistant.json"
    # the environment override wins over everything
    monkeypatch.setenv("PLATFORM_STATE_DIR", str(tmp_path / "elsewhere"))
    assert state.WorkshopState.for_project().path == tmp_path / "elsewhere" / "hr-assistant.json"
    assert state.WorkshopState.for_project(root=repo).path == tmp_path / "elsewhere" / "hr-assistant.json"


def test_set_stores_a_copy_and_never_aliases_the_callers_objects(tmp_path):
    from platform_sdk.state import WorkshopState

    st = WorkshopState(tmp_path / "s.json")
    memories = {"dev": {"id": "m-1"}, "prod": {"id": "m-2"}}
    st.set("memory", memories)
    st.set("memory.sample", {"actorId": "E1001"})  # nested set into the stored section
    assert memories == {"dev": {"id": "m-1"}, "prod": {"id": "m-2"}}, "the caller's dict must not gain the nested key"
    memories["dev"]["id"] = "changed"
    assert st.get("memory.dev.id") == "m-1"
    assert st.get("memory.sample") == {"actorId": "E1001"}
    reloaded = WorkshopState(tmp_path / "s.json")
    assert reloaded.get("memory") == {"dev": {"id": "m-1"}, "prod": {"id": "m-2"}, "sample": {"actorId": "E1001"}}
    assert repr(reloaded).startswith("WorkshopState(")
