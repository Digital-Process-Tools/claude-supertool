"""#1315 — two registry-vs-code token disagreements found by the #1269
sweep, left out of PR #1313 because each needs a person to decide intent
rather than a mechanical fix.

Neither is settled here either. Both pins below record the CURRENT behaviour
as undecided-but-real, per #1315's own closing line: "if you cannot settle
one, say so and pin the current behaviour with a test that names it as
undecided rather than as intended." The reasons neither was settled in this
pass are recorded case by case.

## 1. `gl-pipeline:ID:full`

`presets/gitlab/pipeline.py` accepts `full` as a third segment (`_FILTERS =
{"full", "active", "failed", "traces"}`) and its own refusal message never
offers it ("use 'active', 'failed', or omit for the full board"). Fixing
either side of that (the refusal text, or `presets/gitlab.json`'s `syntax` /
`description`) means editing `presets/gitlab/pipeline.py` and/or
`presets/gitlab.json` — both held by a concurrent lane (#1796) at the time
this issue was worked, confirmed via `lane_setup.py --derive-held`. Editing a
file another live lane holds is exactly what this developer's brief forbids,
so this pins the current, contested behaviour instead of fixing it.

## 2. `mcp_stop:--all`

`presets/mcp/stop.py`'s `main()` treats `argv[1] == "--all"` as the
stop-everything mode regardless of which op invoked it — `mcp_stop:--all`
(from the `mcp_stop` op, whose declared syntax is `mcp_stop:SERVER_NAME`) and
`mcp_stop_all` (whose `cmd` hardcodes `--all`) produce byte-identical argv, so
nothing at the script boundary can tell them apart. A code fix that gates
`--all` behind an op-specific signal (e.g. an env var only `mcp_stop_all`'s
config would set) was drafted and abandoned: `tests/test_mcp_stop_pid_zero_569
.py::test_all_mode_is_guarded_too` calls `stop_mod.main(["stop.py", "--all"])`
directly and asserts the real stop-everything path runs — an existing,
intentional test of the shared script, in a file this lane's claim does not
cover. Gating the path would either break that test or need it rewritten
outside this lane's own claimed files. So this also pins current behaviour.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


def _load(relpath: str, name: str):
    path = REPO_ROOT / relpath
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_gl_pipeline_accepts_full_but_its_own_refusal_never_offers_it() -> None:
    """Pin, not endorse: `full` dispatches, and the refusal text a caller who
    guessed wrong actually reads never tells them it exists."""
    pipe = _load("presets/gitlab/pipeline.py", "gitlab_pipeline_1315")
    assert "full" in pipe._FILTERS
    assert "active" in pipe._FILTERS and "failed" in pipe._FILTERS

    # The refusal text is read straight from source rather than triggered via
    # main(), because triggering it needs a live `glab` call this test does
    # not want to make; the string itself is the thing under test.
    src = (REPO_ROOT / "presets/gitlab/pipeline.py").read_text(encoding="utf-8")
    assert "use 'active', 'failed', or omit for the full board" in src, (
        "the refusal text moved — re-check whether it now names 'full' "
        "explicitly, which would settle this half of #1315"
    )
    assert "'full'" not in src.split("use 'active'")[1].split("\n")[0], (
        "the refusal text now names 'full' explicitly — #1315 instance 1 "
        "may be settled; update this test and its docstring"
    )


def test_gitlab_json_syntax_still_omits_full_as_a_spellable_token() -> None:
    """The JSON manifest's own `syntax` string for `gl-pipeline` is the other
    surface of the same disagreement, and it is likewise unresolved here —
    `presets/gitlab.json` is contested with a concurrent lane."""
    import json
    raw = json.loads((REPO_ROOT / "presets/gitlab.json").read_text(encoding="utf-8"))
    syntax = raw["ops"]["gl-pipeline"]["syntax"]
    assert "full" not in syntax, (
        "presets/gitlab.json now names 'full' in gl-pipeline's syntax — "
        "#1315 instance 1 may be settled; update this test and its docstring"
    )


def test_mcp_stop_dash_dash_all_and_mcp_stop_all_reach_the_identical_code_path() -> None:
    """Pin, not endorse: the two declared ops for 'stop everything' converge
    on one script entrypoint that cannot tell which op it was reached
    through -- `mcp_stop:--all`'s argv and `mcp_stop_all`'s argv are the same
    list."""
    stop_mod = _load("presets/mcp/stop.py", "mcp_stop_1315")
    # Both ops' cmd templates ultimately hand stop.py this exact argv list.
    mcp_stop_argv = ["stop.py", "--all"]     # mcp_stop:--all  -- {args} = "--all"
    mcp_stop_all_argv = ["stop.py", "--all"]  # mcp_stop_all    -- cmd hardcodes it
    assert mcp_stop_argv == mcp_stop_all_argv
    # main() branches on argv[1] alone, before anything that could carry which
    # op name dispatched the call -- confirmed by reading the branch itself
    # rather than asserted by name, since that is the fact this test pins.
    import inspect
    src = inspect.getsource(stop_mod.main)
    assert 'argv[1] == "--all"' in src, (
        "main()'s --all branch condition changed -- re-check whether the two "
        "ops are still indistinguishable, which would settle #1315 instance 2"
    )


def test_mcp_stop_syntax_still_only_declares_server_name() -> None:
    """`mcp_stop`'s own manifest entry still promises `SERVER_NAME`, not
    `--all` -- the declared vocabulary and the reachable behaviour still
    disagree, which is #1315 instance 2 exactly."""
    import json
    raw = json.loads((REPO_ROOT / "presets/mcp.json").read_text(encoding="utf-8"))
    assert raw["ops"]["mcp_stop"]["syntax"] == "mcp_stop:SERVER_NAME"
    assert "mcp_stop_all" in raw["ops"], (
        "mcp_stop_all is the declared canonical op for stopping every daemon "
        "-- if this op is renamed or removed, #1315 instance 2 needs "
        "re-deciding, not just this test updating"
    )
