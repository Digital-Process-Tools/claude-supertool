"""Read ops need an unambiguous route for patterns containing ':' (#625).

Three concerns, in the order they matter:

1. A colon-bearing pattern must be able to find what it should. The colon-CLI
   already copes in the common `PATTERN:PATH:LIMIT` shape (the parsers peel
   trailing ints and take the LAST token as the path, rejoining the rest). It
   does NOT cope when the path is omitted, or in `between`, where the rejoin
   runs the other way.

2. A mis-tokenized read op must not read as an absence in the world. It
   already errors rather than returning a silent zero — that discipline is
   pinned here so it cannot regress — but the error must also NAME the cause
   and point at the escape, or the next person re-derives it from scratch.

3. The `@payload` route the mutating ops already have must round-trip a
   colon-bearing pattern through every read op it is offered on.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

import supertool


def _tree_sitter_installed() -> bool:
    """Is a tree-sitter language pack importable in THIS environment?

    `enable_tree_sitter` only clears supertool's cached detection flags — it
    cannot install the package. CI has no pack, so symbol mode legitimately
    declines there; that is a reason to skip one assertion, not to loosen it.
    """
    for name in ("tree_sitter_language_pack", "tree_sitter_languages"):
        try:
            if importlib.util.find_spec(name) is not None:
                return True
        except (ImportError, ValueError):
            continue
    return False


_HAS_TREE_SITTER = _tree_sitter_installed()


HAYSTACK = (
    "alpha\n"
    "Element: <div>\n"
    "beta\n"
    "A::CONST used here\n"
    "ERROR: boom\n"
    "passed\n"
    "failed\n"
)


@pytest.fixture()
def hay(tmp_path: Path) -> Path:
    f = tmp_path / "traces.txt"
    f.write_text(HAYSTACK, encoding="utf-8")
    return f


def _payload(tmp_path: Path, name: str, obj: dict) -> Path:
    f = tmp_path / name
    f.write_text(json.dumps(obj), encoding="utf-8")
    return f


# ---------------------------------------------------------------------------
# 1. Colon-CLI: what already works stays working (regression pins)
# ---------------------------------------------------------------------------

class TestColonCliStillWorks:
    """These pass today. They are the backward-compatibility contract."""

    def test_grep_colon_pattern_with_path_and_ints(self, hay: Path) -> None:
        out = supertool.dispatch(f"grep:Element: <:{hay}:8:0:no-auto-read")
        assert "Element: <div>" in out
        assert "1 results" in out

    def test_grep_alternation_ending_in_colon_bearing_branch(self, hay: Path) -> None:
        out = supertool.dispatch(f"grep:passed|failed|ERROR:{hay}:15:0:no-auto-read")
        assert "ERROR: boom" in out
        assert "3 results" in out

    def test_around_colon_pattern(self, hay: Path) -> None:
        out = supertool.dispatch(f"around:A::CONST:{hay}:2")
        assert "A::CONST used here" in out

    def test_at_prefixed_pattern_is_still_a_search(self, tmp_path: Path) -> None:
        """`grep:@Override:src/` is a real search. A leading '@' must not be
        read as a payload reference unless the reference actually resolves."""
        src = tmp_path / "T.java"
        src.write_text("@Override\npublic void run() {}\n", encoding="utf-8")
        out = supertool.dispatch(f"grep:@Override:{tmp_path}:5:0:no-auto-read")
        assert "ERROR" not in out
        assert "@Override" in out

    def test_at_prefixed_pattern_without_path_is_still_a_search(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "T.java").write_text("@Override\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        out = supertool.dispatch("grep:@Override")
        assert "ERROR" not in out
        assert "@Override" in out


# ---------------------------------------------------------------------------
# 2. Mis-tokenization must be loud, and must name the cause
# ---------------------------------------------------------------------------

class TestMisTokenizationIsLoud:
    def test_grep_no_path_does_not_return_silent_zero(self, tmp_path: Path) -> None:
        """`grep:A::CONST` eats `CONST` as the path. Never a bare 0-results."""
        out = supertool.dispatch("grep:A::CONST")
        assert "ERROR" in out
        assert "0 results" not in out

    def test_grep_mistokenized_error_names_the_colon_split(self) -> None:
        out = supertool.dispatch("grep:A::CONST")
        assert "':'" in out or "colon" in out.lower()

    def test_grep_mistokenized_error_points_at_the_escape(self) -> None:
        """The fix must be findable FROM the error, not only from the docs."""
        out = supertool.dispatch("grep:A::CONST")
        assert "grep:@" in out

    def test_grep_mistokenized_error_shows_the_pattern_it_actually_used(self) -> None:
        out = supertool.dispatch("grep:key: value")
        assert "key:" in out

    def test_plain_missing_path_error_stays_unadorned(self, tmp_path: Path) -> None:
        """No colon in the pattern => no colon advice. Don't cry wolf."""
        out = supertool.dispatch(f"grep:alpha:{tmp_path}/nope.txt")
        assert "ERROR" in out
        assert "grep:@" not in out

    def test_around_mistokenized_error_points_at_the_escape(self) -> None:
        out = supertool.dispatch("around:A::CONST")
        assert "ERROR" in out
        assert "around:@" in out

    def test_between_re_colon_in_pattern_errors_with_advice(self, hay: Path) -> None:
        """`between:re:START:END:PATH` rejoins rightward: a colon in START or
        END steals from the path. It must say so."""
        out = supertool.dispatch(f"between:re:Element: <:beta:{hay}")
        assert "ERROR" in out
        assert "between:@" in out


# ---------------------------------------------------------------------------
# 3. The @payload route round-trips a colon-bearing pattern
# ---------------------------------------------------------------------------

class TestGrepAtPayload:
    def test_grep_payload_finds_colon_pattern(self, tmp_path: Path, hay: Path) -> None:
        spec = _payload(tmp_path, "g.json", {
            "pattern": "Element: <", "path": str(hay),
            "limit": 8, "context": 0, "no_auto_read": True,
        })
        out = supertool.dispatch(f"grep:@{spec}")
        assert "Element: <div>" in out
        assert "1 results" in out

    def test_grep_payload_pattern_is_not_re_tokenized(self, tmp_path: Path, hay: Path) -> None:
        """A payload pattern with an internal ':' must be used verbatim —
        if it were re-split, this would match `A` or error on a path."""
        spec = _payload(tmp_path, "g.json", {
            "pattern": "A::CONST", "path": str(hay), "no_auto_read": True,
        })
        out = supertool.dispatch(f"grep:@{spec}")
        assert "A::CONST used here" in out

    def test_grep_payload_without_path_defaults_to_cwd_not_to_limit(
        self, tmp_path: Path, hay: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The colon-CLI trap: `limit` must never be mistaken for the path."""
        monkeypatch.chdir(tmp_path)
        spec = _payload(tmp_path, "g.json", {
            "pattern": "Element: <", "limit": 5, "no_auto_read": True,
        })
        out = supertool.dispatch(f"grep:@{spec}")
        assert "ERROR" not in out
        assert "Element: <div>" in out

    def test_grep_payload_count_mode(self, tmp_path: Path, hay: Path) -> None:
        spec = _payload(tmp_path, "g.json", {
            "pattern": "Element: <", "path": str(hay), "count": True,
        })
        out = supertool.dispatch(f"grep:@{spec}")
        assert "1" in out
        assert "Element: <div>" not in out

    def test_grep_payload_respects_context(self, tmp_path: Path, hay: Path) -> None:
        spec = _payload(tmp_path, "g.json", {
            "pattern": "ERROR: boom", "path": str(hay),
            "context": 1, "no_auto_read": True,
        })
        out = supertool.dispatch(f"grep:@{spec}")
        assert "A::CONST used here" in out   # the line before
        assert "passed" in out               # the line after

    def test_grep_payload_missing_pattern_errors(self, tmp_path: Path, hay: Path) -> None:
        spec = _payload(tmp_path, "g.json", {"path": str(hay)})
        out = supertool.dispatch(f"grep:@{spec}")
        assert "ERROR" in out
        assert "pattern" in out

    def test_grep_payload_rejects_extra_colon_args(self, tmp_path: Path) -> None:
        spec = _payload(tmp_path, "g.json", {"pattern": "x"})
        out = supertool.dispatch(f"grep:@{spec}:10")
        assert "ERROR" in out

    def test_grep_payload_from_stdin(
        self, hay: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import io
        body = (
            "pattern = '''Element: <'''\n"
            f'path = {json.dumps(str(hay))}\n'
            "no_auto_read = true\n"
        )
        monkeypatch.setattr("sys.stdin", io.StringIO(body))
        out = supertool.dispatch("grep:@-")
        assert "Element: <div>" in out


class TestAroundAtPayload:
    def test_around_payload_finds_colon_pattern(self, tmp_path: Path, hay: Path) -> None:
        spec = _payload(tmp_path, "a.json", {
            "pattern": "Element: <", "path": str(hay), "n": 1,
        })
        out = supertool.dispatch(f"around:@{spec}")
        assert "Element: <div>" in out
        assert "alpha" in out

    def test_around_payload_missing_pattern_errors(self, tmp_path: Path, hay: Path) -> None:
        spec = _payload(tmp_path, "a.json", {"path": str(hay)})
        out = supertool.dispatch(f"around:@{spec}")
        assert "ERROR" in out
        assert "pattern" in out


class TestGrepAroundAtPayload:
    def test_grep_around_payload_finds_colon_pattern(self, tmp_path: Path, hay: Path) -> None:
        spec = _payload(tmp_path, "ga.json", {
            "pattern": "Element: <", "path": str(hay), "n": 1, "limit": 5,
        })
        out = supertool.dispatch(f"grep_around:@{spec}")
        assert "Element: <div>" in out


class TestBetweenAtPayload:
    def test_between_payload_pattern_mode_with_colons(self, tmp_path: Path, hay: Path) -> None:
        spec = _payload(tmp_path, "b.json", {
            "start": "Element: <", "end": "ERROR: boom", "path": str(hay),
        })
        out = supertool.dispatch(f"between:@{spec}")
        assert "Element: <div>" in out
        assert "A::CONST used here" in out

    def test_between_payload_symbol_mode_extracts_both_fields(self, tmp_path: Path) -> None:
        """Pins the thing #625 changed — routing and field extraction — on every
        platform, with no optional dependency.

        `op_between_symbol` checks symbol, then path, and only *then* reaches
        for tree-sitter. Pointing a well-formed payload at a path that does not
        exist therefore lands on a deterministic error that echoes the path
        back, which is only reachable if BOTH fields survived the payload:
        a dropped route gives "between requires SYMBOL:PATH", a dropped symbol
        gives "empty symbol", a dropped path gives "empty path".
        """
        missing = tmp_path / "no_such_module.py"
        spec = _payload(tmp_path, "b.json", {"symbol": "foo", "path": str(missing)})
        out = supertool.dispatch(f"between:@{spec}")
        assert "file not found" in out
        assert str(missing) in out
        assert "requires SYMBOL:PATH" not in out
        assert "empty symbol" not in out
        assert "empty path" not in out

    def test_between_payload_symbol_mode_reaches_the_resolver(
        self, tmp_path: Path, enable_tree_sitter
    ) -> None:
        """A valid symbol payload must reach the symbol resolver on every
        platform — and either resolve, or DECLINE by name.

        The decline is not a failure: `between` symbol mode without tree-sitter
        states the missing package and points at `between:re`, which is the
        three-state contract working. What must never happen is the payload
        failing to route, so those signatures are excluded explicitly rather
        than the assertion being broadened to accept anything.
        """
        src = tmp_path / "m.py"
        src.write_text("def foo():\n    return 1\n\n\ndef bar():\n    return 2\n", encoding="utf-8")
        spec = _payload(tmp_path, "b.json", {"symbol": "foo", "path": str(src)})
        out = supertool.dispatch(f"between:@{spec}")
        resolved = "return 1" in out and "return 2" not in out
        declined = "requires tree-sitter" in out and "between:re" in out
        assert resolved or declined, out
        assert "requires SYMBOL:PATH" not in out
        assert "unknown field" not in out
        assert "needs 'symbol'" not in out

    @pytest.mark.skipif(
        not _HAS_TREE_SITTER,
        reason="tree-sitter language pack not installed — symbol mode declines "
               "by design; routing is pinned by the two tests above",
    )
    def test_between_payload_symbol_mode_returns_the_symbol(
        self, tmp_path: Path, enable_tree_sitter
    ) -> None:
        src = tmp_path / "m.py"
        src.write_text("def foo():\n    return 1\n\n\ndef bar():\n    return 2\n", encoding="utf-8")
        spec = _payload(tmp_path, "b.json", {"symbol": "foo", "path": str(src)})
        out = supertool.dispatch(f"between:@{spec}")
        assert "return 1" in out
        assert "return 2" not in out

    def test_between_payload_needs_symbol_or_start_end(self, tmp_path: Path, hay: Path) -> None:
        spec = _payload(tmp_path, "b.json", {"path": str(hay)})
        out = supertool.dispatch(f"between:@{spec}")
        assert "ERROR" in out


class TestReadInlineGrepAtPayload:
    def test_read_payload_inline_grep_with_colon(self, tmp_path: Path, hay: Path) -> None:
        spec = _payload(tmp_path, "r.json", {"path": str(hay), "grep": "Element: <"})
        out = supertool.dispatch(f"read:@{spec}")
        assert "Element: <div>" in out
        assert "alpha" not in out


# ---------------------------------------------------------------------------
# 4. Discoverability — the escape must be reachable from `ops` / `help`
# ---------------------------------------------------------------------------

class TestDiscoverability:
    """`ops` / `help:OP` render from the shipped config, so that is what has to
    carry the escape. Asserted against the file rather than through dispatch
    because the test run has no config on disk."""

    @pytest.mark.parametrize(
        "config", [".supertool.json", ".supertool.example.json"]
    )
    @pytest.mark.parametrize("op", ["grep", "around", "grep_around", "between", "read"])
    def test_op_metadata_mentions_the_payload_escape(self, op: str, config: str) -> None:
        root = Path(supertool.__file__).resolve().parent
        meta = json.loads((root / config).read_text(encoding="utf-8"))
        entry = meta["builtin-ops"][op]
        assert f"{op}:@" in entry["description"], (
            f"{config}:{op} must point at the @payload escape — the error "
            f"message and the ops listing are the only places a caller looks"
        )

    def test_readme_documents_the_read_op_payload(self) -> None:
        root = Path(supertool.__file__).resolve().parent
        readme = (root / "README.md").read_text(encoding="utf-8")
        assert "grep:@" in readme
