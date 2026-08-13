"""One decline decision for `read`'s git marker, read off the suffix (#705).

`_path_meta_suffix` asks git for a path's working-tree state under a 2s budget
and, when that query cannot answer, says so: the marker is
``supertool.PATH_META_UNKNOWN`` -- state unknown, not clean. That is the product
behaving correctly (#705, ``docs/validators.md`` "Declining instead of
guessing").

A test that compares two such answers to each other cannot tell a decline from
a disagreement. On 2026-08-13 that reddened master's
``pytest (windows-latest, 3.12)`` leg and nothing else, at ac1b3e4:

    {'sub/clean.txt': ' git?'} != {'sub/clean.txt': ''}

That test spawns `git status` fourteen times, measured: thirteen per-path
queries and one repo-wide. One of the thirteen blew the 2s budget on a loaded
runner, so the route under comparison declined and the parity assertion read an
environment condition as the two routes disagreeing about the file. Reproduced
deterministically on macOS by raising ``TimeoutExpired`` from the 13th -- the
one the coalesced pass makes -- which prints the failure above byte for byte.

Same token, same leg and same shape as #1364 (a docs-only PR reddened by the
parallel receipts), the same budget class one layer over as #1360, and the same
defect this repo files against itself: an absence produced by the tooling, read
as an absence in the world (#1205, #1218).

``suffix()`` sits in front of every assertion about the marker and does one of
two things:

  * **returns** the suffix, when git answered. The caller asserts as before.
    That is the only green -- this is a gate, not a tolerance.
  * **skips**, carrying ``TOKEN``, when the suffix carries the decline. Nothing
    is asserted about a lookup that did not happen, and ``conftest`` prints the
    count, its denominator and its population every run, zero included.

**Residual, stated rather than waved away.** ``PATH_META_UNKNOWN`` is one token
for two causes -- the 2s timeout, and a non-zero git exit whose stderr is not
"not a git repository" (a held index lock, a dubious-ownership refusal). Both
are environment conditions here, and the product does not distinguish them in
the suffix, so this gate cannot either: a product bug that made git exit
non-zero would skip rather than red *in this file*. It stays red in
``tests/test_status_swallowed_705.py``, which pins the decline itself against
shimmed gits and is deliberately not routed through here.

**The count is a subset and says so** (#1274). Only a call routed through
``suffix`` can produce a token skip. The spawn-count tests call the product
directly because they read the counter and never the string;
``test_every_marker_assertion_is_decline_gated`` derives that population from
the AST rather than trusting this sentence.
"""
from __future__ import annotations

import os

import pytest

import supertool

#: Grep handle. Appears in every skip this module produces, so `N skipped` on a
#: Windows leg can be resolved to `N did not get an answer out of git here`.
TOKEN = "git-status-decline(#705)"


def require_answer(out: str, subject: str) -> None:
    """Skip, countably, when `out` says the lookup declined. Never tolerate.

    Token-wise and not a substring test: the suffix is a space-joined token
    list, and a symlink target renders into it verbatim.
    """
    if supertool.PATH_META_UNKNOWN not in out.split():
        return
    pytest.skip(
        "{0}: the working-tree lookup for {1!r} declined ({2!r}), so this "
        "runner never produced a marker to compare. The decline itself is "
        "pinned by tests/test_status_swallowed_705.py; do NOT raise the 2s "
        "budget, and do NOT compare a decline against an answer -- that is "
        "what reddened windows-latest at ac1b3e4.".format(TOKEN, subject, out))


def suffix(path, sample: bytes = b"x\n") -> str:
    """`_path_meta_suffix`, gated. The only route this file asserts through.

    Takes a `Path` as readily as a `str` -- the callers hold `tmp_path`
    objects, and `str(...)` at twenty call sites is twenty chances to convert
    the wrong one.
    """
    path = os.fspath(path)
    out = supertool._path_meta_suffix(path, sample)
    require_answer(out, path)
    return out


#: One line for the terminal summary, printed whether the count is zero or not.
POPULATION = (
    "  ^ counts skips carrying that token only, not every test that reads a "
    "git marker: the spawn-count tests call the product directly and never "
    "look at the string, and a decline outside this gate (the shimmed-git "
    "pins in tests/test_status_swallowed_705.py) stays red rather than "
    "skipping. Full population, derived from the AST: "
    "tests/test_path_meta_bulk_1126.py")


def verdict_line(n: int, total: int) -> str:
    """``N of M skipped``, never a bare ``N`` (#1274)."""
    return (
        "{0}: {1} of {2} skipped tests did NOT get a working-tree answer out "
        "of git -- the 2s budget ran out, or git exited non-zero for a reason "
        "other than 'not a repository' (expect 0; a non-zero count is a "
        "runner too loaded to answer, not a finding)".format(TOKEN, n, total))
