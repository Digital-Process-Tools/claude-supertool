"""The delegated grep must disclose what it hid, not just hide it (#764).

#691 T5 made `grep` skip credential-shaped files and *say so*, because the
count is the entire justification for hiding one silently. That clause is
produced by the native walker. On the rtk-delegated path `--exclude=NAME`
went to the system grep, the file never came back, `_rtk_drop_excluded`
dropped nothing, and `_rtk_grep_report` printed no clause — so the disclosure
was inverted against usefulness: honest when the flags *failed*, silent when
they worked, which is the fast path and the common one.

**The fixture trap this suite exists to avoid.** Every wildcard entry
(`*.pem`, `.env.*`, `id_rsa*`) is already withheld from the argv, because the
default list carries negations (`!.env.example`) and system grep cannot
express them. So a tree containing a `server.pem` *already* comes back from
grep, is caught by the post-filter, and triggers the native redo and an honest
report — on master. A test built on that tree passes against the defect. The
silence lives in the **literal** half alone: `.env`, `.netrc`, `.npmrc`,
`.pgpass` and friends, which grep really did skip.

Every value here is obviously fake, and no test prints one on failure.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

import supertool


# Credential files hidden by a *literal* entry — the half the delegated argv
# genuinely suppressed. Deliberately no `*.pem` / `.env.local`: those take the
# already-honest path and would mask the defect. See the module docstring.
LITERAL_SECRETS = {
    ".env": "TOK=FAKE_root_env_value\n",
    ".netrc": "machine example.com password FAKE_netrc_value\n",
}
NESTED_SECRET = ("sub/.env", "TOK=FAKE_nested_env_value\n")

VISIBLE = {
    "app.py": 'TOKEN_NAME = "FAKE_source_value"\n',
    "sub/lib.py": 'X = "FAKE_nested_source_value"\n',
}

SECRET_NEEDLES = tuple(
    re.search(r"FAKE_\w+", body).group(0) for body in LITERAL_SECRETS.values()
) + (re.search(r"FAKE_\w+", NESTED_SECRET[1]).group(0),)

HIDDEN_COUNT = len(LITERAL_SECRETS) + 1


def _make_literal_secret_tree(root: Path) -> None:
    (root / "sub").mkdir()
    for name, body in LITERAL_SECRETS.items():
        (root / name).write_text(body, encoding="utf-8")
    (root / NESTED_SECRET[0]).write_text(NESTED_SECRET[1], encoding="utf-8")
    for name, body in VISIBLE.items():
        (root / name).write_text(body, encoding="utf-8")


def _assert_no_secret(out: str) -> None:
    leaked = [n for n in SECRET_NEEDLES if n in out]
    assert not leaked, f"{len(leaked)} secret value(s) reached the output"


def _hidden_count(out: str) -> int | None:
    m = re.search(r"(\d+) files hidden by exclude-paths", out)
    return int(m.group(1)) if m else None


class _RtkCalls:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []


@pytest.fixture
def rtk_real_grep(monkeypatch: pytest.MonkeyPatch) -> _RtkCalls:
    """Delegate to the *system* grep, exactly as rtk does.

    Canned output would let a broken argv pass unnoticed; this runs the real
    grep with the argv supertool built, so whether `--exclude=.env` is emitted
    genuinely decides whether `.env` comes back.
    """
    if not shutil.which("grep"):
        pytest.skip("system grep unavailable")
    seen = _RtkCalls()

    def _fake_rtk_run(args, timeout: int = 30) -> str | None:
        seen.calls.append(list(args))
        assert args[0] == "grep"
        proc = subprocess.run(
            ["grep"] + list(args[1:]),
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        return proc.stdout if proc.returncode == 0 else None

    monkeypatch.setattr(supertool, "_CONFIG", {"rtk": True})
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    monkeypatch.setattr(supertool, "_RTK_CHECKED", True)
    monkeypatch.setattr(supertool, "_RTK_PATH", "/fake/bin/rtk")
    monkeypatch.setattr(supertool, "_rtk_run", _fake_rtk_run)
    return seen


# ---------------------------------------------------------------------------
# The fixture's own premise
# ---------------------------------------------------------------------------


def test_the_fixture_tree_is_hidden_only_by_literal_entries() -> None:
    """Guards the trap in the module docstring.

    If someone adds a `server.pem` here, every test below starts passing
    against the defect — the wildcard half already reaches the honest report.
    This asserts each hidden file needs a literal entry to be hidden at all.
    """
    excl = supertool._get_exclude_paths("grep")
    non_literal = tuple(
        p for p in excl
        if p.startswith("!") or supertool.WILDCARD_CHARS.search(p.rstrip("/"))
    )
    for name in list(LITERAL_SECRETS) + [NESTED_SECRET[0]]:
        assert supertool._is_excluded(name, excl), f"{name} is not excluded"
        assert not supertool._is_excluded(name, non_literal), (
            f"{name} is hidden by a wildcard entry — it takes the already-"
            "honest path and pins nothing about #764"
        )


# ---------------------------------------------------------------------------
# The defect
# ---------------------------------------------------------------------------


def test_delegated_grep_discloses_the_files_it_hid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rtk_real_grep: _RtkCalls
) -> None:
    """The post-condition. Asserting only "no secret in the output" passes
    against the defect — the broken code does not show the file either, it
    just never says so."""
    _make_literal_secret_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    out = supertool.op_grep("FAKE", ".", limit=100)
    assert rtk_real_grep.calls, "delegated branch not taken — this pins nothing"
    _assert_no_secret(out)
    assert "FAKE_source_value" in out
    assert _hidden_count(out) == HIDDEN_COUNT, (
        f"report does not disclose {HIDDEN_COUNT} hidden files: "
        f"{out.splitlines()[0] if out else '<empty>'!r}"
    )


def test_both_engines_disclose_the_same_hidden_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rtk_real_grep: _RtkCalls
) -> None:
    """Which backend ran must never change the answer — including the part of
    the answer that is about what is missing from it."""
    _make_literal_secret_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    delegated = supertool.op_grep("FAKE", ".", limit=100)
    assert rtk_real_grep.calls
    monkeypatch.setattr(supertool, "_RTK_PATH", None)
    native = supertool.op_grep("FAKE", ".", limit=100)
    assert _hidden_count(delegated) == _hidden_count(native)


# ---------------------------------------------------------------------------
# The argv split, and the traversal win it must not cost
# ---------------------------------------------------------------------------


class TestGrepExcludeFlagsDisclosureSplit:
    def test_disclosable_entries_withhold_the_file_flag(self) -> None:
        """`--exclude=.env` makes the file invisible to the post-filter, and
        an uncounted drop is the whole defect. `--exclude-dir` stays: a pruned
        directory is not counted by the native walker either, so withholding
        it would cost traversal and buy no disclosure."""
        flags = supertool._grep_exclude_flags((".env/", ".netrc/"))
        assert "--exclude=.env" not in flags
        assert "--exclude=.netrc" not in flags
        assert "--exclude-dir=.env" in flags
        assert "--exclude-dir=.netrc" in flags

    def test_noise_entries_keep_both_flags(self) -> None:
        """Noise is never counted, so there is nothing to disclose and the
        traversal win is the entire point of delegating."""
        flags = supertool._grep_exclude_flags((".git/", "node_modules/"))
        assert "--exclude=.git" in flags
        assert "--exclude-dir=.git" in flags
        assert "--exclude=node_modules" in flags
        assert "--exclude-dir=node_modules" in flags

    def test_a_project_entry_is_treated_as_disclosable(self) -> None:
        """`_is_disclosable_exclusion` counts anything not on the built-in
        noise list, and the argv must agree with the counter."""
        flags = supertool._grep_exclude_flags(("fixtures/",))
        assert "--exclude=fixtures" not in flags
        assert "--exclude-dir=fixtures" in flags


def test_a_noise_only_tree_still_takes_the_delegated_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rtk_real_grep: _RtkCalls
) -> None:
    """The cost bound. The native redo fires only when a *credential* file
    matched; an ordinary repo with a `node_modules` keeps the single delegated
    walk it had before."""
    (tmp_path / "app.py").write_text('X = "FAKE_source_value"\n', encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text(
        'const x = "FAKE_dep_value";\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    out = supertool.op_grep("FAKE", ".", limit=100)
    assert rtk_real_grep.calls
    assert "delegated to rtk" in out, "fell back to the native walker"
    assert "FAKE_dep_value" not in out
    assert _hidden_count(out) is None
