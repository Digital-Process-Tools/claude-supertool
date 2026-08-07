"""A numeric env knob set to junk must not crash, and must not go quiet (#654).

`presets/git/trail.py` read its three knobs with a bare
`int(os.environ.get(...))`, so `SUPERTOOL_MAX_COMMITS=x` ended a run in a
`ValueError` traceback pointing at `int()` — a failure that names neither the
variable, nor the value, nor what a good one looks like.

The obvious repair is the wrong one. Wrapping the parse in `try/except` and
returning the default converts a loud failure into a silent one: the knob is
then *ignored*, and a caller who set a cap believes it is in force when it is
not. That is strictly worse than the crash, and it is the defect class this
tracker exists for. So the contract asserted here is three-state — honour the
value, or say plainly that it could not be read **and what is being used
instead** — never both silently.

**The notice goes to stdout, and that is load-bearing.** `_run_custom_op` in
`supertool.py` returns `result.stdout` on success and appends `result.stderr`
*only* when the preset exits non-zero. A preset that warns on stderr and then
succeeds has its warning dropped on the floor by supertool itself — which would
have shipped exactly the silence this issue forbids. `test_notice_goes_to_stdout`
is the regression lock on that.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import supertool  # noqa: E402
from _preset_loader import load_preset_module  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def _decode(raw: bytes | None) -> str:
    return "" if raw is None else raw.decode("utf-8", errors="replace")


def _run_utf8(argv, *, check: bool = False, cwd=None, env=None, timeout=None):
    """Run `argv`, capture bytes, and decode them as UTF-8 — never as the locale codec.

    `subprocess.run(..., text=True)` with no `encoding=` decodes the child's
    output with the *locale's* preferred codec. On the Windows runners that is
    cp1252, which has no mapping for 0x90 — and 0x90 is the second byte of every
    UTF-8-encoded Control Picture (U+2400-U+243F). supertool prints those glyphs
    (`\u241b`, `\u241e`), so as soon as one reached a helper here the decode blew up
    inside `subprocess`'s reader thread, `proc.stdout` came back as `None`, and
    the caller's `proc.stdout + proc.stderr` raised

        TypeError: unsupported operand type(s) for +: 'NoneType' and 'str'

    which is how four Windows legs went red in #856 while ubuntu and macOS —
    whose locale codec is already UTF-8 — could not see the constraint at all.

    Decoding here takes the locale out of the question entirely, and
    `errors="replace"` means undecodable output degrades to a visible marker
    rather than taking the test down.

    The keyword arguments are spelled out rather than forwarded as `**kwargs`
    because #862 holds `tests/` to the same encoding rule as shipped code, and
    that rule declines to judge a call whose kwargs it cannot read — a
    forwarded `**kwargs` could carry `text=True` and hide the one property the
    rule exists to enforce.
    """
    proc = subprocess.run(argv, capture_output=True,
                          cwd=cwd, env=env, timeout=timeout)
    done = subprocess.CompletedProcess(
        proc.args, proc.returncode, _decode(proc.stdout), _decode(proc.stderr))
    if check:
        done.check_returncode()
    return done


@pytest.fixture()
def env_mod():
    """`presets/_env.py`, loaded the way a preset would import it."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "st_env_654", REPO_ROOT / "presets" / "_env.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------
# The shared helper: honour, or decline out loud.
# --------------------------------------------------------------------------

def test_good_value_is_honoured_and_silent(env_mod, monkeypatch, capsys):
    """A usable value is used, and says nothing.

    Without this the whole suite would pass on a helper that ignored the
    environment and always returned the default.
    """
    monkeypatch.setenv("SUPERTOOL_MAX_COMMITS", "7")
    assert env_mod.env_int("SUPERTOOL_MAX_COMMITS", 20) == 7
    assert capsys.readouterr().out == ""


def test_unset_uses_default_and_is_silent(env_mod, monkeypatch, capsys):
    monkeypatch.delenv("SUPERTOOL_MAX_COMMITS", raising=False)
    assert env_mod.env_int("SUPERTOOL_MAX_COMMITS", 20) == 20
    assert capsys.readouterr().out == ""


def test_junk_value_falls_back_and_names_all_three(env_mod, monkeypatch, capsys):
    """The message must name the variable, the value it saw, and the fallback.

    Asserting merely "did not raise" would pass on a bare `except: return
    default` — the silent repair this issue forbids.
    """
    monkeypatch.setenv("SUPERTOOL_MAX_COMMITS", "x")
    assert env_mod.env_int("SUPERTOOL_MAX_COMMITS", 20) == 20

    out = capsys.readouterr().out
    assert "SUPERTOOL_MAX_COMMITS" in out, "the variable is not named"
    assert "'x'" in out or '"x"' in out, "the offending value is not quoted back"
    assert "20" in out, "the fallback actually in force is not stated"
    assert re.search(r"\busing 20\b", out), (
        "the message must say what is being used, not merely that something failed")


def test_empty_value_is_declined_not_silently_defaulted(env_mod, monkeypatch, capsys):
    """`SUPERTOOL_MAX_COMMITS=` is a set-but-unusable knob, not an unset one."""
    monkeypatch.setenv("SUPERTOOL_MAX_COMMITS", "")
    assert env_mod.env_int("SUPERTOOL_MAX_COMMITS", 20) == 20
    out = capsys.readouterr().out
    assert "SUPERTOOL_MAX_COMMITS" in out
    assert "using 20" in out


def test_float_value_for_int_knob_is_declined(env_mod, monkeypatch, capsys):
    monkeypatch.setenv("SUPERTOOL_MAX_COMMITS", "2.5")
    assert env_mod.env_int("SUPERTOOL_MAX_COMMITS", 20) == 20
    assert "SUPERTOOL_MAX_COMMITS" in capsys.readouterr().out


# --------------------------------------------------------------------------
# Negatives: a decision, not an inheritance.
# --------------------------------------------------------------------------

def test_below_minimum_is_declined_out_loud(env_mod, monkeypatch, capsys):
    """`SUPERTOOL_MAX_COMMITS=-5` must not silently mean "none".

    It is announced and falls back to the documented default — the same rule and
    the same message shape as an unparseable value, because -5 expresses no more
    usable intent than "x" does. A silent `max(0, ...)` clamp would turn "show me
    -5 commits" into "show me none" with nothing said, which is the silent class
    again wearing a different hat.
    """
    monkeypatch.setenv("SUPERTOOL_MAX_COMMITS", "-5")
    assert env_mod.env_int("SUPERTOOL_MAX_COMMITS", 20, minimum=1) == 20

    out = capsys.readouterr().out
    assert "SUPERTOOL_MAX_COMMITS" in out
    assert "-5" in out
    assert "using 20" in out
    assert "1" in out, "the minimum that was violated is not stated"


def test_zero_below_minimum_one_is_declined(env_mod, monkeypatch, capsys):
    monkeypatch.setenv("SUPERTOOL_PER_PAGE", "0")
    assert env_mod.env_int("SUPERTOOL_PER_PAGE", 50, minimum=1) == 50
    assert "using 50" in capsys.readouterr().out


def test_zero_is_honoured_when_minimum_allows_it(env_mod, monkeypatch, capsys):
    """minimum=0 knobs (an enrich *cap*) legitimately accept 0."""
    monkeypatch.setenv("SUPERTOOL_ENRICH_CAP", "0")
    assert env_mod.env_int("SUPERTOOL_ENRICH_CAP", 40, minimum=0) == 0
    assert capsys.readouterr().out == ""


def test_minimum_boundary_value_is_honoured(env_mod, monkeypatch, capsys):
    monkeypatch.setenv("SUPERTOOL_MAX_COMMITS", "1")
    assert env_mod.env_int("SUPERTOOL_MAX_COMMITS", 20, minimum=1) == 1
    assert capsys.readouterr().out == ""


# --------------------------------------------------------------------------
# The float variant (delays, timeouts).
# --------------------------------------------------------------------------

def test_env_float_honours_good_value(env_mod, monkeypatch, capsys):
    monkeypatch.setenv("SUPERTOOL_STAR_DELAY", "0.25")
    assert env_mod.env_float("SUPERTOOL_STAR_DELAY", 1.0) == 0.25
    assert capsys.readouterr().out == ""


def test_env_float_declines_junk_out_loud(env_mod, monkeypatch, capsys):
    monkeypatch.setenv("SUPERTOOL_STAR_DELAY", "soon")
    assert env_mod.env_float("SUPERTOOL_STAR_DELAY", 1.0) == 1.0
    out = capsys.readouterr().out
    assert "SUPERTOOL_STAR_DELAY" in out
    assert "'soon'" in out
    assert "using 1.0" in out


def test_env_float_declines_below_minimum(env_mod, monkeypatch, capsys):
    monkeypatch.setenv("SUPERTOOL_STAR_DELAY", "-1")
    assert env_mod.env_float("SUPERTOOL_STAR_DELAY", 1.0, minimum=0.0) == 1.0
    assert "using 1.0" in capsys.readouterr().out


def test_notice_goes_to_stdout(env_mod, monkeypatch, capsys):
    """Not stderr — supertool drops a successful preset's stderr entirely.

    `_run_custom_op` returns `result.stdout` and only appends `result.stderr`
    when the preset exits non-zero. A warning on stderr from a preset that then
    succeeds is discarded by supertool before the caller ever sees it, which
    would reintroduce the silence this fix exists to prevent.
    """
    monkeypatch.setenv("SUPERTOOL_MAX_COMMITS", "nope")
    env_mod.env_int("SUPERTOOL_MAX_COMMITS", 20)
    captured = capsys.readouterr()
    assert "SUPERTOOL_MAX_COMMITS" in captured.out
    assert "SUPERTOOL_MAX_COMMITS" not in captured.err


def test_a_repeated_bad_read_is_announced_once(env_mod, monkeypatch, capsys):
    """Several knobs are read from helpers called once per file or per git call.

    Without dedupe, one bad `SUPERTOOL_READ_MAX_LINES` prints its notice six
    times above the output it is warning about — and a message that repeats is
    a message readers learn to skip, which costs the fix its whole point.
    """
    monkeypatch.setenv("SUPERTOOL_DEFAULT_LIMIT", "many")
    for _ in range(5):
        assert env_mod.env_int("SUPERTOOL_DEFAULT_LIMIT", 10) == 10
    out = capsys.readouterr().out
    assert out.count("SUPERTOOL_DEFAULT_LIMIT") == 1, out


def test_dedupe_is_per_message_not_per_variable(env_mod, monkeypatch, capsys):
    """A knob that goes wrong in a *different* way still gets its own line.

    Keying on the variable alone would swallow the second, distinct fault.
    """
    monkeypatch.setenv("SUPERTOOL_DEFAULT_LIMIT", "many")
    env_mod.env_int("SUPERTOOL_DEFAULT_LIMIT", 10)
    monkeypatch.setenv("SUPERTOOL_DEFAULT_LIMIT", "-2")
    env_mod.env_int("SUPERTOOL_DEFAULT_LIMIT", 10, minimum=1)
    out = capsys.readouterr().out
    assert out.count("SUPERTOOL_DEFAULT_LIMIT") == 2, out
    assert "'many'" in out and "'-2'" in out


# --------------------------------------------------------------------------
# supertool.py's own copy. Coverage measures this file only.
# --------------------------------------------------------------------------

def test_supertool_env_int_honours_good_value(monkeypatch, capsys):
    monkeypatch.setenv("SUPERTOOL_DISPATCH_MAX_DEPTH", "9")
    assert supertool._env_int("SUPERTOOL_DISPATCH_MAX_DEPTH", 32, minimum=1) == 9
    assert capsys.readouterr().out == ""


def test_supertool_env_int_declines_junk_out_loud(monkeypatch, capsys):
    monkeypatch.setenv("SUPERTOOL_DISPATCH_MAX_DEPTH", "deep")
    assert supertool._env_int("SUPERTOOL_DISPATCH_MAX_DEPTH", 32, minimum=1) == 32
    out = capsys.readouterr().out
    assert "SUPERTOOL_DISPATCH_MAX_DEPTH" in out
    assert "'deep'" in out
    assert "using 32" in out


def test_supertool_env_int_declines_below_minimum(monkeypatch, capsys):
    monkeypatch.setenv("SUPERTOOL_DISPATCH_MAX_DEPTH", "-3")
    assert supertool._env_int("SUPERTOOL_DISPATCH_MAX_DEPTH", 32, minimum=1) == 32
    assert "using 32" in capsys.readouterr().out


def test_supertool_env_float_declines_junk_out_loud(monkeypatch, capsys):
    monkeypatch.setenv("SUPERTOOL_MCP_CONNECT_TIMEOUT", "later")
    assert supertool._env_float("SUPERTOOL_MCP_CONNECT_TIMEOUT", 5.0, minimum=0.0) == 5.0
    out = capsys.readouterr().out
    assert "SUPERTOOL_MCP_CONNECT_TIMEOUT" in out
    assert "using 5.0" in out


def test_lint_timeout_no_longer_defaults_in_silence(monkeypatch, capsys):
    """`_lint_timeout` already tolerated junk — silently, which is the other half
    of the same defect. `SUPERTOOL_LINT_TIMEOUT=0` used to fall back to the
    default with nothing said, so a runner configured with a bad timeout looked
    exactly like one configured correctly."""
    monkeypatch.setenv("SUPERTOOL_LINT_TIMEOUT", "0")
    assert supertool._lint_timeout() == supertool._LINT_TIMEOUT_DEFAULT
    out = capsys.readouterr().out
    assert "SUPERTOOL_LINT_TIMEOUT" in out
    assert str(supertool._LINT_TIMEOUT_DEFAULT) in out


def test_parallel_knob_junk_is_announced(monkeypatch, capsys):
    """`SUPERTOOL_PARALLEL=x` returned 0 — identical to never setting it.

    A caller who asked for parallelism and got none had no way to tell the
    difference. This one never crashed; it was silent from the start, which is
    the half of the defect that is easier to miss.
    """
    monkeypatch.setenv("SUPERTOOL_PARALLEL", "lots")
    assert supertool._parallel_workers() == 0
    out = capsys.readouterr().out
    assert "SUPERTOOL_PARALLEL" in out
    assert "'lots'" in out
    assert "sequential" in out


def test_parallel_knob_negative_is_announced(monkeypatch, capsys):
    """`max(0, -4)` quietly turned "-4 workers" into "no parallelism"."""
    monkeypatch.setenv("SUPERTOOL_PARALLEL", "-4")
    assert supertool._parallel_workers() == 0
    out = capsys.readouterr().out
    assert "SUPERTOOL_PARALLEL" in out
    assert "-4" in out


def test_parallel_knob_good_values_still_work(monkeypatch, capsys):
    monkeypatch.setenv("SUPERTOOL_PARALLEL", "6")
    assert supertool._parallel_workers() == 6
    monkeypatch.setenv("SUPERTOOL_PARALLEL", "true")
    assert supertool._parallel_workers() == 4
    monkeypatch.setenv("SUPERTOOL_PARALLEL", "off")
    assert supertool._parallel_workers() == 0
    assert capsys.readouterr().out == ""


def test_op_int_env_override_junk_is_announced(monkeypatch, capsys):
    """`SUPERTOOL_READ_MAX_LINES=x` fell through to config in silence.

    The env override is documented as taking precedence over JSON config, so a
    discarded one is a broken promise — and it read exactly like no override.
    """
    monkeypatch.setenv("SUPERTOOL_READ_MAX_LINES", "x")
    value = supertool._get_op_int("read", "max_lines", 300)
    out = capsys.readouterr().out
    assert "SUPERTOOL_READ_MAX_LINES" in out
    assert "'x'" in out
    assert f"using {value}" in out, "the limit actually in force is not named"


def test_op_int_env_override_zero_is_announced(monkeypatch, capsys):
    monkeypatch.setenv("SUPERTOOL_READ_MAX_LINES", "0")
    value = supertool._get_op_int("read", "max_lines", 300)
    out = capsys.readouterr().out
    assert "SUPERTOOL_READ_MAX_LINES" in out
    assert f"using {value}" in out


def test_op_int_repeated_reads_announce_once(monkeypatch, capsys):
    """`_get_op_int` is consulted several times for a single `read`.

    `_ENV_ANNOUNCED` is per-run scratch and is listed in `conftest.RESET_GLOBALS`
    (#397), so each test starts with nothing already announced.
    """
    monkeypatch.setenv("SUPERTOOL_READ_MAX_LINES", "plenty")
    for _ in range(4):
        supertool._get_op_int("read", "max_lines", 300)
    out = capsys.readouterr().out
    assert out.count("SUPERTOOL_READ_MAX_LINES") == 1, out


def test_git_timeout_junk_is_announced(monkeypatch, capsys):
    """`SUPERTOOL_GIT_TIMEOUT` arrived with #650 in the same silent shape.

    It swallowed junk *and* a non-positive value without a word, so a runner
    configured with a bad budget looked exactly like one configured correctly.
    Caught by this issue's scanning test the moment #650 merged.
    """
    monkeypatch.setenv("SUPERTOOL_GIT_TIMEOUT", "ages")
    assert supertool._git_timeout() == supertool._GIT_TIMEOUT_DEFAULT
    out = capsys.readouterr().out
    assert "SUPERTOOL_GIT_TIMEOUT" in out
    assert f"using {supertool._GIT_TIMEOUT_DEFAULT}" in out


def test_git_timeout_good_value_is_honoured(monkeypatch, capsys):
    monkeypatch.setenv("SUPERTOOL_GIT_TIMEOUT", "30")
    assert supertool._git_timeout() == 30
    assert capsys.readouterr().out == ""


def test_op_int_env_override_good_value_wins_and_is_silent(monkeypatch, capsys):
    monkeypatch.setenv("SUPERTOOL_READ_MAX_LINES", "42")
    assert supertool._get_op_int("read", "max_lines", 300) == 42
    assert capsys.readouterr().out == ""


# --------------------------------------------------------------------------
# End to end: the reproduction from the issue.
# --------------------------------------------------------------------------

def _run_supertool(args, env_extra):
    import os

    env = dict(os.environ)
    env.update(env_extra)
    env["SUPERTOOL_NO_RTK"] = "1"
    return _run_utf8(
        [sys.executable, str(REPO_ROOT / "supertool.py"), *args],
        timeout=120, cwd=str(REPO_ROOT), env=env,
    )


def test_issue_reproduction_no_longer_tracebacks():
    """`SUPERTOOL_MAX_COMMITS=x ./supertool 'git-trail:...'` — the issue's repro.

    Deliberately asserts nothing about how many commits came back. It runs
    against whatever history the checkout happens to have, and its job is the
    part that does not vary: the crash is gone, and the notice **survives the
    trip through `_run_custom_op`** — which is the claim that matters here,
    because that function returns a successful subprocess's stdout and discards
    its stderr. "The fallback is the value actually used" is proved separately,
    against history this file builds; see `test_junk_knob_falls_back_to_the_cap_
    that_is_actually_applied`.
    """
    proc = _run_supertool(["git-trail:return:supertool.py"],
                          {"SUPERTOOL_MAX_COMMITS": "x"})
    combined = proc.stdout + proc.stderr
    # The op receipt, not a substring search: this preset prints source diffs,
    # and the word "ValueError" legitimately appears inside supertool's own
    # `except ValueError:` lines. `PASS (` is emitted only when the subprocess
    # exited 0, so it is the assertion that cannot be faked by printed content.
    assert "\nPASS (" in combined, combined[:2000]
    assert "Traceback (most recent call last)" not in combined, combined[-2000:]
    assert "note: SUPERTOOL_MAX_COMMITS='x'" in combined, combined[:2000]
    assert "using 20" in combined, combined[-2000:]


# --------------------------------------------------------------------------
# The decode, which the locale must not get a vote in (#856).
# --------------------------------------------------------------------------

#: A UTF-8 Control Picture whose second byte, 0x90, is unmapped in cp1252.
#: `\u241b` is what supertool renders `\x1b` as; `\u241e` appears in its own source.
_CONTROL_PICTURES = "\u241e RS \u241b ESC"


def test_supertool_output_is_undecodable_under_the_windows_locale_codec(tmp_path):
    """The hazard the helpers defend against, forced on any platform.

    Not a platform-gated skip: cp1252 is a stock codec everywhere, so asking for
    it explicitly reproduces the Windows failure on a Mac. If this ever stops
    raising, supertool has stopped emitting Control Pictures and the guard below
    is defending against nothing — which is worth failing over either way.
    """
    target = tmp_path / "glyphs.txt"
    target.write_text(f"alpha\n{_CONTROL_PICTURES}\nomega\n", encoding="utf-8")

    import os

    env = dict(os.environ)
    env["SUPERTOOL_NO_RTK"] = "1"
    raw = subprocess.run(
        [sys.executable, str(REPO_ROOT / "supertool.py"), f"read:{target}"],
        capture_output=True, timeout=120, cwd=str(REPO_ROOT), env=env,
    ).stdout
    assert b"\xe2\x90" in raw, "supertool emitted no Control Picture to decode"
    with pytest.raises(UnicodeDecodeError):
        raw.decode("cp1252")


def test_helper_returns_text_with_the_glyphs_intact(tmp_path):
    """And the helper hands back a `str`, not the `None` that produced #856."""
    target = tmp_path / "glyphs.txt"
    target.write_text(f"alpha\n{_CONTROL_PICTURES}\nomega\n", encoding="utf-8")

    proc = _run_supertool([f"read:{target}"], {})
    combined = proc.stdout + proc.stderr
    assert isinstance(proc.stdout, str) and isinstance(proc.stderr, str)
    assert "\u241e" in combined and "\u241b" in combined, combined[:2000]


def test_no_subprocess_here_leaves_decoding_to_the_locale():
    """The lock that a Mac cannot pass by accident.

    Every behavioural assertion above is satisfied on a UTF-8 platform whether or
    not the decoding is pinned, so the reintroduction of `text=True` without
    `encoding=` would go green on every leg the author can run. This reads the
    source instead, and so fails wherever it is run.

    It walks the AST rather than the text, because a regex over the source reads
    prose as code: the docstring on `_run_utf8` names the very construct being
    banned, and a textual scan duly reported it as a violation.
    """
    import ast

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and isinstance(n.func.value, ast.Name)
             and n.func.value.id == "subprocess"
             and n.func.attr in {"run", "Popen", "check_output"}]
    assert calls, "the scan matched no subprocess call at all — this guard has drifted"

    def _is_true(kw):
        return isinstance(kw.value, ast.Constant) and kw.value.value is True

    offenders = []
    for call in calls:
        names = {kw.arg for kw in call.keywords}
        text_mode = any(_is_true(kw) for kw in call.keywords
                        if kw.arg in {"text", "universal_newlines"})
        if text_mode and "encoding" not in names:
            offenders.append(f"line {call.lineno}")
    assert not offenders, (
        "decoding a child's output with the locale codec breaks the Windows legs "
        f"(#856); capture bytes and use _run_utf8 instead: {offenders}")


# --------------------------------------------------------------------------
# History the test owns, because the checkout's history is not ours to assume.
# --------------------------------------------------------------------------

#: More commits than `DEFAULT_MAX_COMMITS` (20), so the default cap visibly
#: bites. Without that headroom every assertion below passes vacuously.
_HISTORY_COMMITS = 25


@pytest.fixture(scope="session")
def history_repo(tmp_path_factory):
    """A git repo whose history this file builds, and can therefore rely on.

    Proving a cap is *in force* — not merely announced — needs more commits than
    the cap. Reading that from supertool's own history passed locally and failed
    on eight of fourteen CI legs: `actions/checkout` clones at depth 1 by
    default, so the repo there has exactly one commit and `## Timeline (20
    commits)` is unreachable by construction. The test was pinning a property of
    the developer's clone while claiming to pin a property of the fix.

    Twenty-five commits here cost about a second, once per session, and the
    claim stops depending on how the checkout was configured. Raising
    `fetch-depth` in the workflow would also have made it pass — by changing CI
    for every job in the repo to suit one assertion, which is the wrong lever.
    """
    repo = tmp_path_factory.mktemp("trail_history")

    def git(*args):
        _run_utf8(["git", *args], cwd=repo, check=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Knob Test")
    git("config", "commit.gpgsign", "false")
    target = repo / "data.txt"
    for i in range(_HISTORY_COMMITS):
        with target.open("a", encoding="utf-8") as fh:
            fh.write(f"NEEDLE occurrence {i}\n")
        git("add", "data.txt")
        git("commit", "-q", "-m", f"add occurrence {i}")
    return repo


def _run_trail(repo, env_extra):
    """Run the preset directly, in `repo`, with the detail section switched off.

    `SUPERTOOL_TRAIL_DETAIL_CAP=0` is a legitimate value (minimum 0) and so is
    silent; it just spares us one `git show` per commit, which is the only slow
    part of the preset.
    """
    import os

    env = dict(os.environ)
    env.setdefault("SUPERTOOL_TRAIL_DETAIL_CAP", "0")
    env.update(env_extra)
    return _run_utf8(
        [sys.executable, str(REPO_ROOT / "presets" / "git" / "trail.py"),
         "NEEDLE", "data.txt"],
        timeout=120, cwd=str(repo), env=env,
    )


def test_the_history_fixture_outgrows_the_default_cap(history_repo):
    """Guards every assertion below from passing vacuously.

    If the fixture ever built fewer than 21 commits, "the cap is 20" and "the
    cap is however many exist" would be indistinguishable, and the tests that
    rely on the difference would go quietly green.
    """
    count = _run_utf8(["git", "rev-list", "--count", "HEAD"],
                      cwd=history_repo, check=True).stdout.strip()
    assert int(count) == _HISTORY_COMMITS
    out = _run_trail(history_repo, {}).stdout
    assert "## Timeline (20 commits)" in out, out[:2000]
    assert "CAPPED" in out, "20 of 25 must render as a cut, not as the whole set"


def test_junk_knob_falls_back_to_the_cap_that_is_actually_applied(history_repo):
    """The announced number must be the number in force, not just printed.

    A helper that reported "using 20" and then applied something else would pass
    a message-only assertion. The timeline is the observable: 25 commits exist,
    20 come back.
    """
    proc = _run_trail(history_repo, {"SUPERTOOL_MAX_COMMITS": "x"})
    out = proc.stdout
    assert proc.returncode == 0, out + proc.stderr
    assert "Traceback (most recent call last)" not in (out + proc.stderr)
    assert "note: SUPERTOOL_MAX_COMMITS='x' is not a whole number" in out, out[:2000]
    assert "using 20" in out
    assert "## Timeline (20 commits)" in out, (
        "the announced fallback of 20 is not the cap that was actually applied\n"
        + out[:2000])


def test_negative_knob_falls_back_rather_than_showing_nothing(history_repo):
    """`SUPERTOOL_MAX_COMMITS=-5` must not quietly become "none".

    A `max(0, ...)` clamp would have produced an empty timeline with nothing
    said. The timeline is the observable: it holds 20, the documented default.
    """
    proc = _run_trail(history_repo, {"SUPERTOOL_MAX_COMMITS": "-5"})
    out = proc.stdout
    assert proc.returncode == 0, out + proc.stderr
    assert "note: SUPERTOOL_MAX_COMMITS='-5' is below the minimum of 1" in out, out[:2000]
    assert "using 20" in out
    assert "## Timeline (20 commits)" in out, out[:2000]
    assert "## Timeline (0 commits)" not in out


def test_dispatch_depth_junk_does_not_break_every_op(tmp_path):
    """`SUPERTOOL_DISPATCH_MAX_DEPTH` is parsed at *module scope*.

    A bad value there raised during import, so it took down every op in the
    tool — including ops that have nothing to do with dispatch depth. The blast
    radius is the whole binary, which makes it the worst instance of the class.
    """
    proc = _run_supertool(
        ["read:presets/_env.py:1:3"],
        {"SUPERTOOL_DISPATCH_MAX_DEPTH": "deep"},
    )
    combined = proc.stdout + proc.stderr
    assert "Traceback" not in combined, combined[-2000:]
    assert proc.returncode == 0, combined[-2000:]
    assert "SUPERTOOL_DISPATCH_MAX_DEPTH" in combined
    assert "using 32" in combined


def test_good_knob_still_reaches_the_preset(history_repo):
    """The fallback path must not be the only path.

    `SUPERTOOL_MAX_COMMITS=3` has to actually cut the timeline to 3 — otherwise
    every assertion above would hold just as well on a helper that ignored the
    environment and returned the default every time.
    """
    proc = _run_trail(history_repo, {"SUPERTOOL_MAX_COMMITS": "3"})
    out = proc.stdout
    assert proc.returncode == 0, out + proc.stderr
    assert "## Timeline (3 commits)" in out, out[:2000]
    assert "note: SUPERTOOL_" not in out, (
        "a good value must not be reported as a problem")


# --------------------------------------------------------------------------
# The sweep boundary, executable.
# --------------------------------------------------------------------------

#: Files still reading a numeric env knob with a bare `int(...)`/`float(...)`.
#:
#: #654 asks that a bounded sweep not render as complete coverage, so the
#: boundary lives here — where it fails a build when it moves — rather than only
#: in a PR body. The sweep reached every site, so it is empty; that is a claim
#: this test keeps honest rather than a formality, because the next preset to
#: add a bare parse fails here instead of shipping.
#:
#: It covers the *syntactic* pattern only. Two knobs in `supertool.py` are
#: numeric but not spelled this way and were swept by hand:
#: `_parallel_workers` (`SUPERTOOL_PARALLEL`, which also accepts true/false) and
#: `_get_op_int` (the `SUPERTOOL_<OP>_<KEY>` family). `SUPERTOOL_DEBUG` is read
#: for truthiness only and is not a numeric knob.
BARE_PARSE_ALLOWLIST: set[str] = set()

_BARE_PARSE = re.compile(r"(?:int|float)\(\s*os\.(?:environ\.get|getenv)\(")


def _scan_bare_parses():
    hits = {}
    for path in (sorted(REPO_ROOT.glob("presets/**/*.py"))
                 + [REPO_ROOT / "supertool.py", REPO_ROOT / "_supertool.py"]):
        rel = path.relative_to(REPO_ROOT).as_posix()
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        found = [i + 1 for i, ln in enumerate(lines) if _BARE_PARSE.search(ln)]
        if found:
            hits[rel] = found
    return hits


def test_swept_files_have_no_bare_numeric_env_parse():
    """No file outside the declared remainder still parses a knob barehanded."""
    hits = _scan_bare_parses()
    unexpected = {k: v for k, v in hits.items() if k not in BARE_PARSE_ALLOWLIST}
    assert not unexpected, (
        "bare numeric env parse outside the declared #654 sweep boundary: "
        f"{unexpected}")


def test_allowlist_is_not_stale():
    """Every file claimed as "left" must actually still have one.

    An allowlist that outlives its entries is how a sweep boundary quietly turns
    into a lie in the other direction.
    """
    hits = _scan_bare_parses()
    stale = sorted(BARE_PARSE_ALLOWLIST - set(hits))
    assert not stale, f"allowlist names files with no bare parse left: {stale}"
