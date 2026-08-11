"""#1347 — the raw-command block belongs to the registry, and it parses argv.

Three hand-written regexes under `.claude/jit-context/tools/00-manual/` were
failing in three different directions on 2026-08-11, all of them a regex
reading a shell command as a string:

* `gh-pr-view-merge-have-ops.md` blocked and did not block the *same* command
  minutes apart, and reached no subagent Bash tool at all (20 raw
  `gh pr view --json body` calls in one triager run).
* `gh-list-limit.md` refuses commands that carry the very flag it requires,
  whenever a quoted argument precedes it (#1336). It fired on this author
  twice while this file was being written, once on a python heredoc that
  merely contained the string in a fixture.
* `supertool-no-cut.md` fires on the substring `supertool`, which in this repo
  is the **directory name** (#1221) — eight false positives on 2026-08-11,
  none of which piped a supertool op.

The fix is not a better regex. The command is tokenised into argv the way a
shell would, and the match is on the command word, its subcommands and its
flags. A directory name is then not a command word and a heredoc body is not
an argv, structurally rather than by another pattern.

The mapping itself is a property of the op — `replaces` in the op registry
entry — so a new op cannot ship its enforcement in a second file that goes
stale, and the refusal text is the op own description.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Dict

import pytest

import supertool

_ROOT = Path(__file__).resolve().parent.parent
_PIPE = chr(124)
_Q3 = chr(39) * 3


@pytest.fixture
def guard_config(monkeypatch: pytest.MonkeyPatch):
    """A config carrying `replaces` entries, loaded through the real loader."""

    def _load(tmp_path: Path, config: Dict[str, Any]) -> Dict[str, Any]:
        (tmp_path / ".supertool.json").write_text(
            json.dumps(config), encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(supertool, "_CONFIG", None)
        monkeypatch.setattr(supertool, "_CONFIG_CHECKED", False)
        monkeypatch.setattr(supertool, "_CONFIG_PATH", None)
        return supertool._load_config()

    return _load


_TWO_OPS = {
    "ops": {
        "gh-pr": {
            "safety": "read-only",
            "cmd": "true",
            "syntax": "gh-pr:NUMBER[:status|:diff]",
            "description": "Review a pull request: checks, reviews, diff stat.",
            "replaces": [
                {"argv": "gh pr view", "use": "gh-pr:NUMBER"},
                {"argv": "gh pr view", "flag": "--json", "value": "state",
                 "use": "gh-pr:NUMBER:status"},
                {"argv": "gh pr view", "flag": "--json", "value": "files",
                 "use": "gh-pr:NUMBER:diff"},
            ],
        },
        "gh-issues": {
            "safety": "read-only",
            "cmd": "true",
            "syntax": "gh-issues[:FILTERS]",
            "description": "Issue triage board.",
            "replaces": [{"argv": "gh issue list", "use": "gh-issues"}],
        },
    }
}


# --------------------------------------------------------------------------
# Tokenising, which is the decision the issue says it stands or falls on
# --------------------------------------------------------------------------

def test_the_directory_name_is_not_a_command_word(tmp_path, guard_config):
    """#1221 whole class: `claude-supertool` in a path is not an invocation."""
    guard_config(tmp_path, _TWO_OPS)
    cmd = ("cd ~/Documents/claude-supertool && pytest tests/test_x.py -q "
           + _PIPE + " tail -3")
    verdict = supertool.guard_command(cmd)
    assert verdict.state == "clean", verdict


def test_a_quoted_argument_before_a_flag_does_not_hide_the_flag(
        tmp_path, guard_config):
    """#1336: the flag is a token, so a quoted value cannot swallow it."""
    guard_config(tmp_path, _TWO_OPS)
    limit = "--" + "limit"
    cmd = 'gh issue list --search "state:open label:a" ' + limit + ' 20'
    verdict = supertool.guard_command(cmd)
    assert verdict.state == "blocked", verdict
    assert [m.op for m in verdict.matches] == ["gh-issues"]


def test_a_heredoc_body_is_not_an_argv(tmp_path, guard_config):
    """The message text of a `git-commit:@-` payload is content, not commands."""
    guard_config(tmp_path, _TWO_OPS)
    cmd = chr(10).join([
        "supertool " + chr(39) + "git-commit:@-" + chr(39) + " <<" + chr(39)
        + "EOF" + chr(39),
        "message = " + _Q3 + "docs: gh pr view " + _PIPE + " head is out",
        "",
        "gh pr view is replaced by an op now.",
        _Q3,
        "EOF",
    ])
    verdict = supertool.guard_command(cmd)
    assert verdict.state == "clean", verdict


def test_every_heredoc_on_a_line_has_its_body_stripped(tmp_path, guard_config):
    """`cmd <<A <<B` is legal bash, and only the first opener used to be read.

    The second body was tokenised as ordinary shell text, so content became an
    invocation — the exact class this whole feature exists to remove, inside
    the routine written to prevent it.
    """
    guard_config(tmp_path, _TWO_OPS)
    cmd = chr(10).join([
        "cat <<A <<B", "ignored", "A",
        "doc note; gh pr view 12", "B", ""])
    verdict = supertool.guard_command(cmd)
    assert verdict.state == "clean", verdict


def test_a_backtick_substitution_is_a_command(tmp_path, guard_config):
    """`$(...)` was matched only by accident and backticks not at all.

    Worse, glued to an assignment the whole head token was eaten:
    `x=`gh pr view 12`` stripped as an env assignment and took `gh` with it.
    """
    guard_config(tmp_path, _TWO_OPS)
    tick = chr(96)
    for cmd in (tick + "gh pr view 12" + tick,
                "x=" + tick + "gh pr view 12" + tick + "; echo $x",
                "echo $(gh pr view 12)"):
        assert supertool.guard_command(cmd).state == "blocked", cmd


def test_a_backtick_inside_quotes_is_still_text(tmp_path, guard_config):
    guard_config(tmp_path, _TWO_OPS)
    tick = chr(96)
    cmd = "echo 'run " + tick + "gh pr view 12" + tick + " by hand'"
    assert supertool.guard_command(cmd).state == "clean", cmd


def test_a_substitution_the_lexer_cannot_open_is_undecided(
        tmp_path, guard_config):
    """Quoting hides a substitution from the tokeniser. That is not `clean`."""
    guard_config(tmp_path, _TWO_OPS)
    for cmd in ('echo "$(some-command)"',
                'echo "' + chr(96) + 'some-command' + chr(96) + '"'):
        verdict = supertool.guard_command(cmd)
        assert verdict.state == "undecided", (cmd, verdict)


def test_an_interpreter_handed_a_string_is_undecided(tmp_path, guard_config):
    """`eval` and `sh -c` run a command this matcher never sees."""
    guard_config(tmp_path, _TWO_OPS)
    for cmd in ('eval "$SOMETHING"',
                'bash -c "gh pr view 12"',
                "sh -c 'ls'"):
        verdict = supertool.guard_command(cmd)
        assert verdict.state in ("blocked", "undecided"), (cmd, verdict)
        if verdict.state == "undecided":
            assert verdict.notes


def test_a_command_after_an_operator_is_still_matched(tmp_path, guard_config):
    """A block that only reads the first word has one hop of reach."""
    guard_config(tmp_path, _TWO_OPS)
    for cmd in ("cd /tmp && gh pr view 12",
                "for i in 1 2; do gh pr view $i; done",
                "FOO=1 rtk gh pr view 12",
                "gh pr view 12 > /tmp/out"):
        verdict = supertool.guard_command(cmd)
        assert verdict.state == "blocked", (cmd, verdict)


def test_a_mention_is_not_an_invocation(tmp_path, guard_config):
    """A word inside another command argument never becomes a command."""
    guard_config(tmp_path, _TWO_OPS)
    verdict = supertool.guard_command('echo "run gh pr view 12 by hand"')
    assert verdict.state == "clean", verdict


# --------------------------------------------------------------------------
# Flags select WHICH op is named, not whether to block
# --------------------------------------------------------------------------

def test_json_state_and_json_files_name_different_ops(tmp_path, guard_config):
    guard_config(tmp_path, _TWO_OPS)
    state = supertool.guard_command("gh pr view 1321 --json state -q .state")
    files = supertool.guard_command("gh pr view 1321 --json files")
    plain = supertool.guard_command("gh pr view 1321")
    assert [m.use for m in state.matches] == ["gh-pr:NUMBER:status"]
    assert [m.use for m in files.matches] == ["gh-pr:NUMBER:diff"]
    assert [m.use for m in plain.matches] == ["gh-pr:NUMBER"]


def test_a_comma_list_flag_value_still_selects(tmp_path, guard_config):
    guard_config(tmp_path, _TWO_OPS)
    v = supertool.guard_command("gh pr view 1321 --json=number,state")
    assert [m.use for m in v.matches] == ["gh-pr:NUMBER:status"]


def test_no_replaces_entry_is_the_escape_hatch(tmp_path, guard_config):
    """Tagging, releasing and deleting a ref have no op and stay usable."""
    guard_config(tmp_path, _TWO_OPS)
    for cmd in ("gh release create v0.34.0",
                "gh api -X DELETE repos/o/n/git/refs/heads/x",
                "gh run rerun 42"):
        assert supertool.guard_command(cmd).state == "clean", cmd


# --------------------------------------------------------------------------
# The third state: a guard that could not answer must not answer "clean"
# --------------------------------------------------------------------------

def test_a_registry_it_could_not_enumerate_is_undecided_not_clean(
        tmp_path, guard_config):
    guard_config(tmp_path, {"presets": ["nope-does-not-exist"], "ops": {}})
    verdict = supertool.guard_command("git status")
    assert verdict.state == "undecided", verdict
    assert verdict.notes, "an undecided verdict must say why"


def test_a_match_survives_an_incomplete_registry(tmp_path, guard_config):
    """A positive match is authoritative even when the population is short."""
    cfg = {"presets": ["nope-does-not-exist"]}
    cfg.update(_TWO_OPS)
    guard_config(tmp_path, cfg)
    verdict = supertool.guard_command("gh pr view 12")
    assert verdict.state == "blocked", verdict


def test_a_command_it_cannot_tokenise_is_undecided_not_clean(
        tmp_path, guard_config):
    guard_config(tmp_path, _TWO_OPS)
    verdict = supertool.guard_command('gh pr view "unterminated')
    assert verdict.state == "undecided", verdict
    assert verdict.notes


# --------------------------------------------------------------------------
# The escape hatch is a file in the repo, never an env var
# --------------------------------------------------------------------------

def test_the_guard_is_on_by_default_and_off_only_from_config(
        tmp_path, guard_config):
    guard_config(tmp_path, _TWO_OPS)
    assert supertool.guard_command("gh pr view 12").state == "blocked"

    off = dict(_TWO_OPS)
    off["raw_command_guard"] = False
    guard_config(tmp_path, off)
    verdict = supertool.guard_command("gh pr view 12")
    assert verdict.state == "off", verdict


def test_no_environment_variable_turns_the_guard_off(
        tmp_path, guard_config, monkeypatch):
    """An env var everyone learns is not a block. The hatch is reviewable."""
    guard_config(tmp_path, _TWO_OPS)
    for name in ("SUPERTOOL_RAW_COMMAND_GUARD", "SUPERTOOL_NO_GUARD",
                 "ST_RAW_COMMAND_GUARD"):
        monkeypatch.setenv(name, "0")
    assert supertool.guard_command("gh pr view 12").state == "blocked"


# --------------------------------------------------------------------------
# What the refusal says
# --------------------------------------------------------------------------

def test_the_refusal_carries_the_op_own_description(tmp_path, guard_config):
    guard_config(tmp_path, _TWO_OPS)
    text = supertool.guard_refusal(supertool.guard_command("gh pr view 12"))
    assert "gh-pr:NUMBER" in text
    assert "Review a pull request" in text
    assert "help:gh-pr" in text


def test_op_guard_renders_all_three_states(tmp_path, guard_config):
    guard_config(tmp_path, _TWO_OPS)
    assert "BLOCKED" in supertool.op_guard("gh pr view 12")
    assert "OK" in supertool.op_guard("git status")
    assert "UNDECIDED" in supertool.op_guard('gh pr view "x')


# --------------------------------------------------------------------------
# The shipped mappings, and the shipped hook
# --------------------------------------------------------------------------

def _shipped_replaces() -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for path in sorted((_ROOT / "presets").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for name, definition in (data.get("ops") or {}).items():
            if isinstance(definition, dict) and definition.get("replaces"):
                out[name] = definition
    return out


def test_the_first_cut_covers_the_measured_commands():
    shipped = _shipped_replaces()
    assert "gh-pr" in shipped
    assert "gh-issues" in shipped
    argvs = {e["argv"] for d in shipped.values() for e in d["replaces"]}
    for wanted in ("gh pr view", "gh pr merge", "gh pr create",
                   "gh issue list"):
        assert wanted in argvs, (wanted, sorted(argvs))


def test_every_shipped_entry_uses_only_known_keys():
    shipped = _shipped_replaces()
    # Without this the whole test is vacuous: at the base commit `replaces`
    # exists nowhere, `_shipped_replaces()` is empty, both loops never run and
    # the schema check passes against nothing. Proved red-first — it was the
    # one test of these that passed with no product code at all.
    assert shipped, "no shipped op declares `replaces`, so nothing was checked"
    allowed = {"argv", "flag", "value", "use"}
    for name, definition in shipped.items():
        for entry in definition["replaces"]:
            assert set(entry) <= allowed, (name, entry)
            assert entry["argv"].strip() == entry["argv"]
            assert "use" in entry, (name, entry)
            if "value" in entry:
                assert "flag" in entry, (name, entry)


def test_every_use_string_names_an_op_that_exists():
    """The refusal's *description* cannot go stale. Its `use` line can.

    #1347's second promise is that the refusal text is the op's own
    documentation, so it cannot describe a flag the op no longer has. That
    holds for `description`, which is read off the registry entry at match
    time — and it does **not** hold for `use`, which is a hand-written string
    sitting beside it. Rename or drop an op and every refusal that points at
    it keeps confidently naming a command that does not exist, which is
    exactly the failure #1221's hand-written rule had.

    Nothing else checks this: the schema test above accepts any `use` at all
    as long as the key is present.
    """
    known = set(supertool._OP_SAFETY_BUILTIN)
    for path in sorted((_ROOT / "presets").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        known.update(data.get("ops") or {})
    assert known, "no op names could be enumerated, so nothing was checked"

    checked = 0
    for name, definition in _shipped_replaces().items():
        for entry in definition["replaces"]:
            # `use` is optional in the schema and defaults to the op's own
            # syntax, so an entry without one can only name its own op.
            head = entry.get("use", name).split(":")[0]
            assert head in known, (
                "op " + name + " declares replaces -> " + entry.get("use", name)
                + ", but no op named " + head + " exists, so the refusal "
                "would name a command the reader cannot run")
            checked += 1
    assert checked, "no `use` strings were checked"


def test_replaces_is_not_handed_to_the_op_subprocess(tmp_path):
    """It is guard metadata, not op configuration.

    Unreserved op keys are exported as `SUPERTOOL_<KEY>` to the op's own
    subprocess. A mapping table nothing reads has no business in the
    environment of every gh-* call.
    """
    probe = ("import os, json; print(json.dumps(sorted("
             "k for k in os.environ if k.startswith('SUPERTOOL_'))))")
    (tmp_path / ".supertool.json").write_text(json.dumps({
        "ops": {
            "probe": {
                "safety": "read-only",
                "cmd": "{python} -c " + json.dumps(probe),
                "lines": 80,
                "replaces": [{"argv": "nothing at all", "use": "probe"}],
            }
        }
    }), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(_ROOT / "supertool.py"), "probe"],
        capture_output=True, text=True, cwd=str(tmp_path), timeout=60)
    assert "SUPERTOOL_LINES" in proc.stdout, proc.stdout
    assert "SUPERTOOL_REPLACES" not in proc.stdout, proc.stdout


def test_the_hook_is_registered_for_pretooluse_bash():
    hooks = json.loads((_ROOT / "hooks" / "hooks.json").read_text(
        encoding="utf-8"))["hooks"]
    pre = hooks["PreToolUse"]
    assert any(m.get("matcher") == "Bash" for m in pre), pre


def test_every_hook_command_points_at_a_script_that_exists():
    """A hooks.json path nobody resolves is a hook that errors on every call.

    Nothing checked this before, and this change is what makes it matter: the
    SessionStart hook fires once a session, so a broken path there is one
    visible error. `PreToolUse(Bash)` fires before **every** Bash command, so
    the same typo is an error on every command the user runs, for as long as
    the release is out. The path is only ever exercised at install time, which
    is the one place this repo's tests do not reach.
    """
    raw = (_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8")
    hooks = json.loads(raw)["hooks"]
    referenced = []
    for event, groups in hooks.items():
        for group in groups:
            for hook in group.get("hooks") or []:
                if hook.get("type") != "command":
                    continue
                for token in str(hook.get("command", "")).split():
                    token = token.strip(chr(34) + chr(39))
                    marker = "${CLAUDE_PLUGIN_ROOT}/"
                    if marker not in token:
                        continue
                    rel = token.split(marker, 1)[1].rstrip(chr(34) + chr(39))
                    referenced.append((event, rel))
    assert referenced, "no plugin-root hook commands found, so nothing was checked"
    for event, rel in referenced:
        # PurePosixPath: hooks.json spells these with forward slashes, and the
        # assertion is about the repo layout, not about the host separator.
        target = _ROOT.joinpath(*PurePosixPath(rel).parts)
        assert target.is_file(), (
            event + " hook references " + rel + ", which is not a file in the "
            "repo — every " + event + " would fire a hook that cannot run")


def test_the_wrapper_never_runs_the_bare_interpreter_name(tmp_path):
    """#572, in a hook that fires before EVERY Bash call.

    On Windows the bare name can resolve to the App Execution Alias stub,
    which blocks rather than erroring. One slow `git push` is what that cost
    #572; here it would be every command in the session, each waiting out the
    hook timeout.
    """
    wrapper = (_ROOT / "hooks" / "pre-bash-guard.sh").read_text(
        encoding="utf-8")
    for line in wrapper.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert not stripped.startswith("exec python3 "), line
        assert "command -v python3 " not in stripped, line
    assert "python3.9" in wrapper, "the versioned ladder is gone"


def _run_hook(command: str, cwd: Path) -> Dict[str, Any]:
    payload = json.dumps({"tool_name": "Bash",
                          "tool_input": {"command": command}})
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = str(_ROOT)
    proc = subprocess.run(
        [sys.executable, str(_ROOT / "hooks" / "pre_bash_guard.py")],
        input=payload, capture_output=True, text=True, cwd=str(cwd), env=env,
        timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def test_the_hook_denies_a_replaced_command(tmp_path):
    (tmp_path / ".supertool.json").write_text(json.dumps(_TWO_OPS),
                                              encoding="utf-8")
    out = _run_hook("gh pr view 12", tmp_path)
    hook = out["hookSpecificOutput"]
    assert hook["hookEventName"] == "PreToolUse"
    assert hook["permissionDecision"] == "deny"
    assert "gh-pr" in hook["permissionDecisionReason"]


def test_the_hook_stays_out_of_the_way_when_nothing_replaces_it(tmp_path):
    (tmp_path / ".supertool.json").write_text(json.dumps(_TWO_OPS),
                                              encoding="utf-8")
    out = _run_hook("ls -la", tmp_path)
    assert out.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"


def test_the_hook_discloses_that_it_could_not_decide(tmp_path):
    """Fail open, but never silently: the transcript records the gap."""
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"presets": ["nope-does-not-exist"], "ops": {}}),
        encoding="utf-8")
    out = _run_hook("git status", tmp_path)
    hook = out["hookSpecificOutput"]
    assert hook.get("permissionDecision") != "deny"
    assert "raw-command guard" in hook["additionalContext"]
