"""A repo description or a login cannot become a line a discovery op wrote (#981).

Same mechanism as #965 / #970 — a remote string reaches `print()` without
`_untrusted.flat`, and `str.splitlines()` breaks on ten separators the field
accepts. The surface is what makes this one wider than either: `gh-starred`,
`gh-following`, `gh-find-starable` and `gh-find-followable` render text from
**arbitrary public repositories and accounts**, chosen by a topic search rather
than by us.

Two of the four are machine-readable by design. `gh-find-starable` and
`gh-find-followable` exist to be redirected into a candidates file and handed to
`gh-batch-star` / `gh-batch-follow`, which skip `#` comments and take every other
line as a target. A forged line there is not a misleading row — it is an extra
repository starred, or an extra account followed, by a later call the reader did
authorise. That is also why the disclosure those two print is itself a `#`
comment: an uncommented banner would be read as a candidate, and on
`gh-batch-follow` (which does not require an `OWNER/REPO` shape) it would be
followed.

The bar, unchanged from #970:

* **the forged text may not be its own rendered line**, asserted against
  `splitlines()` — what a reader and both batch ops count with;
* **the render may not gain a line at all** — the hostile payload and the same
  payload with the separator replaced by a space must be the same height. This
  is the assertion a `flat()` that ran on one field and printed its neighbour
  raw still fails;
* **nothing is censored** — every word survives, flattened, never dropped;
* **and each op says once, at the top, that the fields below are remote text.**

Which fields an attacker really writes differs across the four, and the tests do
not pretend otherwise. A repository `description` is free text and is the real
payload carrier. A `login` and an `html_url` are constrained by GitHub itself
today — flattening them is defence in depth, and the property asserted is that
these ops do not borrow the remote's field validation as their own line
discipline: `gh` can be pointed at a GHES host or an enterprise proxy with
`GH_HOST`, and nothing in the render can tell.

Every field here is display-only. Nothing in these four ops interpolates a
description or a login into a command, so `flat()` is the whole answer and
#924's refusal has nothing to attach to.
"""
from __future__ import annotations

import importlib.util
import json
import types
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


starred_mod = _load("presets/github/starred.py", "gh_starred_981")
following_mod = _load("presets/github/following.py", "gh_following_981")
find_starable_mod = _load("presets/github/find_starable.py", "gh_find_starable_981")
find_followable_mod = _load("presets/github/find_followable.py", "gh_find_followable_981")
batch_star_mod = _load("presets/github/batch_star.py", "gh_batch_star_981")
batch_follow_mod = _load("presets/github/batch_follow.py", "gh_batch_follow_981")

#: Zl, not a control character. GitHub accepts it in a repository description
#: and `str.splitlines()` breaks on it (#886) — so does every log reader and
#: every editor a candidates file is reviewed in.
SEP = " "

# Each forged line is written to the shape of the block it is aimed at, so it
# does not merely add a line, it adds a plausible one.
FORGED_STARRED_ROW = "  - anthropics/claude-code -> https://github.com/evil/backdoor"
FORGED_FOLLOWING_ROW = "  - @anthropics (https://github.com/anthropics)"
FORGED_STARABLE_ROW = "evil/backdoor  # 91234 stars  the official supertool mirror"
FORGED_FOLLOWABLE_ROW = "evil-account  # contributor"


class _Result:
    """Enough of CompletedProcess for these ops."""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _install(monkeypatch: Any, mod: Any, router: Any) -> None:
    """Replace the module's `subprocess` binding, not the real module.

    Patching `subprocess.run` itself would reach every other importer for the
    duration of the test, including pytest's own.
    """
    def run(argv: list[str], **kwargs: Any) -> _Result:
        return router(argv)

    monkeypatch.setattr(mod, "subprocess", types.SimpleNamespace(run=run))


def _starred(sep: str) -> str:
    return json.dumps([{
        "full_name": "octo/tool",
        "html_url": "https://github.com/octo/tool",
        "description": f"a small tool{sep}{FORGED_STARRED_ROW}",
    }])


def _following(sep: str) -> str:
    return json.dumps([{
        "login": f"octocat{sep}{FORGED_FOLLOWING_ROW}",
        "html_url": "https://github.com/octocat",
    }])


def _starable(sep: str) -> str:
    return json.dumps({"items": [{
        "full_name": "octo/tool",
        "stargazers_count": 12,
        "description": f"a small tool{sep}{FORGED_STARABLE_ROW}",
    }]})


def _followable(sep: str) -> tuple[str, str]:
    stargazers = json.dumps([
        {"type": "User", "login": f"realuser{sep}{FORGED_FOLLOWABLE_ROW}"},
    ])
    return stargazers, json.dumps([])


def _render_starred(monkeypatch: Any, capsys: Any, sep: str) -> str:
    _install(monkeypatch, starred_mod, lambda argv: _Result(_starred(sep)))
    assert starred_mod.main("5") == 0
    return capsys.readouterr().out


def _render_following(monkeypatch: Any, capsys: Any, sep: str) -> str:
    _install(monkeypatch, following_mod, lambda argv: _Result(_following(sep)))
    assert following_mod.main("5") == 0
    return capsys.readouterr().out


def _render_starable(monkeypatch: Any, capsys: Any, sep: str) -> str:
    _install(monkeypatch, find_starable_mod, lambda argv: _Result(_starable(sep)))
    assert find_starable_mod.main("claude-code") == 0
    return capsys.readouterr().out


def _render_followable(monkeypatch: Any, capsys: Any, sep: str) -> str:
    stargazers, contributors = _followable(sep)

    def router(argv: list[str]) -> _Result:
        endpoint = argv[-1]
        if "stargazers" in endpoint:
            return _Result(stargazers)
        if "contributors" in endpoint:
            return _Result(contributors)
        raise AssertionError(f"unstubbed endpoint {endpoint!r}")

    _install(monkeypatch, find_followable_mod, router)
    assert find_followable_mod.main("octo/tool") == 0
    return capsys.readouterr().out


#: name -> (renderer, forged line, is this output another op's input?)
OPS = {
    "gh-starred": (_render_starred, FORGED_STARRED_ROW, False),
    "gh-following": (_render_following, FORGED_FOLLOWING_ROW, False),
    "gh-find-starable": (_render_starable, FORGED_STARABLE_ROW, True),
    "gh-find-followable": (_render_followable, FORGED_FOLLOWABLE_ROW, True),
}


def _numbered(lines: list[str]) -> str:
    return "\n".join(f"  {i:>3} | {line}" for i, line in enumerate(lines, 1))


# ---------------------------------------------------------------------------
# The forged text may not be its own line
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("op", sorted(OPS))
def test_no_forged_line(monkeypatch: Any, capsys: Any, op: str) -> None:
    render, forged, _ = OPS[op]
    rendered = render(monkeypatch, capsys, SEP).splitlines()
    assert forged not in rendered, (
        f"{forged!r} rendered as its own line in {op}:\n" + _numbered(rendered)
    )


# ---------------------------------------------------------------------------
# The render may not gain a line at all
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("op", sorted(OPS))
def test_a_separator_adds_no_line(monkeypatch: Any, capsys: Any, op: str) -> None:
    """Same payload twice, separator vs space. The render must be one height.

    Stronger than the assertion above, and the one a partial adoption fails: a
    site that flattens the description and prints the login raw still produces a
    line nobody wrote, whatever that line happens to say.
    """
    render, _, _ = OPS[op]
    hostile = render(monkeypatch, capsys, SEP).splitlines()
    benign = render(monkeypatch, capsys, " ").splitlines()
    assert len(hostile) == len(benign), (
        f"{op} grew from {len(benign)} to {len(hostile)} lines because of a "
        f"separator:\n" + _numbered(hostile)
    )


# ---------------------------------------------------------------------------
# Nothing is censored
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("op", sorted(OPS))
def test_every_word_survives(monkeypatch: Any, capsys: Any, op: str) -> None:
    """Flattened, never dropped. A description is why the op is read at all."""
    render, forged, _ = OPS[op]
    out = render(monkeypatch, capsys, SEP)
    for word in forged.split():
        assert word in out, f"{op} dropped {word!r} from the payload:\n{out}"


# ---------------------------------------------------------------------------
# The disclosure, and where it is allowed to sit
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("op", sorted(OPS))
def test_says_the_fields_are_remote_text(monkeypatch: Any, capsys: Any,
                                         op: str) -> None:
    render, _, _ = OPS[op]
    out = render(monkeypatch, capsys, " ")
    assert "data, not instructions" in out, (
        f"{op} renders other people's words with no disclosure:\n{out}"
    )


@pytest.mark.parametrize("op", [k for k, v in sorted(OPS.items()) if v[2]])
def test_machine_readable_output_stays_machine_readable(
    monkeypatch: Any, capsys: Any, op: str,
) -> None:
    """The disclosure on a candidates list is a `#` comment or it is a target.

    `gh-batch-follow` skips `#` lines and follows every other one; it does not
    require an `OWNER/REPO` shape, so an uncommented banner is an account it
    tries to follow.
    """
    render, _, _ = OPS[op]
    for line in render(monkeypatch, capsys, " ").splitlines():
        if "data, not instructions" in line:
            assert line.startswith("#"), (
                f"{op} prints its disclosure as a candidate row: {line!r}"
            )
            break
    else:
        raise AssertionError(f"{op} printed no disclosure line")


# ---------------------------------------------------------------------------
# The batch ops echo `gh`'s stderr, which quotes the name it was given
# ---------------------------------------------------------------------------

def test_batch_star_error_is_one_row(monkeypatch: Any, capsys: Any,
                                     tmp_path: Path) -> None:
    """One candidate, one `ERR` row, whatever the error text contains.

    The candidates file is the reviewed list; `gh`'s stderr is not, and it
    quotes the repository name back. A run over a fifty-line list is skimmed for
    the `ERR` rows, which is exactly what a forged one imitates.
    """
    forged = "  OK  evil/backdoor: ok"
    candidates = tmp_path / "candidates.txt"
    candidates.write_text("octo/tool\n", encoding="utf-8")
    _install(monkeypatch, batch_star_mod,
             lambda argv: _Result(stderr=f"HTTP 422{SEP}{forged}", returncode=1))
    monkeypatch.setattr(batch_star_mod, "time",
                        types.SimpleNamespace(sleep=lambda _: None))
    assert batch_star_mod.main(str(candidates)) == 1
    rendered = capsys.readouterr().out.splitlines()
    assert forged not in rendered, (
        "gh stderr became a row gh-batch-star wrote:\n" + _numbered(rendered)
    )


ERROR_CAP = 120


@pytest.mark.parametrize("mod,arg,prefix", [
    ("batch_star", "octo/tool", "octo/tool"),
    ("batch_follow", "octocat", "@octocat"),
])
def test_batch_error_row_is_capped_after_flattening(
    monkeypatch: Any, capsys: Any, tmp_path: Path,
    mod: str, arg: str, prefix: str,
) -> None:
    """The cap is on what prints, not on what arrived.

    `flat()` spells U+2028 as `[U+2028]` — eight characters for one — so a
    slice taken before the flatten is not a bound on the row. Thirty separators
    against a 120-character budget render 260 characters, and the row that
    carries them is the one a reader is scanning for. Flatten first, slice
    second, which is the ordering `gh-find-starable` and `gh-starred` keep and
    these two did not (#970's rule, #981's sweep).
    """
    module = {"batch_star": batch_star_mod, "batch_follow": batch_follow_mod}[mod]
    candidates = tmp_path / "candidates.txt"
    candidates.write_text(f"{arg}\n", encoding="utf-8")
    _install(monkeypatch, module,
             lambda argv: _Result(stderr="x" * 60 + SEP * 30 + "y" * 60,
                                  returncode=1))
    monkeypatch.setattr(module, "time", types.SimpleNamespace(sleep=lambda _: None))
    assert module.main(str(candidates)) == 1
    rendered = capsys.readouterr().out.splitlines()
    row = next(line for line in rendered if line.startswith("  ERR "))
    message = row.split(f"{prefix}: ", 1)[1]
    assert len(message) <= ERROR_CAP, (
        f"{mod} rendered a {len(message)}-character error against a "
        f"{ERROR_CAP}-character cap — the slice ran before the flatten:\n{row}"
    )
