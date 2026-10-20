from workshop_cli import cli, tools


def test_record_command_runs_without_aws(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "scenario").mkdir()
    (tmp_path / "platform_sdk").mkdir()
    (tmp_path / "design-record.yaml").write_text("module_4:\n  inputs: {max_leave_days: 12}\n")
    assert cli.main(["record"]) == 0
    out = capsys.readouterr().out
    assert "max_leave_days" in out and "12" in out and "(record)" in out


def test_invalid_record_is_reported(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "scenario").mkdir()
    (tmp_path / "platform_sdk").mkdir()
    (tmp_path / "design-record.yaml").write_text("module_6:\n  inputs: {prompt_version: v9}\n")
    assert cli.main(["record"]) == 2
    assert "prompt_version" in capsys.readouterr().err


def test_tool_shortcuts_build_arguments():
    name, args = tools._arguments("get_leave_balance", ["E1002"])
    assert name == "hrtools___get_leave_balance" and args == {"employee_id": "E1002"}
    name, args = tools._arguments("submit_leave_request", ["E1001", "15"])
    assert name == "hrtools___submit_leave_request" and args["days"] == 15 and args["leave_type"] == "annual"
    name, args = tools._arguments("retrieve", ["remote", "work"])
    assert name == "hrkb___Retrieve" and args == {"retrievalQuery": {"text": "remote work"}}
    name, args = tools._arguments("hrtools___get_public_holidays", ['{"year": 2026}'])
    assert args == {"year": 2026}
