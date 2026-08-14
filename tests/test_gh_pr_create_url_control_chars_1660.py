"""#1660 - gh's stdout reaches a receipt supertool owns, and ESC survives.

`str.splitlines()` consumes every line SEPARATOR, so no line selected out of
`gh pr create`'s stdout can be forged - that half of #1652's argument holds
here. What it does not consume is ESC, and the selected line is printed at
column 0 as `URL:`, feeds `number`, and `number` reaches this op's own
`[result]` line. That is #851's question (closed by #853 for `check.py` and
`branch.py`) arriving at a site #851 did not cover.

The register entry in `tests/test_preset_twin_splitlines_register_1119.py` said
"the extracted value is printed, not parsed", which answers forgery and is
silent on control characters. Corrected in the same change.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

MOD_PATH = Path(__file__).parent.parent / "presets" / "github" / "pr_create.py"
_spec = importlib.util.spec_from_file_location("github_pr_create_1660", MOD_PATH)
assert _spec is not None and _spec.loader is not None
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

REPO = "Digital-Process-Tools/claude-supertool"
ESC = chr(27)

#: What a hostile or proxied `gh` can put on the line this op selects. `[2K`
#: erases the line the cursor is on and `[1A` moves it up one - together they
#: rub out the receipt line above, which is `PR:   #N  <title>`.
HOSTILE_URL = "https://github.com/o/r/pull/957" + ESC + "[1A" + ESC + "[2K"


class _Harness:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout

    def gh(self, args, timeout=30):
        if args[:2] == ["pr", "create"]:
            return subprocess.CompletedProcess(args, 0, self.stdout, "")
        raise AssertionError("unexpected gh call: %r" % (args,))

    def gh_json(self, args, timeout=30):
        if args[:2] == ["repo", "view"]:
            return ({"defaultBranchRef": {"name": "master"}}, "")
        if args[:2] == ["pr", "view"]:
            return ({"statusCheckRollup": [], "headRefOid": "a" * 40,
                     "createdAt": "2999-01-01T00:00:00Z"}, "")
        raise AssertionError("unexpected gh_json call: %r" % (args,))


def _run(monkeypatch, tmp_path, stdout: str) -> str:
    payload = tmp_path / "pr.json"
    payload.write_text(json.dumps(
        {"repo": REPO, "title": "a change", "base": "master",
         "body": "Closes #1660"}), encoding="utf-8")
    h = _Harness(stdout)
    monkeypatch.setattr(m, "_gh", h.gh)
    monkeypatch.setattr(m, "_gh_json", h.gh_json)
    monkeypatch.setattr(m, "_current_branch", lambda: ("fix/1660", ""))
    monkeypatch.setattr(sys, "argv", ["pr_create.py", str(payload)])
    return m.main()


def test_esc_in_ghs_url_line_never_reaches_the_receipt(monkeypatch, capsys,
                                                       tmp_path):
    """The whole of #1660: not one raw ESC anywhere in a receipt this op wrote."""
    assert _run(monkeypatch, tmp_path, HOSTILE_URL + chr(10)) == 0
    out = capsys.readouterr().out
    assert ESC not in out, (
        "a cursor-control sequence out of gh's stdout reached the receipt at "
        "column 0 (#1660): %r" % out)


def test_the_escape_is_disclosed_rather_than_stripped(monkeypatch, capsys,
                                                      tmp_path):
    """Removing the bytes silently would be the absence-produced-by-the-tool
    defect one layer along - the reader could not tell a mangled URL from a
    clean one. `_untrusted.flat` shows the character instead."""
    assert _run(monkeypatch, tmp_path, HOSTILE_URL + chr(10)) == 0
    out = capsys.readouterr().out
    assert ("␛" in out or "[U+001B]" in out), (
        "the ESC was dropped rather than disclosed: %r" % out)
    # The part of the URL that is real still reads as itself.
    assert "https://github.com/o/r/pull/957" in out


def test_the_number_derived_from_that_line_is_clean_too(monkeypatch, capsys,
                                                        tmp_path):
    """`number` is `url.rstrip("/").split("/")[-1]`, so it inherits whatever the
    URL carried - and it is printed four more times, including inside this op's
    own `[result]` verdict, which is the line #851 exists to protect."""
    assert _run(monkeypatch, tmp_path, HOSTILE_URL + chr(10)) == 0
    lines = capsys.readouterr().out.splitlines()
    result = [ln for ln in lines if ln.startswith("[result]")]
    assert result, lines
    assert ESC not in result[0], result[0]
    assert not any(ln.startswith("[1A") or ln.startswith("[2K")
                   for ln in lines), (
        "a receipt line begins with the tail of an escape sequence: %r" % lines)


def test_an_ordinary_url_is_left_exactly_as_it_came(monkeypatch, capsys,
                                                    tmp_path):
    """The guard must be invisible on the path everyone actually takes."""
    url = "https://github.com/%s/pull/1660" % REPO
    assert _run(monkeypatch, tmp_path, url + chr(10)) == 0
    out = capsys.readouterr().out
    assert ("URL:  " + url) in out
    assert "PR:   #1660" in out
