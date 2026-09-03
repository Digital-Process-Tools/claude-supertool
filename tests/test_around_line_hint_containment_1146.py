"""The `around_line:` delegation hint must not advertise a call dispatch refuses (#1146).

`_op_around`'s int-path branch (#734) builds

    Did you mean: around_line:{pattern}:{path}[:N]

straight from the raw `pattern` string, with no containment check. The colon
route never reaches this branch with an out-of-bounds `pattern` -- dispatch's
own `_PATH_ARG_POSITIONS` gate refuses `around:/etc/hosts:3` before `op_around`
ever runs. The `@payload`/`batch` route does not delegate that gate onto the
`pattern` field (it is a regex, not a declared path there), so it reaches this
branch with `pattern="/etc/hosts"` and happily prints a suggestion that running
it -- on the colon route, the very thing being suggested -- gets refused for
containment. Same defect class as the `git-checkout:<branch>` hint in #850: a
prescribed command the tool's own sibling refuses.

`_swap_suggest` (#1711) already gates its own candidate through `_gate_paths`
before offering it; this is the one remaining call site with no such gate.

Two things have to hold for the containment check under test to actually
run: `SUPERTOOL_ALLOW_OUTSIDE_CWD=1` is on for the whole suite (conftest.py,
its own documented escape hatch is `monkeypatch.delenv`), AND cwd must not be
this repo's own checkout -- `.supertool.json` here separately sets
`"allow_outside_cwd": true`, which the env var is not the only route to.
`monkeypatch.chdir(tmp_path)` clears the second; `tmp_path` carries no config.
"""
from __future__ import annotations

import io
import json

import supertool


def test_around_line_hint_omits_pattern_outside_containment(
    tmp_path, monkeypatch
) -> None:
    """The exact repro from #1146: pattern is an absolute path outside cwd."""
    monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)
    monkeypatch.chdir(tmp_path)
    out = supertool.op_around("/etc/hosts", "3")
    assert "around_line:/etc/hosts:3" not in out, out


def test_around_line_hint_still_fires_when_pattern_is_contained(
    tmp_path, monkeypatch
) -> None:
    """The suggestion is not simply deleted -- it still fires on the shape
    #734 was filed for, where nothing crosses the containment boundary."""
    monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "presets" / "gitlab").mkdir(parents=True)
    (tmp_path / "presets" / "gitlab" / "mr.py").write_text("x = 1\n")
    out = supertool.op_around("presets/gitlab/mr.py", "681")
    assert "around_line:presets/gitlab/mr.py:681" in out, out


def test_around_line_hint_via_payload_route_omits_uncontained_pattern(
    tmp_path, monkeypatch
) -> None:
    """The actual #1146 repro shape: the batch/payload route, which never
    applies containment to the `pattern` field the way the colon route does
    to its PATH slot."""
    monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)
    monkeypatch.chdir(tmp_path)
    payload = json.dumps({"pattern": "/etc/hosts", "path": "3"})
    monkeypatch.setattr(supertool.sys, "stdin", io.StringIO(payload))
    out = supertool.dispatch("around:@-")
    assert "around_line:/etc/hosts:3" not in out, out
