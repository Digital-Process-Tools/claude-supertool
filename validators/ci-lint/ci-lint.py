#!/usr/bin/env python3
"""ci-lint validator adapter -- `glab ci lint` against the resolved GitLab CI
root config. Emits SCHEMA.md JSON. #1797.

Why this one and not yamllint: the failure that costs a round-trip is almost
never a parse error -- `yaml-check` and `prettier-check` both catch those. It
is a misspelled key, a stage that does not exist, an `extends` pointing at a
template that does not define what the job assumes -- all valid YAML, all
green under every validator that ships today. `glab ci lint` is the only
thing that can confirm GitLab would accept a merged CI config.

Trap 1 -- linting the edited file is wrong: an include is not a standalone
document and never validates alone, so this adapter is always invoked
against the resolved ROOT config (see `resolve_root.py`, wired as the
`resolve` key in this validator's `.supertool.json` spec), never against the
include a caller actually edited. Whole-config coverage is the upside: any
edit to any include gets validated against the merged result.

Trap 2 -- a network failure must not read as an invalid config: `glab ci
lint` posts to the GitLab instance and needs auth. Offline, VPN down, an
instance hung -- every one of those must not become `ok: false`, because
`rollback_on_fail: true` would then silently revert a genuinely correct edit
during an outage, which is a worse failure than having no validator running
at all. So this adapter separates three outcomes rather than two:

    1. config invalid       -> ok: false, roll back
    2. config valid         -> ok: true
    3. could not reach/auth -> skipped, never a verdict

`glab` prints a real validation failure as "<file> is invalid." followed by
per-job detail (confirmed against the shipped `glab` binary's own compiled
string table: "%s is invalid." and "CI/CD YAML is valid!" are the two
literal formats it emits for this command). Every other non-zero exit --
"You must be in a GitLab project repository...", an HTTP status from the
lint endpoint itself (401/403/404/5xx), a bare connection failure -- is glab
declining to answer the question at all, and lands in the third state.

Usage: ci-lint.py <resolved-root-config-file>

Env vars:
  GLAB_BIN  glab binary (default: glab)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from refusal import absent, guard_main
from bin_resolve import resolve_bin_cmd

TOOL = "ci-lint"

# The two literal formats `glab ci lint` emits for THIS command, confirmed
# against the shipped binary's own string table -- never guessed at, since a
# guessed marker is exactly the kind of claim this adapter exists to avoid
# making about a file it never actually parsed.
#
# `_INVALID_MARKER` alone is a bare substring, and glab's own compiled format
# is `"%s is invalid."` -- the `%s` half is the file, not decoration. Checking
# the tail alone would fold Trap 2 (a network/auth failure that must stay
# `skipped`) back into a false `ok: false`: an expired-token message plausibly
# contains the words "is invalid." on their own (e.g. "your personal access
# token is invalid."), which would roll back a correct edit exactly the way
# the issue's own Trap 2 warns against. `_invalid_marker_for(file)` ties the
# match to THIS file's own name, which an unrelated auth message has no
# reason to contain.
_VALID_MARKER = "is valid"


def _invalid_marker_for(file: str) -> str:
    return f"{os.path.basename(file).lower()} is invalid."

#: `count` is always exactly 1 on a real finding (`glab` reports the whole
#: config's validity as one verdict, never a per-line list) and 1 on every
#: `code: "adapter"` decline -- never a count that excludes adapter rows or
#: truncates a longer list, so both keys are the simplest they can be
#: (validators/SCHEMA.md, tests/test_count_basis_contract_1728.py).
COUNT_CONTRACT = {"count_basis": "measured", "errors_truncated": False}


def emit(obj: dict) -> None:
    print(json.dumps(obj))


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": TOOL, "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": 0, **COUNT_CONTRACT})
        return

    file = sys.argv[1]
    glab_bin_cmd_str = os.environ.get("GLAB_BIN", "glab")
    # Accept either a single binary path (may contain a space, e.g. the
    # default Windows install location "C:\\Program Files\\glab\\glab.exe")
    # or a shlex-quoted command line. Cross-platform test stubs pass e.g.
    # "python /path/stub.py" (each token shlex.quote'd) so the stub runs on
    # Windows too (no #!/usr/bin/env bash dependency). resolve_bin_cmd()
    # tries the whole string as one path first, and only falls back to
    # shlex.split when that does not resolve to a real executable (#2176).
    bin_cmd = resolve_bin_cmd(glab_bin_cmd_str, "glab")
    glab_bin = bin_cmd[0]

    if not shutil.which(glab_bin) and not (
        os.path.isfile(glab_bin) and os.access(glab_bin, os.X_OK)
    ):
        emit(absent(TOOL, file, f"GLAB_BIN not found: {glab_bin}", 0))
        return

    if not os.path.isfile(file):
        emit({"tool": TOOL, "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "file not found"}],
              "duration_ms": 0, **COUNT_CONTRACT})
        return

    start = time.time()
    try:
        r = subprocess.run([*bin_cmd, "ci", "lint", file], capture_output=True,
                            text=True, timeout=30, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        # A hung network call is Trap 2, not a parse verdict -- `glab` never
        # answered the question, so this is a skip, not an `ok: false`.
        emit({"tool": TOOL, "file": file,
              "duration_ms": int((time.time() - start) * 1000),
              "skipped": "glab ci lint timed out after 30s -- could not "
                         "confirm GitLab would accept this config"})
        return
    except OSError as e:
        emit(absent(TOOL, file, str(e), int((time.time() - start) * 1000)))
        return

    dur = int((time.time() - start) * 1000)
    combined = (r.stdout + "\n" + r.stderr).strip()
    low = combined.lower()

    if r.returncode == 0 and _VALID_MARKER in low:
        emit({"tool": TOOL, "file": file, "ok": True, "count": 0,
              "errors": [], "duration_ms": dur, **COUNT_CONTRACT})
        return

    if _invalid_marker_for(file) in low:
        msg = combined.strip()[:500]
        emit({"tool": TOOL, "file": file, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": TOOL, "msg": msg}],
              "duration_ms": dur, **COUNT_CONTRACT})
        return

    # Everything else -- repo/host resolution failure, an HTTP status off
    # the lint endpoint itself, "glab auth login", a bare connection error --
    # is glab declining to answer the question, never a finding about the
    # file. Trap 2: this must be the third state, not `ok: false`.
    reason = combined.strip()[:500] or f"glab ci lint exited {r.returncode} " \
        "with no output"
    emit({"tool": TOOL, "file": file, "duration_ms": dur,
          "skipped": f"could not confirm this config with GitLab: {reason}"})


if __name__ == "__main__":
    guard_main(TOOL, main)
