"""#1394 — an op can say which shapes of a command it does *not* replace.

`_guard_score` had no negative term: `flag` and `value` only ever added
specificity. So an op could say *this argv is mine* and could not say *this
argv is mine except when it carries these flags* — and the schema's only
escape hatch, declaring no entry at all, is per-op while the opt-out
(`raw_command_guard: false`) is repo-global. One over-broad GitLab entry
therefore took the four shipped GitHub mappings down with it, which is why
`gl-api` shipped unmapped in #1384.

`gl-api` is the worked case and the reason the general form is "any flag",
not a list of the write flags. `glab api` is GET by default and POST under
`-X`/`--method`, `-F`/`--field`, `-f`/`--raw-field` or `--input`, and
supertool has no GitLab write route at any spelling. But gl-api forwards no
flags at all, so `--hostname`, `-H`, `-i`, `--output`, `--silent` and even
`glab api -h` are equally unanswerable — a denylist of the four write
spellings would have wedged the CLI's own help.

Three decisions this file pins, because each could reasonably have gone the
other way:

* **An exclusion is "this entry does not claim this argv", never "this argv
  is allowed."** It loses to nothing. A veto that crossed entries would let
  any op in any repository's `.supertool.json` un-block a command another op
  legitimately claims, and `.supertool.json` is repo-authored.
* **It keys on the flag, not on its value.** `glab api -X GET` is a read and
  is excluded anyway. That costs a *missed block* — the caller runs a raw
  read supertool could have answered — which is the direction this guard may
  be wrong in, because there is no per-command way past a wrong block.
* **A bare `--` ends the option list**, for the exclusion and for the `flag`
  matcher alike. A flag-shaped token after it is a positional.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

import supertool

_ROOT = Path(__file__).resolve().parent.parent
_GITLAB_OPS = json.loads(
    (_ROOT / "presets" / "gitlab.json").read_text(encoding="utf-8"))["ops"]


def _load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
          config: Dict[str, Any]) -> None:
    (tmp_path / ".supertool.json").write_text(
        json.dumps(config), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(supertool, "_CONFIG", None)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", False)
    monkeypatch.setattr(supertool, "_CONFIG_PATH", None)
    supertool._load_config()


@pytest.fixture
def shipped_gitlab(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The real gitlab preset as the effective registry, per #1384's fixture."""
    _load(tmp_path, monkeypatch, {"ops": _GITLAB_OPS})
    return tmp_path


def _probe_op(**entry: Any) -> Dict[str, Any]:
    """A one-op registry whose only content is the mapping under test."""
    return {"ops": {"probe-op": {
        "safety": "read-only",
        "cmd": "true",
        "description": "a probe",
        "syntax": "probe-op:X",
        "replaces": [entry],
    }}}


# --------------------------------------------------------------------------
# gl-api: the read is blocked, and every shape it cannot answer stays usable
# --------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "glab api projects/:id/members/all",
    "glab api projects/:fullpath/releases",
    "glab api issues",
])
def test_a_plain_glab_api_read_names_gl_api(shipped_gitlab, command):
    verdict = supertool.guard_command(command)
    assert verdict.state == "blocked", (command, verdict)
    assert [m.use for m in verdict.matches] == ["gl-api:PATH"], command


@pytest.mark.parametrize("command,why", [
    ("glab api -X POST projects/:id/issues", "-X is the explicit write"),
    ("glab api --method POST projects/:id/issues", "long spelling of -X"),
    ("glab api --method=POST projects/:id/issues", "attached value"),
    ("glab api -X DELETE projects/:id/issues/1", "a delete is a write too"),
    ("glab api -XPOST projects/:id/issues", "pflag clusters the value on"),
    ("glab api -F title=x projects/:id/issues", "-F makes glab POST"),
    ("glab api --field title=x projects/:id/issues", "long spelling of -F"),
    ("glab api -f query=x graphql", "graphql reads are POSTs, and gl-api "
                                    "is REST-path only"),
    ("glab api --raw-field query=x graphql", "long spelling of -f"),
    ("glab api --input body.json projects/:id/issues", "body from a file"),
    ("glab api --input - projects/:id/issues", "body from stdin"),
    # gl-api forwards no flags, so these are unanswerable without being
    # writes. A denylist of the write spellings would have wedged them.
    ("glab api -h", "glab own help must stay reachable"),
    ("glab api --help", "same, long spelling"),
    ("glab api -H 'X-Trace: 1' projects/1", "gl-api sets its own headers"),
    ("glab api --hostname gitlab.example.com projects/1",
     "gl-api takes the host glab resolved, and cannot be pointed elsewhere"),
    ("glab api -i projects/1", "response headers are not in gl-api output"),
    ("glab api --output ndjson issues", "gl-api emits its own JSON shape"),
    ("glab api --silent projects/1", "no gl-api spelling suppresses a body"),
    ("glab api --paginate issues", "gl-api:PATH:full paginates, but the "
                                   "bare use string would name the wrong one"),
])
def test_a_glab_api_shape_gl_api_cannot_answer_stays_usable(
        shipped_gitlab, command, why):
    assert supertool.guard_command(command).state == "clean", (command, why)


def test_the_gl_api_refusal_carries_the_op_own_words(shipped_gitlab):
    verdict = supertool.guard_command("glab api projects/1")
    text = supertool.guard_refusal(verdict)
    assert "gl-api" in text
    assert "GET any GitLab REST path" in text


# --------------------------------------------------------------------------
# The semantics: an exclusion un-claims one entry, it never allows a command
# --------------------------------------------------------------------------

def test_an_exclusion_loses_to_a_second_entry_that_still_matches(
        tmp_path, monkeypatch):
    """The decision the issue left open, and the one with a security edge.

    If an exclusion meant "allowed" rather than "this entry does not match",
    any op declared in a repository's own `.supertool.json` could un-block a
    command another op legitimately claims — a repo-authored file quietly
    disarming a shipped mapping, one command at a time.
    """
    _load(tmp_path, monkeypatch, {"ops": {
        "narrow-op": {
            "safety": "read-only", "cmd": "true", "syntax": "narrow-op",
            "description": "narrow",
            "replaces": [{"argv": "gh pr view", "unless_flag": ["--json"],
                          "use": "narrow-op:N"}],
        },
        "broad-op": {
            "safety": "read-only", "cmd": "true", "syntax": "broad-op",
            "description": "broad",
            "replaces": [{"argv": "gh pr view", "use": "broad-op:N"}],
        },
    }})
    plain = supertool.guard_command("gh pr view 12")
    assert plain.state == "blocked"
    assert sorted(m.use for m in plain.matches) == ["broad-op:N",
                                                    "narrow-op:N"]

    excluded = supertool.guard_command("gh pr view 12 --json state")
    assert excluded.state == "blocked", excluded
    assert sorted(m.use for m in excluded.matches) == ["broad-op:N"]


def test_a_named_flag_excludes_only_itself(tmp_path, monkeypatch):
    _load(tmp_path, monkeypatch, _probe_op(
        argv="probe push", unless_flag=["--tags", "--follow-tags"],
        use="probe-op:X"))
    assert supertool.guard_command("probe push origin master").state == "blocked"
    assert supertool.guard_command("probe push --tags").state == "clean"
    assert supertool.guard_command("probe push --follow-tags").state == "clean"
    # A flag not on the list is not an exclusion.
    assert supertool.guard_command(
        "probe push --force-with-lease").state == "blocked"


def test_a_string_unless_flag_is_read_as_a_one_item_list(
        tmp_path, monkeypatch):
    """JSON authors write a scalar for a single value; refusing it would only
    turn a natural spelling into a silently over-broad block."""
    _load(tmp_path, monkeypatch, _probe_op(
        argv="probe api", unless_flag="-X", use="probe-op:X"))
    assert supertool.guard_command("probe api path").state == "blocked"
    assert supertool.guard_command("probe api -X POST path").state == "clean"


@pytest.mark.parametrize("bad", [
    {"why": "a dict"}, [1, 2], ["-X", 7], "", ["-X", ""], 3,
])
def test_a_malformed_unless_flag_drops_the_entry_and_says_so(
        tmp_path, monkeypatch, bad):
    """Not "no exclusion", which is the direction that hurts.

    Reading an unreadable exclusion as an absent one turns one typo into the
    over-broad block this key exists to prevent — and the block has no
    per-command way past. The entry is dropped instead, and the note makes
    the verdict `undecided` rather than a clean bill.
    """
    _load(tmp_path, monkeypatch, _probe_op(
        argv="probe api", unless_flag=bad, use="probe-op:X"))
    verdict = supertool.guard_command("probe api path")
    assert verdict.state == "undecided", verdict
    assert any("unless_flag" in note for note in verdict.notes), verdict


def test_an_empty_list_is_an_exclusion_of_nothing(tmp_path, monkeypatch):
    """`[]` is a legal spelling, not a malformed one, and means what it says.

    Asserting only that `probe api -X POST path` stays blocked would be
    vacuous: that also holds when `unless_flag` is never read at all. The
    second op is what makes the test a statement about the empty list — it
    declares the same argv with a real exclusion, so a run where the key is
    ignored returns both entries and this returns one.
    """
    _load(tmp_path, monkeypatch, {"ops": {
        "empty-op": {
            "safety": "read-only", "cmd": "true", "syntax": "empty-op",
            "description": "excludes nothing",
            "replaces": [{"argv": "probe api", "unless_flag": [],
                          "use": "empty-op:X"}],
        },
        "named-op": {
            "safety": "read-only", "cmd": "true", "syntax": "named-op",
            "description": "excludes -X",
            "replaces": [{"argv": "probe api", "unless_flag": ["-X"],
                          "use": "named-op:X"}],
        },
    }})
    plain = supertool.guard_command("probe api path")
    assert plain.state == "blocked", plain
    assert sorted(m.use for m in plain.matches) == ["empty-op:X",
                                                    "named-op:X"]

    written = supertool.guard_command("probe api -X POST path")
    assert written.state == "blocked", written
    assert sorted(m.use for m in written.matches) == ["empty-op:X"]


# --------------------------------------------------------------------------
# What counts as a flag
# --------------------------------------------------------------------------

@pytest.mark.parametrize("command,state", [
    # A lone `-` is stdin, a positional in every CLI that takes one.
    ("probe api -", "blocked"),
    # A bare `--` ends the option list; it is not itself a flag.
    ("probe api --", "blocked"),
    # ...and everything after it is a positional, however it is spelled.
    ("probe api -- -X", "blocked"),
    ("probe api -- --method", "blocked"),
    # Before it, they are flags.
    ("probe api -X POST -- path", "clean"),
    ("probe api -q", "clean"),
    ("probe api --anything", "clean"),
])
def test_any_flag_before_a_double_dash_un_claims_the_entry(
        tmp_path, monkeypatch, command, state):
    _load(tmp_path, monkeypatch, _probe_op(
        argv="probe api", unless_flag=["*"], use="probe-op:X"))
    assert supertool.guard_command(command).state == state, command


def test_the_flag_matcher_also_stops_at_a_double_dash(tmp_path, monkeypatch):
    """The same POSIX rule, applied to the positive term.

    `gh pr diff 1 -- --json` names a path called `--json`. Reading it as the
    flag that selects which op to name is the class of error the whole
    matcher exists to remove — a flag inside an argument is not a flag.
    """
    _load(tmp_path, monkeypatch, {"ops": {"probe-op": {
        "safety": "read-only", "cmd": "true", "syntax": "probe-op",
        "description": "a probe",
        "replaces": [
            {"argv": "probe view", "flag": "--json", "value": "state",
             "use": "probe-op:status"},
        ],
    }}})
    assert supertool.guard_command(
        "probe view 1 --json state").state == "blocked"
    assert supertool.guard_command(
        "probe view 1 -- --json state").state == "clean"


# --------------------------------------------------------------------------
# Nothing already shipped changed
# --------------------------------------------------------------------------

@pytest.mark.parametrize("command,use", [
    ("glab mr view 5", "gl-mr:NUMBER_OR_BRANCH"),
    ("glab mr view 5 --comments", "gl-mr:NUMBER_OR_BRANCH:full"),
    ("glab ci get --pipeline-id 12345", "gl-pipeline:NUMBER"),
    ("glab ci trace 224356863", "gl-job:NUMBER"),
])
def test_the_entries_without_an_exclusion_are_untouched(
        shipped_gitlab, command, use):
    verdict = supertool.guard_command(command)
    assert verdict.state == "blocked", (command, verdict)
    assert [m.use for m in verdict.matches] == [use], command


# --------------------------------------------------------------------------
# End to end, through the hook the plugin installs, with a real preset
# --------------------------------------------------------------------------

def _run_hook(command: str, cwd: Path) -> Dict[str, Any]:
    payload = json.dumps({"tool_name": "Bash",
                          "tool_input": {"command": command}})
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = str(_ROOT)
    proc = subprocess.run(
        [sys.executable, str(_ROOT / "hooks" / "pre_bash_guard.py")],
        input=payload, capture_output=True, text=True, encoding="utf-8",
        errors="replace", cwd=str(cwd), env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def _decision(out: Dict[str, Any]):
    return out.get("hookSpecificOutput", {}).get("permissionDecision")


def test_the_hook_denies_the_read_and_allows_the_write(tmp_path):
    """Through `presets`, not through injected `ops`.

    Every assertion above injects op bodies, which would pass identically if
    presets were never read — and `"presets": ["gitlab"]` is the only route a
    plugin user ever gets this mapping by.
    """
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"presets": ["gitlab"]}), encoding="utf-8")

    denied = _run_hook("glab api projects/:id/members/all", tmp_path)
    assert _decision(denied) == "deny", denied
    assert "gl-api" in denied["hookSpecificOutput"]["permissionDecisionReason"]

    for command in ("glab api -X POST projects/:id/issues",
                    "glab api -F title=x projects/:id/issues",
                    "glab api --input - projects/:id/issues",
                    "glab api --help"):
        assert _decision(_run_hook(command, tmp_path)) != "deny", command
