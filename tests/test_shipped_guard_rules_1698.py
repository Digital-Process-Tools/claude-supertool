r"""The guard rules `replaces` cannot express now ship with the plugin (#1698).

`.claude/jit-context/tools/00-manual/` holds five rules that the `replaces`
registry cannot reach. They are read by `claude-jit-context`'s hooks out of
`$CLAUDE_PROJECT_DIR`, so they guard sessions run **inside this checkout** and
nothing else. Measured in `Digital-Process-Tools/claude-oss` — a repo whose
whole workflow is ops — `./supertool 'git-push' 2>&1 | tail -6` ran unblocked
and the cut removed the two lines naming the repository that was written to.

The distribution channel that actually reaches every user is the plugin's own
`hooks/hooks.json`, which registers `pre-bash-guard.sh` on `Bash|PowerShell`
regardless of what the target repo contains. `hooks/shipped_rules.py` reads the
rule files out of `$CLAUDE_PLUGIN_ROOT` — the same markdown, not a copy — and
applies the ones that are true of *supertool* rather than of *this repository*.

**Not all five travel, and the four that do not are a recorded absence rather
than an omission.** A rule that encodes this repo's merge strategy, or that
names an op the caller's presets may not load, or that fires on a tool the
shipped matcher does not cover, would arrive in someone else's repository as a
wrong block whose only escape is repo-global.

Would these pass if the code did nothing? No: at fafa019 there is no
`hooks/shipped_rules.py` at all, and the end-to-end row below records the
current answer for the piped command as an envelope carrying no decision.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from _guard_wire import envelope

_ROOT = Path(__file__).resolve().parents[1]
_HOOKS = _ROOT / "hooks"
_MANUAL = _ROOT / ".claude" / "jit-context" / "tools" / "00-manual"

sys.path.insert(0, str(_HOOKS))

import shipped_rules  # noqa: E402

#: The command the measured incident cut, in the spelling a worktree uses.
_PIPED = "python3 supertool.py 'gh-pr:1424:status' | tail -3"

#: The same op with nothing cutting it. A guard that refused this would be a
#: guard that refuses everything, and the deny row above would still pass.
_UNPIPED = "python3 supertool.py 'gh-pr:1424:status'"

#: No supertool call at all. `head` is not the subject; the op's output is.
_UNRELATED = "git status | head -3"


def _project(tmp_path: Path, *own_rules: str) -> Path:
    """A repo with no jit layer, or one owning the named rule files."""
    project = tmp_path / "repo"
    project.mkdir(exist_ok=True)
    if own_rules:
        manual = project / ".claude" / "jit-context" / "tools" / "00-manual"
        manual.mkdir(parents=True, exist_ok=True)
        for name in own_rules:
            (manual / name).write_text("---\ntitle: mine\n---\n",
                                       encoding="utf-8")
    return project


# --- which rules travel, and the ones that do not are stated ---------------

def test_every_manual_rule_is_shipped_or_a_recorded_absence() -> None:
    """A sixth rule must choose; it may not arrive as an absence.

    This is `tests/test_replaces_census_1384.py`'s partition pointed at the
    other layer: a silent four-of-five and a considered four-of-five render
    identically, which is this repository's own defect class.
    """
    on_disk = {path.name for path in _MANUAL.glob("*.md")}
    classified = set(shipped_rules.SHIPPED) | set(shipped_rules.NOT_SHIPPED)
    assert on_disk == classified, (
        "unclassified: " + repr(sorted(on_disk - classified))
        + " / classified but absent: " + repr(sorted(classified - on_disk)))
    assert not (set(shipped_rules.SHIPPED) & set(shipped_rules.NOT_SHIPPED))


@pytest.mark.parametrize("name", sorted(getattr(shipped_rules,
                                                "NOT_SHIPPED", {})))
def test_a_rule_that_does_not_travel_carries_its_reason(name: str) -> None:
    reason = shipped_rules.NOT_SHIPPED[name]
    assert isinstance(reason, str) and len(reason) > 40, (name, reason)


def test_the_no_cut_rule_is_the_one_that_travels() -> None:
    """The rule the measured incident names, pinned by name.

    `supertool-no-cut` is a claim about supertool's own output format, so it
    is true wherever supertool is installed and needs nothing from the target
    repository's config.
    """
    assert shipped_rules.SHIPPED.get("supertool-no-cut.md") == "deny"


# --- the matcher: a refusal, and an allow in the same fixture --------------

def test_a_repo_with_no_layer_gets_the_refusal(tmp_path: Path) -> None:
    project = _project(tmp_path)
    answer = shipped_rules.match(_PIPED, str(_ROOT), str(project))
    assert answer is not None, _PIPED
    verb, body = answer
    assert verb == "deny"
    assert "Narrow the op" in body, body[:200]


@pytest.mark.parametrize("command", [_UNPIPED, _UNRELATED])
def test_the_same_fixture_still_allows_what_the_rule_is_not_about(
        tmp_path: Path, command: str) -> None:
    """The positive control. Without it the deny row passes on a rule that
    refuses every command it is shown."""
    project = _project(tmp_path)
    assert shipped_rules.match(command, str(_ROOT), str(project)) is None, (
        command)


def test_a_repo_owning_the_rule_keeps_it(tmp_path: Path) -> None:
    """Layers are the ownership boundary, so the plugin defers to a copy.

    Without this the rule is refused twice with two different messages in
    every repo that already carries it — #1376's option 3, which this
    repository killed rather than accepted for one release.
    """
    project = _project(tmp_path, "supertool-no-cut.md")
    assert shipped_rules.match(_PIPED, str(_ROOT), str(project)) is None


def test_owning_some_other_rule_defers_nothing(tmp_path: Path) -> None:
    """The positive control for the deferral: it is per rule, not per repo."""
    project = _project(tmp_path, "merged-is-not-ancestry.md")
    answer = shipped_rules.match(_PIPED, str(_ROOT), str(project))
    assert answer is not None and answer[0] == "deny"


def test_this_checkout_owns_every_rule_it_ships(tmp_path: Path) -> None:
    """Dogfooding without duplication: here the local layer wins, silently."""
    assert shipped_rules.match(_PIPED, str(_ROOT), str(_ROOT)) is None


# --- a pattern the translator cannot honour is skipped, never dropped ------

def test_a_posix_class_the_translator_knows_becomes_a_python_class() -> None:
    assert shipped_rules.translate("a[[:space:]]b") == r"a[ \t\n\r\f\v]b"
    assert shipped_rules.translate("[^&[:space:]]") == r"[^& \t\n\r\f\v]"


def test_a_class_it_does_not_know_is_declined_rather_than_guessed() -> None:
    """Three states. Dropping the rule silently is the defect, not the fix."""
    assert shipped_rules.translate("[[:alpha:]]") is None


def test_an_untranslatable_rule_is_reported_as_skipped(
        tmp_path: Path) -> None:
    """A rule that could not be compiled must be named, not absent."""
    manual = tmp_path / ".claude" / "jit-context" / "tools" / "00-manual"
    manual.mkdir(parents=True)
    (manual / "00-index.tsv").write_text(
        "Bash\t~[[:alpha:]]+\tsupertool-no-cut.md\tblock\t\t\n",
        encoding="utf-8")
    (manual / "supertool-no-cut.md").write_text("---\nx: 1\n---\nbody\n",
                                                encoding="utf-8")
    rules, skipped = shipped_rules.load(str(tmp_path))
    assert rules == []
    assert any("supertool-no-cut.md" in note for note in skipped), skipped


def test_a_missing_rule_directory_is_skipped_not_clean(tmp_path: Path) -> None:
    rules, skipped = shipped_rules.load(str(tmp_path))
    assert rules == []
    assert skipped, "an install with no rule files must say so"


def test_an_index_that_is_not_utf8_declines_rather_than_raising(
        tmp_path: Path) -> None:
    """`open(encoding="utf-8")` raises `UnicodeDecodeError`, not `OSError`.

    A narrow `except OSError` never fires on it, the exception leaves `load`,
    and the caller's own `except Exception` turns a broken index into
    `nothing matched` — the guard's whole defect class, inside the guard.
    """
    manual = tmp_path / ".claude" / "jit-context" / "tools" / "00-manual"
    manual.mkdir(parents=True)
    (manual / "00-index.tsv").write_bytes(
        b"Bash" + b"\t" + b"~a" + b"\t" + b"x.md" + b"\t" + b"block\xff\n")
    rules, skipped = shipped_rules.load(str(tmp_path))
    assert rules == []
    assert skipped, "an index that could not be decoded must say so"


def test_a_broken_shipped_index_is_a_note_not_silence(tmp_path: Path) -> None:
    """The finding an auditor raised on the first cut of this change.

    `match` dropped `load`'s skip notes, so a rule that is indexed, named in
    `SHIPPED` and impossible to honour returned the same `None` as a command
    nothing claimed. That is an absence produced by the tool read as an
    absence in the world, shipped inside the thing that exists to stop it.
    """
    manual = tmp_path / ".claude" / "jit-context" / "tools" / "00-manual"
    manual.mkdir(parents=True)
    (manual / "00-index.tsv").write_text(
        "Bash\t~[[:alpha:]]+\tsupertool-no-cut.md\tblock\t\t\n",
        encoding="utf-8")
    (manual / "supertool-no-cut.md").write_text("---\nx: 1\n---\nbody\n",
                                                encoding="utf-8")
    answer = shipped_rules.match(_PIPED, str(tmp_path),
                                 str(_project(tmp_path)))
    assert answer is not None, "a rule that could not be honoured said nothing"
    verb, text = answer
    assert verb == "note", answer
    assert "supertool-no-cut.md" in text, text


def test_the_broken_index_note_rides_on_every_command_deliberately(
        tmp_path: Path) -> None:
    """A reviewer called this over-firing. It is, and it is the right cost.

    `echo hello` shares nothing with `supertool-no-cut`'s subject and still
    gets the disclosure, because the pattern that would have decided is the
    thing that failed to compile — there is nothing left to screen with. The
    module's own `SHIPPED` comment refuses a note under every call, and this
    is not that: `op-defaults-that-narrow` would note on every call in a
    *healthy* install, forever, by design; this fires only in a **defective**
    one and stops the moment the index is repaired. That is the difference
    between a permanent tax and a defect alarm, and it is the trade
    `hooks/pre-bash-guard.sh`'s own `decline` already makes — it fires on
    `echo hello` too when no interpreter answers.

    The condition cannot arrive from this repository:
    `test_the_shipped_tree_honours_every_rule_it_claims_to_ship` below is the
    write-time gate, the same place #1254 put a dead jit escape.
    """
    manual = tmp_path / ".claude" / "jit-context" / "tools" / "00-manual"
    manual.mkdir(parents=True)
    (manual / "00-index.tsv").write_text(
        "Bash\t~[[:alpha:]]+\tsupertool-no-cut.md\tblock\t\t\n",
        encoding="utf-8")
    (manual / "supertool-no-cut.md").write_text("---\nx: 1\n---\nbody\n",
                                                encoding="utf-8")
    answer = shipped_rules.match("echo hello unrelated command",
                                 str(tmp_path), str(_project(tmp_path)))
    assert answer is not None and answer[0] == "note", answer


def test_the_shipped_tree_honours_every_rule_it_claims_to_ship() -> None:
    """The gate that keeps the note above unreachable from a release.

    A `SHIPPED` name with no index row, a row whose mode disagrees with the
    verb, a pattern that does not translate, a body that is not on disk — each
    is a rule that looks shipped and is not, and each would reach every plugin
    user as a note on every Bash call. Caught here instead, at the same point
    #1254 catches a jit escape awk cannot compile.
    """
    rules, skipped = shipped_rules.load(str(_ROOT))
    assert skipped == [], skipped
    assert sorted(rule.name for rule in rules) == sorted(
        shipped_rules.SHIPPED), rules


def test_an_install_with_no_rule_layer_at_all_stays_quiet(
        tmp_path: Path) -> None:
    """The control for the row above, and the reason it is not `skipped`.

    A plugin root that carries no rule directory is an install without the
    layer, not a layer that is broken. A note there would be attached to every
    Bash call anyone ever makes, forever, which is the silence-with-a-token-
    cost #1413 declined to add — and `hooks/guard-selftest.py` is the surface
    that reports it, the same division #1378 settled for the hook as a whole.
    """
    assert shipped_rules.match(_PIPED, str(tmp_path),
                               str(_project(tmp_path))) is None


# --- the inventory: a repo without the rules gets a statement --------------

def test_the_inventory_names_every_rule_and_every_absence(
        tmp_path: Path) -> None:
    """`.claude/settings.json`'s surviving registration announces itself when
    its script is absent. The rule layer keeps that property: what a repo does
    not get is enumerated, not silent."""
    project = _project(tmp_path)
    lines = shipped_rules.inventory(str(_ROOT), str(project))
    text = chr(10).join(lines)
    for name in shipped_rules.SHIPPED:
        assert name in text, name
    for name, reason in shipped_rules.NOT_SHIPPED.items():
        assert name in text, name
        assert reason[:30] in text, name


def test_the_selftest_report_carries_the_inventory() -> None:
    """The one surface a user runs to ask whether they are guarded."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "guard_selftest", str(_HOOKS / "guard-selftest.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    lines, _code = module.report(str(_ROOT))
    text = chr(10).join(lines)
    assert "supertool-no-cut.md" in text, text


def test_the_inventory_is_ascii_so_a_cp1252_console_can_print_it(
        tmp_path: Path) -> None:
    """`guard-selftest.py` writes these lines through a text-mode stdout.

    Reasoned, not observed — nobody here has a Windows box (the #627
    convention). Anything written to stdout is encoded with the *console's*
    code page, not the source file's, and on Windows that is typically cp1252,
    where an em dash raises `UnicodeEncodeError` and kills the process at the
    `print` — after the check it was reporting had already run. The rule
    *bodies* are exempt and are not asserted here: they reach the caller as
    bytes through `_say`, which #1625 made byte-mode for this exact reason.
    """
    lines = shipped_rules.inventory(str(_ROOT), str(_project(tmp_path)))
    for line in lines:
        assert line.isascii(), line


# --- end to end, through the file Claude Code actually runs ----------------

def _hook(command: str, project: Path, tmp_path: Path,
          tool_name: str = "Bash"):
    """The house idiom: inherit the environment and override two keys.

    A hand-built `env` would need a portable `PATH`, and there is no such
    literal — `/usr/bin:/bin` is a POSIX assertion that would take the
    `windows-latest` legs down with it.
    """
    payload = json.dumps({"tool_name": tool_name,
                          "tool_input": {"command": command}})
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = str(_ROOT)
    env["CLAUDE_PROJECT_DIR"] = str(project)
    proc = subprocess.run(
        [sys.executable, str(_HOOKS / "pre_bash_guard.py")],
        input=payload, capture_output=True, text=True, encoding="utf-8",
        errors="replace", cwd=str(tmp_path), env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return envelope(proc.stdout)["hookSpecificOutput"]


def test_the_hook_denies_the_piped_op_in_a_repo_with_no_layer(
        tmp_path: Path) -> None:
    project = _project(tmp_path)
    hook = _hook(_PIPED, project, tmp_path)
    assert hook.get("permissionDecision") == "deny", hook


def test_the_hook_says_nothing_about_the_unpiped_op(tmp_path: Path) -> None:
    project = _project(tmp_path)
    hook = _hook(_UNPIPED, project, tmp_path)
    assert "permissionDecision" not in hook, hook


@pytest.mark.parametrize("command,denies", [(_PIPED, True), (_UNPIPED, False)])
def test_the_powershell_route_gets_the_same_answer(
        tmp_path: Path, command: str, denies: bool) -> None:
    """Wherever the PowerShell tool is enabled Claude routes shell commands
    through it, and a hook that never fires there reads exactly like one that
    fired and approved (#1413). The registry declines that route because its
    tokeniser is POSIX; a shipped rule is a regex over the command text, so it
    reads this shell as well as it reads Bash and does not decline. The
    `False` row is the control: without it this passes on a layer that denies
    every PowerShell call.
    """
    hook = _hook(command, _project(tmp_path), tmp_path,
                 tool_name="PowerShell")
    assert (hook.get("permissionDecision") == "deny") is denies, hook


@pytest.mark.parametrize("enabled,denies", [(False, False), (True, True)])
def test_raw_command_guard_false_covers_the_shipped_layer_too(
        tmp_path: Path, enabled: bool, denies: bool) -> None:
    """One over-broad regex must stay fixable from inside the repository.

    `raw_command_guard: false` is the only escape from a wrong block, and it
    is repo-global already. A second gate that did not honour it would be a
    block with no hatch at all — worse than the one `unless_flag` was added
    for. The `True` row is the positive control: without it this passes on a
    layer that never fires.
    """
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"raw_command_guard": enabled}), encoding="utf-8")
    hook = _hook(_PIPED, _project(tmp_path), tmp_path)
    assert (hook.get("permissionDecision") == "deny") is denies, hook
