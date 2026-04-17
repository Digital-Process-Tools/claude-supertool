from __future__ import annotations

from pathlib import Path

import supertool


# ---------------------------------------------------------------------------
# pre_tool_hook — enforcement logic
# ---------------------------------------------------------------------------

def test_pre_tool_hook_permissive_allows_everything() -> None:
    # Even with blocked tools, permissive mode returns 0
    code, msg = supertool.pre_tool_hook({"tool_name": "Grep"}, enforced=False)
    assert code == 0
    assert msg == ""


def test_pre_tool_hook_enforced_blocks_grep() -> None:
    code, msg = supertool.pre_tool_hook({"tool_name": "Grep"}, enforced=True)
    assert code == 2
    assert "Use ./supertool instead of Grep" in msg


def test_pre_tool_hook_enforced_blocks_glob() -> None:
    code, msg = supertool.pre_tool_hook({"tool_name": "Glob"}, enforced=True)
    assert code == 2
    assert "Use ./supertool instead of Glob" in msg


def test_pre_tool_hook_enforced_blocks_ls() -> None:
    code, msg = supertool.pre_tool_hook({"tool_name": "LS"}, enforced=True)
    assert code == 2
    assert "LS" in msg


def test_pre_tool_hook_enforced_allows_read() -> None:
    # Read must stay allowed — Edit requires it
    code, msg = supertool.pre_tool_hook({"tool_name": "Read"}, enforced=True)
    assert code == 0
    assert msg == ""


def test_pre_tool_hook_bash_blocks_cat() -> None:
    payload = {"tool_name": "Bash", "tool_input": {"command": "cat /etc/hosts"}}
    code, msg = supertool.pre_tool_hook(payload, enforced=True)
    assert code == 2
    assert "Bash(cat ...)" in msg


def test_pre_tool_hook_bash_blocks_find() -> None:
    payload = {"tool_name": "Bash",
               "tool_input": {"command": "find . -name '*.py'"}}
    code, msg = supertool.pre_tool_hook(payload, enforced=True)
    assert code == 2
    assert "find" in msg


def test_pre_tool_hook_bash_blocks_grep() -> None:
    payload = {"tool_name": "Bash",
               "tool_input": {"command": "grep -rn needle src/"}}
    code, msg = supertool.pre_tool_hook(payload, enforced=True)
    assert code == 2


def test_pre_tool_hook_bash_blocks_sed() -> None:
    payload = {"tool_name": "Bash",
               "tool_input": {"command": "sed -n '1,10p' file.txt"}}
    code, msg = supertool.pre_tool_hook(payload, enforced=True)
    assert code == 2


def test_pre_tool_hook_bash_allows_git() -> None:
    payload = {"tool_name": "Bash",
               "tool_input": {"command": "git status"}}
    code, msg = supertool.pre_tool_hook(payload, enforced=True)
    assert code == 0


def test_pre_tool_hook_bash_allows_python() -> None:
    payload = {"tool_name": "Bash",
               "tool_input": {"command": "python3 script.py"}}
    code, msg = supertool.pre_tool_hook(payload, enforced=True)
    assert code == 0


def test_pre_tool_hook_bash_allows_supertool() -> None:
    # The whole point — supertool itself must not be blocked
    payload = {"tool_name": "Bash",
               "tool_input": {"command": "./supertool read:foo.py"}}
    code, msg = supertool.pre_tool_hook(payload, enforced=True)
    assert code == 0


def test_pre_tool_hook_bash_empty_command_allowed() -> None:
    payload = {"tool_name": "Bash", "tool_input": {"command": ""}}
    code, msg = supertool.pre_tool_hook(payload, enforced=True)
    assert code == 0


def test_pre_tool_hook_missing_tool_name_allowed() -> None:
    code, msg = supertool.pre_tool_hook({}, enforced=True)
    assert code == 0


def test_pre_tool_hook_bash_missing_command_allowed() -> None:
    payload = {"tool_name": "Bash", "tool_input": {}}
    code, msg = supertool.pre_tool_hook(payload, enforced=True)
    assert code == 0


# ---------------------------------------------------------------------------
# is_enforced + --pre-tool-hook CLI dispatch
# ---------------------------------------------------------------------------

def test_is_enforced_returns_false_when_state_file_absent(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(supertool, "ENFORCE_STATE_FILE", str(tmp_path / "nope"))
    assert supertool.is_enforced() is False


def test_is_enforced_returns_true_when_state_file_present(monkeypatch, tmp_path) -> None:
    state = tmp_path / "enforced"
    state.touch()
    monkeypatch.setattr(supertool, "ENFORCE_STATE_FILE", str(state))
    assert supertool.is_enforced() is True


def test_main_pre_tool_hook_permissive(monkeypatch, tmp_path, capsys) -> None:
    # State file absent → permissive → exit 0 even for blocked tool
    monkeypatch.setattr(supertool, "ENFORCE_STATE_FILE", str(tmp_path / "nope"))
    monkeypatch.setattr("sys.stdin",
                        type("S", (), {"read": staticmethod(
                            lambda: '{"tool_name":"Grep"}')})())
    ret = supertool.main(["--pre-tool-hook"])
    assert ret == 0


def test_main_pre_tool_hook_enforced_blocks(monkeypatch, tmp_path, capsys) -> None:
    state = tmp_path / "enforced"
    state.touch()
    monkeypatch.setattr(supertool, "ENFORCE_STATE_FILE", str(state))
    monkeypatch.setattr("sys.stdin",
                        type("S", (), {"read": staticmethod(
                            lambda: '{"tool_name":"Grep"}')})())
    ret = supertool.main(["--pre-tool-hook"])
    captured = capsys.readouterr()
    assert ret == 2
    assert "Use ./supertool" in captured.err


def test_main_pre_tool_hook_malformed_json_fail_open(monkeypatch, tmp_path) -> None:
    state = tmp_path / "enforced"
    state.touch()
    monkeypatch.setattr(supertool, "ENFORCE_STATE_FILE", str(state))
    monkeypatch.setattr("sys.stdin",
                        type("S", (), {"read": staticmethod(
                            lambda: "not json{{")})())
    # Malformed input → fail-open (exit 0, don't block legit workflows)
    ret = supertool.main(["--pre-tool-hook"])
    assert ret == 0
