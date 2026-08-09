"""#1143 -- the symlink blind spot is decided once, and it is legible.

Every symlink-dependent test in this suite used to answer the question
"can I make a symlink" for itself, in one of five spellings, split across two
kinds of answer:

  * a *platform* gate -- ``skipif(os.name == "nt")`` and its two synonyms.
    That is not a capability check. It is the assumption that Windows cannot,
    baked in so hard that a runner which *can* still never runs the test. On
    the platform where this repo has gone red most often, that is a blind spot
    the suite creates for itself and then cannot see.
  * a *runtime* probe -- try, catch OSError, ``pytest.skip``. Honest, but each
    with its own wording, so the skips are indistinguishable from the other
    ~680 in a Windows leg.

`tests/_symlink.py` takes the decision once, by probing rather than by naming a
platform, and `conftest.py` prints the verdict in the report header and the
skipped-for-this-reason count in the terminal summary. The skip stays a skip
where the privilege is genuinely absent -- what changes is that it is a
measured absence, stated in words, in every leg's log.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent

_PLATFORM_NAMES = ("os.name", "sys.platform", "platform.system")


def _reads_a_platform_name(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute):
            if isinstance(sub.value, ast.Name):
                if sub.value.id + "." + sub.attr in _PLATFORM_NAMES:
                    return True
        if isinstance(sub, ast.Call):
            f = sub.func
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                if f.value.id + "." + f.attr in _PLATFORM_NAMES:
                    return True
    return False


def _skipif_decorators(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            f = dec.func
            if isinstance(f, ast.Attribute) and f.attr == "skipif":
                yield node, dec


def _reason_text(dec: ast.Call) -> str:
    for kw in dec.keywords:
        if kw.arg == "reason" and isinstance(kw.value, ast.Constant):
            return str(kw.value.value)
    return ""


def _platform_gated_symlink_skips():
    """Every ``skipif`` that names a platform to decide a *symlink* question."""
    hits = []
    for path in sorted(TESTS.glob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a broken fixture module
            continue
        for func, dec in _skipif_decorators(tree):
            reason = _reason_text(dec)
            if "symlink" not in reason.lower():
                continue
            condition = dec.args[0] if dec.args else None
            if condition is None:
                for kw in dec.keywords:
                    if kw.arg == "condition":
                        condition = kw.value
            if condition is not None and _reads_a_platform_name(condition):
                hits.append((path.name, dec.lineno, func.name, reason))
    return hits


def test_no_symlink_test_is_gated_on_the_name_of_a_platform() -> None:
    """A platform name is not a capability, and guessing one is not a check.

    ``skipif(os.name == "nt")`` on a symlink test asserts nothing about the
    runner it is running on -- it hardcodes an answer. Where the privilege is
    present (Developer Mode, an elevated runner) the test still never executes,
    so the suite reports a coverage it does not have, on the one platform whose
    reds are load-bearing here. Use ``_symlink.requires_symlink`` /
    ``_symlink.require_symlink()``, which ask the filesystem.
    """
    hits = _platform_gated_symlink_skips()
    assert hits == [], (
        "these skips decide a symlink question by naming a platform instead of "
        "probing for the privilege:" + os.linesep
        + os.linesep.join(
            "  {0}:{1} {2} -- {3!r}".format(*h) for h in hits
        )
    )


def test_the_probe_agrees_with_the_filesystem(tmp_path) -> None:
    """The probe is the whole contract, so it is checked against reality.

    Not ``if os.name == "nt": assert not supported`` -- that would be the
    vacuous-on-one-platform branch this file exists to remove. Both arms assert
    something real on every platform.
    """
    import _symlink

    supported, why = _symlink.symlink_support()
    link = tmp_path / "link"
    try:
        os.symlink(str(tmp_path / "target"), str(link))
    except (OSError, NotImplementedError, AttributeError) as e:
        assert not supported, (
            "the probe reported symlinks available, but creating one here "
            "raised " + repr(e))
        assert why, "an unavailable verdict must carry its reason, never a bare False"
        return
    assert supported, (
        "creating a symlink here worked, but the probe reported it unavailable: "
        + repr(why))
    assert os.path.islink(str(link)), (
        "os.symlink returned but produced something os.path.islink denies -- an "
        "NTFS junction exercises the non-link branch and asserts nothing (#1143)")


def test_an_unavailable_verdict_names_itself_in_the_skip_reason() -> None:
    """The skip has to be findable in a 688-skip log, or it is not visible."""
    import _symlink

    supported, _ = _symlink.symlink_support()
    reason = _symlink.skip_reason()
    if supported:
        assert reason == "", "there is nothing to skip when the privilege is present"
        return
    assert _symlink.TOKEN in reason
    assert "symlink" in reason.lower()


def test_the_report_header_states_the_capability_on_every_platform() -> None:
    """Header, not summary: it prints on a green leg too.

    A blind spot that is only announced when something fails is announced
    exactly when nobody needs telling.
    """
    import conftest
    import _symlink

    lines = conftest.pytest_report_header(_HeaderConfig())
    text = os.linesep.join(lines) if isinstance(lines, list) else str(lines)
    assert _symlink.TOKEN in text, (
        "the report header does not state the symlink verdict: " + repr(text))
    supported, _ = _symlink.symlink_support()
    assert ("available" in text) if supported else ("unavailable" in text)


class _HeaderConfig:
    """The two attributes `pytest_report_header` reads, and nothing else."""

    def __init__(self) -> None:
        self._supertool_leaked_git_env = []

    def getoption(self, name, default=None):  # pragma: no cover - unused today
        return default


def test_the_terminal_summary_counts_the_symlink_skips_apart() -> None:
    """688 skipped is a number. `N skipped because X` is a fact."""
    import conftest
    import _symlink

    reporter = _FakeReporter([
        _FakeReport("Skipped: " + _symlink.TOKEN + ": no privilege"),
        _FakeReport("Skipped: " + _symlink.TOKEN + ": no privilege"),
        _FakeReport("Skipped: needs bun"),
    ])
    conftest.pytest_terminal_summary(reporter, 0, None)
    written = os.linesep.join(reporter.lines)
    assert "2" in written and _symlink.TOKEN in written, written
    assert "needs bun" not in written


class _FakeReport:
    def __init__(self, text: str) -> None:
        self.longrepr = ("some_test.py", 12, text)


class _FakeReporter:
    def __init__(self, skipped) -> None:
        self.stats = {"skipped": list(skipped)}
        self.lines = []

    def write_line(self, line, **kw) -> None:
        self.lines.append(line)

    def write_sep(self, sep, title=None, **kw) -> None:
        self.lines.append(str(title or sep))
