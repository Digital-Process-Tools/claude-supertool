"""A skip a CI job can promise to turn into a failure (#557).

`pytest.mark.skipif(shutil.which("bun") is None, ...)` is the right default. A
contributor without bun should not see a red suite for code they did not touch.

But it makes a job that *does* install bun unable to prove anything. If the
install step half-fails, the tests skip, the leg is green, and the board reports
exactly what it reports for a fully exercised change — which is #557 in one
sentence: an absence produced by the tool, read as an absence in the world. The
two `notifiers/claude-channel/` test files were in that state on twelve legs;
`channel.ts` had never had a line executed by CI.

So a caller states both things: whether the prerequisite is there, and whether
something *promised* it would be. Promise kept and prerequisite present: run.
No promise and prerequisite absent: skip, with the reason visible. Promise made
and prerequisite absent: raise at import, which pytest reports as a collection
error and the job goes red. That last case is the one worth having — it is the
difference between a job that can fail and a job that cannot, and only the
second kind is worth adding to a board.

The promise is deliberately not inferred from `CI`. Eleven of the twelve pytest
legs are on CI and install no JS runtime at all; making them promise a toolchain
they were never going to have would turn the whole board red to fix a reporting
defect. The promise belongs to the one job that does the install, and it says so
with `SUPERTOOL_REQUIRE_JS=1`.
"""
from __future__ import annotations

import os
import shutil

import pytest

#: Set by the `notifiers` job in `.github/workflows/tests.yml`, two steps after
#: it installs bun and the channel's `node_modules`. Set nowhere else.
REQUIRE_JS_ENV = "SUPERTOOL_REQUIRE_JS"


class ToolchainPromiseBroken(RuntimeError):
    """A job said a prerequisite would be present and it is not.

    Raised at module import so pytest reports a collection error. Skipping would
    report a pass for tests that never ran, which is the defect, not the fix.
    """


def js_promised() -> bool:
    """True when a job has taken responsibility for the JS toolchain."""
    return os.environ.get(REQUIRE_JS_ENV) == "1"


def posix_ci_promised() -> bool:
    """True on a POSIX CI runner, where `bash` is a given rather than a hope.

    Windows is excluded on purpose and not out of caution. `bash -n` answers a
    question about syntax, which is platform-independent, so checking the shell
    files on ubuntu and macOS is complete coverage rather than partial — while
    whether Git Bash happens to be on `PATH` for a pytest subprocess on
    `windows-latest` is a property of the runner image that would make the board
    red for a reason no contributor could act on.
    """
    return os.environ.get("CI") == "true" and os.name == "posix"


def require_or_skip(available: bool, reason: str, *,
                    promised: bool) -> pytest.MarkDecorator:
    """`skipif` when nobody promised, a hard error when somebody did.

    Returns a mark either way so it drops straight into a `pytestmark` list.
    """
    if available:
        return pytest.mark.skipif(False, reason=reason)
    if promised:
        raise ToolchainPromiseBroken(
            f"a CI job promised this prerequisite and it is absent: {reason}. "
            "Either its install step failed without failing the job, or the "
            "promise is set somewhere that never installs anything. Skipping "
            "here would report a pass for tests that did not run."
        )
    return pytest.mark.skipif(True, reason=reason)


def which(tool: str) -> str | None:
    """`shutil.which`, re-exported so a caller needs one import, not two."""
    return shutil.which(tool)
