r"""A jit-context index row whose regex the matcher cannot honour (#1254).

`.claude/jit-context/**/00-index.tsv` rows are compiled by awk, not PCRE
(`claude-jit-context/scripts/pre-tool-hook.sh:80`, `pre-path-hook.sh:105`).
macOS ships the one-true-awk, which drops an undefined string escape before
compiling: `gh\s+pr` becomes `ghs+pr` and matches nothing at all. Two `block`
rules were dead this way on 2026-08-10, one of which had never fired since the
day it was written. Nothing anywhere said so — a rule
that never matches and a rule that never runs render identically in the index,
in a directory listing, and in the hook log.

Measured on the machine that found it, `awk version 20200816`:

    backslash-s : no       backslash-d : no
    backslash-w : no       backslash-b : no
    posix class : MATCH    backslash-n : MATCH

**awk does not fail to compile `\s`.** It compiles it, silently, into the
wrong thing and exits 0. So a validator that merely hands each pattern to awk
and checks the exit status returns `ok` on the exact row that produced this
issue. The structural half is therefore the load-bearing one, and it is
deliberately awk-independent: a pattern has to survive the most conservative
POSIX awk, because working under gawk is not a licence to write `\s` for a
hook that fires on a developer's Mac.

The compile half answers a different question. A genuinely malformed regex
(`gh[a`) is a *fatal* awk error, and the hook is invoked as `bash "$S" || true`,
so one unbalanced bracket silences every rule in the file rather than one row.

Would these pass if the code did nothing? No. Each finding test asserts a
refusal naming the offending row and the replacement construct; the skip tests
assert the verdict keys are *absent*, which a fabricated `ok:true` cannot do.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ADAPTER = REPO / "validators" / "jit-index" / "jit-index.py"
CONFIG = REPO / ".supertool.json"

HAS_AWK = shutil.which("awk") is not None
needs_awk = pytest.mark.skipif(
    not HAS_AWK,
    reason="awk absent: the adapter declines, so no clean verdict is available to assert")

TAB = "\t"


def _run(target, env_path=None):
    env = None
    if env_path is not None:
        env = dict(os.environ)
        env["PATH"] = env_path
    proc = subprocess.run(
        [sys.executable, str(ADAPTER), str(target)],
        capture_output=True, text=True, env=env)
    assert proc.stdout.strip(), "adapter emitted nothing (stderr: {0})".format(proc.stderr)
    return json.loads(proc.stdout)


def _tool_row(match, mode="block", rule="r.md", tool="Bash"):
    """A tools row as the hook splits it: six fields, trailing tab."""
    return TAB.join([tool, match, rule, mode, "", ""])


def _index(tmp_path, family, *rows):
    d = tmp_path / "jit-context" / family / "00-manual"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "00-index.tsv"
    f.write_text("".join(r + "\n" for r in rows), encoding="utf-8")
    return f


def _tools_index(tmp_path, *rows):
    return _index(tmp_path, "tools", *rows)


def _paths_index(tmp_path, *rows):
    return _index(tmp_path, "paths", *rows)


def _msgs(result):
    return " | ".join(e["msg"] for e in result.get("errors", []))


class TestDeadEscapes:
    """The construct that started this. No awk needed to reach the verdict."""

    def test_backslash_s_is_refused_and_names_the_posix_class(self, tmp_path):
        idx = _tools_index(tmp_path, _tool_row(r"~(^|[;&|\n] *)gh\s+pr"))
        result = _run(idx)
        assert result["ok"] is False
        assert result["count"] == 1
        assert "[[:space:]]" in _msgs(result)
        assert result["errors"][0]["line"] == 1

    def test_backslash_d_names_the_digit_class(self, tmp_path):
        idx = _tools_index(tmp_path, _tool_row(r"~pr \d+", mode="remind"))
        result = _run(idx)
        assert result["ok"] is False
        assert "[[:digit:]]" in _msgs(result)

    def test_backslash_w_names_the_alnum_class(self, tmp_path):
        idx = _tools_index(tmp_path, _tool_row(r"~\w+ push", mode="remind"))
        result = _run(idx)
        assert result["ok"] is False
        assert "[[:alnum:]_]" in _msgs(result)

    def test_backslash_b_is_a_backspace_not_a_word_boundary(self, tmp_path):
        """awk defines backslash-b, so it is not dropped: it compiles to a backspace."""
        idx = _tools_index(tmp_path, _tool_row(r"~\bgit push"))
        result = _run(idx)
        assert result["ok"] is False
        assert "backspace" in _msgs(result).lower()

    def test_an_unlisted_pcre_escape_is_caught_too(self, tmp_path):
        """backslash-h is on no denylist anywhere. The rule is structural, not enumerated."""
        idx = _tools_index(tmp_path, _tool_row(r"~gh\h+pr"))
        result = _run(idx)
        assert result["ok"] is False
        assert r"\h" in _msgs(result)

    def test_escaped_punctuation_is_left_alone(self, tmp_path):
        """An escaped dot is a legitimate escape and means what the author meant."""
        idx = _tools_index(tmp_path, _tool_row(r"~supertool\.py [;&|]", mode="remind"))
        result = _run(idx)
        assert _msgs(result) == ""

    def test_newline_escape_survives_and_is_not_flagged(self, tmp_path):
        idx = _tools_index(tmp_path, _tool_row(r"~(^|[;&|\n] *)git push"))
        result = _run(idx)
        assert _msgs(result) == ""


class TestLowercasedSubject:
    """`match(tolower(full_command), ...)` — pre-tool-hook.sh:80."""

    def test_uppercase_literal_can_never_match(self, tmp_path):
        idx = _tools_index(tmp_path, _tool_row("~gh PR view"))
        result = _run(idx)
        assert result["ok"] is False
        assert "lowercase" in _msgs(result).lower()

    def test_a_literal_row_is_lowercased_on_both_sides_so_case_is_fine(self, tmp_path):
        """Rows without a leading tilde go through index(tolower(a), tolower(b))."""
        idx = _tools_index(tmp_path, _tool_row("gh PR view", mode="remind"))
        result = _run(idx)
        assert _msgs(result) == ""

    def test_paths_index_is_not_lowercased_so_uppercase_is_fine(self, tmp_path):
        """pre-path-hook.sh:105 matches the raw path — case is meaningful there."""
        idx = _paths_index(tmp_path, "docs/SCHEMA" + TAB + "schema.md")
        result = _run(idx)
        assert _msgs(result) == ""


class TestPathsIndexIsRegexToo:
    """Column 1 of a paths index is handed to match(), not index()."""

    def test_a_dead_escape_in_a_paths_row_is_refused(self, tmp_path):
        idx = _paths_index(tmp_path, r"presets/\w+/" + TAB + "p.md")
        result = _run(idx)
        assert result["ok"] is False
        assert "[[:alnum:]_]" in _msgs(result)


class TestMalformedAbortsEveryRule:

    @needs_awk
    def test_an_unterminated_class_is_refused(self, tmp_path):
        idx = _tools_index(tmp_path, _tool_row("~gh[a"))
        result = _run(idx)
        assert result["ok"] is False
        assert "awk" in _msgs(result).lower()


class TestThirdState:
    """`skipped` is not `ok` — the defect this issue is about, one layer in."""

    def test_awk_absent_and_structurally_clean_is_a_skip_not_a_pass(self, tmp_path):
        idx = _tools_index(tmp_path, _tool_row("~gh[[:space:]]+pr"))
        result = _run(idx, env_path=str(tmp_path))
        assert "skipped" in result
        assert "ok" not in result
        assert "count" not in result
        assert "errors" not in result

    def test_awk_absent_does_not_suppress_a_structural_finding(self, tmp_path):
        idx = _tools_index(tmp_path, _tool_row(r"~gh\s+pr"))
        result = _run(idx, env_path=str(tmp_path))
        assert result["ok"] is False
        assert "[[:space:]]" in _msgs(result)

    def test_a_broken_row_is_a_finding_even_when_it_is_the_only_row(self, tmp_path):
        """The skip arm must not swallow the answer it already has.

        `\\tp.md` has a paths row's two fields and an empty pattern column — an
        empty pattern is handed to `match()` and matches every path. It leaves
        `parsed` at zero exactly as unparseable prose does, and collapsing the
        two cases turned a finding into "this is not an index".
        """
        idx = _paths_index(tmp_path, TAB + "p.md")
        result = _run(idx)
        assert "skipped" not in result
        assert result["ok"] is False
        assert result["errors"][0]["code"] == "shape"
        assert result["errors"][0]["line"] == 1

    @pytest.mark.skipif(os.name != "posix",
                        reason="needs an executable shim on PATH to make awk fail to exec")
    def test_awk_that_cannot_run_is_a_skip_not_a_pass(self, tmp_path):
        """awk found by `which` and unable to start is a no-verdict, not a clean one.

        Distinct from awk being *absent*, and distinct from awk exiting
        non-zero — that is a finding, and it is loud.
        """
        binder = tmp_path / "bin"
        binder.mkdir()
        shim = binder / "awk"
        shim.write_text("#!/nonexistent/interpreter\n", encoding="utf-8")
        shim.chmod(0o755)

        idx = _tools_index(tmp_path, _tool_row("~gh[[:space:]]+pr"))
        result = _run(idx, env_path=str(binder))
        assert "skipped" in result
        assert "ok" not in result
        assert "count" not in result
        assert "errors" not in result
        assert "never compiled" in result["skipped"]

    def test_a_file_that_is_not_an_index_is_a_skip(self, tmp_path):
        d = tmp_path / "00-manual"
        d.mkdir(parents=True)
        f = d / "00-index.tsv"
        f.write_text("this file has no tabs at all\n", encoding="utf-8")
        result = _run(f)
        assert "skipped" in result
        assert "ok" not in result

    def test_an_empty_index_is_a_skip(self, tmp_path):
        idx = _tools_index(tmp_path)
        result = _run(idx)
        assert "skipped" in result
        assert "ok" not in result


class TestThisRepo:
    """The regression guard proper: both live indexes, and a count of what was read."""

    @needs_awk
    @pytest.mark.parametrize("family", ["tools", "paths"])
    def test_live_index_is_clean_and_every_pattern_was_examined(self, family):
        idx = REPO / ".claude" / "jit-context" / family / "00-manual" / "00-index.tsv"
        assert idx.is_file(), idx

        # Counted here independently of the adapter, and from the file rather
        # than from a literal, so adding a rule does not redden this test — the
        # claim being pinned is "it read every regex", not "there are seven".
        expected = 0
        for raw in idx.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            fields = raw.split(TAB)
            if family == "tools" and len(fields) >= 4 and fields[1].startswith("~"):
                expected += 1
            elif family == "paths" and len(fields) == 2:
                expected += 1
        assert expected > 0, "the fixture found no patterns; the index shape changed"

        result = _run(idx)
        assert result["ok"] is True, _msgs(result)
        assert result["metrics"]["patterns_checked"] == expected


class TestRegistration:

    def test_declared_in_this_repos_config(self):
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        entry = cfg["validators"]["jit-index"]
        assert entry["match"] == "*00-manual/00-index.tsv"
        for op in ("edit", "replace", "replace_lines", "paste", "append"):
            assert op in entry["hooks_into"]
