"""#1387 — the batch ops could not eat their own producers' output.

`gh-find-starable` and `gh-find-followable` write one candidate per line with an
inline annotation, because that is what makes the list reviewable:

    octo/tool  # 12 stars, a description
    octocat    # stargazer of X

Neither `gh-batch-star` nor `gh-batch-follow` stripped it, so
`octo/tool  # 12 desc` went out as the repository path and `octocat  # stargazer`
as the login. The two ops are each other's obvious pipeline and it did not
connect without a hand edit.

**The contract, which is the decision the issue asked for.** A `#` **preceded by
whitespace** ends the name and starts an annotation; the rest of the line is
dropped. A `#` with no whitespace before it is *not* a comment — `foo#bar` stays
`foo#bar` and is then refused, because no GitHub owner, repository or login may
contain one, so a line that still holds a `#` after the split was never a name.

The refusal is the half that matters. "A consumer that silently strips whatever
it does not understand is how a wrong name becomes a starred repo" — so nothing
here guesses: the value after the split must contain no whitespace and no `#`,
or the line is skipped by name, counted, and reported. And the count of lines
whose annotation was dropped is disclosed on the receipt, because a batch op
that silently reinterprets its input is this repository's house defect with a
write attached.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


candidates = _load("presets/github/_candidates.py", "gh_candidates_1387")
batch_star = _load("presets/github/batch_star.py", "gh_batch_star_1387")
batch_follow = _load("presets/github/batch_follow.py", "gh_batch_follow_1387")


def _run(monkeypatch: Any, mod: Any, attr: str, text: str,
         tmp_path: Path) -> tuple[list[str], int]:
    """`(targets, exit code)` for one candidates file. stdout via capsys."""
    seen: list[str] = []

    def _fake(name: str) -> tuple[bool, str]:
        seen.append(name)
        return True, "ok"

    monkeypatch.setattr(mod, attr, _fake)
    monkeypatch.setattr(mod, "time", types.SimpleNamespace(sleep=lambda _: None))
    path = tmp_path / "candidates.txt"
    path.write_text(text, encoding="utf-8")
    return seen, mod.main(str(path))


# ---------------------------------------------------------------------------
# the split, on its own
# ---------------------------------------------------------------------------

class TestSplit:

    def test_a_whitespace_preceded_hash_ends_the_name(self) -> None:
        assert candidates.split_annotation("octo/tool  # 12 desc") == (
            "octo/tool", "# 12 desc")

    def test_a_tab_counts_as_whitespace(self) -> None:
        assert candidates.split_annotation("octocat\t# stargazer") == (
            "octocat", "# stargazer")

    def test_a_line_with_no_annotation_is_unchanged_and_says_so(self) -> None:
        assert candidates.split_annotation("octo/tool") == ("octo/tool", "")

    def test_a_hash_with_no_space_before_it_is_not_an_annotation(self) -> None:
        """`foo#bar` is not a name with a comment — it is not a name at all.

        Splitting it would hand `foo` to the API, which is a *different*
        account that may well exist. The line is kept whole so the guard
        below refuses it.
        """
        assert candidates.split_annotation("evil#tool") == ("evil#tool", "")

    def test_the_first_annotating_hash_wins(self) -> None:
        assert candidates.split_annotation("a/b # one # two") == (
            "a/b", "# one # two")


class TestGuard:

    @pytest.mark.parametrize("value,token", [
        ("", "empty"),
        ("evil#tool", "#"),
        ("octo/tool 12", "whitespace"),
    ])
    def test_a_value_that_cannot_be_a_github_name_is_named(
            self, value: str, token: str) -> None:
        why = candidates.unusable(value)
        assert why, f"{value!r} was accepted as a name"
        assert token in why, why

    @pytest.mark.parametrize("value", [
        "octo/tool", "octocat", "a-b.c/d_e.f", "Octo/Tool-2",
    ])
    def test_an_ordinary_name_is_not_refused(self, value: str) -> None:
        assert candidates.unusable(value) == ""


# ---------------------------------------------------------------------------
# the pipeline the issue is about
# ---------------------------------------------------------------------------

PRODUCED_STAR = (
    "# 2 repos for topic 'cli' (sorted by stars)\n"
    "# Review, delete those you do not want, then:\n"
    "\n"
    "octo/tool  # 12  a small tool\n"
    "other/thing  # 4  something else\n"
)

PRODUCED_FOLLOW = (
    "# 2 candidates from octo/tool (stargazers + contributors)\n"
    "\n"
    "octocat  # stargazer of octo/tool\n"
    "hubber  # contributor to octo/tool\n"
)


def test_batch_star_eats_gh_find_starable_output(
        monkeypatch: Any, capsys: Any, tmp_path: Path) -> None:
    seen, code = _run(monkeypatch, batch_star, "star", PRODUCED_STAR, tmp_path)
    out = capsys.readouterr().out
    assert seen == ["octo/tool", "other/thing"], out
    assert code == 0, out


def test_batch_follow_eats_gh_find_followable_output(
        monkeypatch: Any, capsys: Any, tmp_path: Path) -> None:
    seen, code = _run(monkeypatch, batch_follow, "follow", PRODUCED_FOLLOW,
                      tmp_path)
    out = capsys.readouterr().out
    assert seen == ["octocat", "hubber"], out
    assert code == 0, out


@pytest.mark.parametrize("mod,attr,text", [
    ("star", "star", "octo/tool  # 12  a small tool\n"),
    ("follow", "follow", "octocat  # stargazer\n"),
])
def test_the_receipt_says_how_many_annotations_it_dropped(
        monkeypatch: Any, capsys: Any, tmp_path: Path,
        mod: str, attr: str, text: str) -> None:
    """Silent reinterpretation is the defect; the count is the disclosure."""
    target = batch_star if mod == "star" else batch_follow
    _run(monkeypatch, target, attr, text, tmp_path)
    out = capsys.readouterr().out
    assert "annotation" in out, out
    assert "1" in out, out


@pytest.mark.parametrize("mod,attr", [("star", "star"), ("follow", "follow")])
def test_no_disclosure_when_nothing_was_rewritten(
        monkeypatch: Any, capsys: Any, tmp_path: Path,
        mod: str, attr: str) -> None:
    """A qualifier on every run is one nobody reads on the run that matters."""
    target = batch_star if mod == "star" else batch_follow
    text = "octo/tool\n" if mod == "star" else "octocat\n"
    _run(monkeypatch, target, attr, text, tmp_path)
    assert "annotation" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# what must NOT be stripped
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mod,attr,line", [
    ("star", "star", "evil#tool/x"),
    ("follow", "follow", "evil#tool"),
])
def test_a_name_holding_a_hash_is_refused_not_trimmed(
        monkeypatch: Any, capsys: Any, tmp_path: Path,
        mod: str, attr: str, line: str) -> None:
    """The whole reason the split requires whitespace.

    `evil#tool/x` trimmed at the `#` is `evil`, a different account. Nothing is
    sent; the line is named and counted.
    """
    target = batch_star if mod == "star" else batch_follow
    seen, code = _run(monkeypatch, target, attr, line + "\n", tmp_path)
    out = capsys.readouterr().out
    assert seen == [], f"a refused line was sent anyway: {seen!r}"
    assert code == 2, out


@pytest.mark.parametrize("mod,attr", [("star", "star"), ("follow", "follow")])
def test_a_skipped_line_is_counted_on_the_receipt(
        monkeypatch: Any, capsys: Any, tmp_path: Path,
        mod: str, attr: str) -> None:
    """A line that vanishes between the file and the run is an absence the tool made."""
    target = batch_star if mod == "star" else batch_follow
    good = "octo/tool\n" if mod == "star" else "octocat\n"
    seen, code = _run(monkeypatch, target, attr, good + "evil#x/y\n", tmp_path)
    out = capsys.readouterr().out
    assert len(seen) == 1, out
    assert "1 skipped" in out, out


@pytest.mark.parametrize("mod,attr", [("star", "star"), ("follow", "follow")])
def test_a_full_line_comment_is_still_a_comment(
        monkeypatch: Any, capsys: Any, tmp_path: Path,
        mod: str, attr: str) -> None:
    target = batch_star if mod == "star" else batch_follow
    good = "octo/tool\n" if mod == "star" else "octocat\n"
    seen, code = _run(monkeypatch, target, attr,
                      "  # a leading-space comment\n" + good, tmp_path)
    out = capsys.readouterr().out
    assert len(seen) == 1, out
    assert "skipped" not in out, out
