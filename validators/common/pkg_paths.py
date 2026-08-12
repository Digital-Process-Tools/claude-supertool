"""Which file did a package-scoped tool name — this one, or another one?

Some analysers do not work per file. `cargo check` compiles a crate, `go vet`
vets a package, and a per-file adapter is handed one path out of it. So "the
output carries a located diagnostic" and "the output says something about the
file you edited" are two different facts, and conflating them publishes a line
number belonging to a sibling under the name of a file that is fine —
`validators/SCHEMA.md` §"A located diagnostic still has to be about *this* file
(#754)".

Three answers, not two. `"unknown"` is the point: a relative path with no base
names a file in every package in the tree, and only `"other"` is entitled to
say another file is at fault.

**This module is deliberately smaller than `cargo-check`'s private copy of the
same idea**, and the difference is the base. cargo prints paths relative to a
workspace root the adapter has to ask cargo for (#1045), which is why that copy
carries a `cargo metadata` round trip and the history of a suffix match that
could not be made correct (#1037). A caller that *chooses* the working
directory it invokes the tool in already knows the base, so nothing here has to
be inferred. `go-vet` runs the tool in the package directory for that reason.
cargo-check has not adopted this module; it cannot, without deciding what to do
with the workspace lookup, which is its own change.
"""

from __future__ import annotations

import ntpath
import os
import posixpath
from pathlib import Path
from typing import Callable


def canon(path: object, normcase: Callable[[str], str] | None = None) -> str:
    """A path in one comparable form: folded for case, separated by `/` (#754).

    **The separator is normalised after the fold, never before.**
    `os.path.normcase` is the only stdlib call that knows whether this platform
    is case-insensitive, and on Windows it does a second thing its name does
    not advertise: it rewrites every `/` into a backslash. Replacing separators
    first and folding afterwards therefore un-normalises them, and a comparison
    still looking for `/` matches nothing at all — on Windows every diagnostic,
    including the file's own, is demoted to a non-verdict. That is #1005, four
    red legs, on the regression the check existed to prevent.

    `normcase` is injectable so Windows semantics can be asserted from any
    runner. A platform behaviour pinned only on that platform is pinned only
    where it was already going to be noticed.
    """
    fold = normcase or os.path.normcase
    return fold(str(path)).replace("\\", "/")


def is_abs(path: str) -> bool:
    """Absolute under either platform's rules, whichever one we run on.

    `os.path.isabs` answers for the host, and the host is not always the
    platform the path came from: a Windows-shaped path reaches these tests, and
    a Windows CI leg's tool output, from a runner this module also has to pass
    on.
    """
    return ntpath.isabs(path) or posixpath.isabs(path)


def _spellings(text: str, normcase: Callable[[str], str] | None) -> tuple:
    """`(forms, refused)` for one absolute path.

    `forms` always holds the unresolved spelling, so the caller's comparison
    happens either way — a refusal here never empties the set and never turns a
    finding into a crash. What it costs is the *second* spelling:
    `Path.resolve()` goes through `_getfinalpathname` on Windows and returns the
    canonical on-disk name, following `subst` and symlinked drives, which is the
    only thing that can tell two spellings of one file apart (#754).

    So `refused` is reported rather than swallowed. Losing that spelling can
    only shrink the match set — it can never invent a match — which means it can
    demote the edited file's own finding to "some other file", and never charge
    a foreign file's finding to the edited one. The safe direction, but still a
    wrong sentence, and `attribute` uses the flag to decline instead of saying
    it. **A resolution nobody attempted is not a refusal**: a Windows-shaped
    path on a POSIX runner is absolute under `ntpath` and not under `os.path`,
    so the guard below is the host's `isabs`, not this module's.
    """
    forms = {posixpath.normpath(canon(text, normcase))}
    if not os.path.isabs(text):
        return forms, False
    try:
        forms.add(posixpath.normpath(canon(Path(text).resolve(), normcase)))
    except OSError:
        # The OS declined to canonicalise a path it was handed — Windows
        # `_getfinalpathname` raising `PermissionError` or `[WinError 123]`,
        # where CPython's non-strict fallback catches `FileNotFoundError`
        # alone. Nothing to log to (an adapter's stdout is its JSON receipt),
        # so it is carried out as a value.
        return forms, True
    return forms, False


def _target_spellings(target: object, target_raw: object = "",
                      normcase: Callable[[str], str] | None = None) -> tuple:
    """`(forms, refused)` over both spellings a caller may have produced."""
    forms, refused = set(), False
    for raw in (target, target_raw):
        text = str(raw or "").strip()
        if not text:
            continue
        if not is_abs(text):
            text = os.path.abspath(text)
        more, bad = _spellings(text, normcase)
        forms |= more
        refused = refused or bad
    return forms, refused


def target_forms(target: object, target_raw: object = "",
                 normcase: Callable[[str], str] | None = None) -> set:
    """Every spelling of the file under validation — all of them absolute.

    Both forms are kept for the reason #754 gave: `os.path.abspath` joins onto
    the working directory while `Path.resolve()` goes through
    `_getfinalpathname` on Windows and returns the canonical on-disk spelling,
    following `subst` and symlinked drives. They disagree, and the answer must
    not depend on which one a caller happened to produce.

    This returns the set alone. Whether the OS refused to resolve one of them
    is a fact `attribute` needs and this signature cannot carry, so `attribute`
    calls `_target_spellings` instead.
    """
    return _target_spellings(target, target_raw, normcase)[0]


def attribute(reported: str, target: object, base: object = "",
              target_raw: object = "",
              normcase: Callable[[str], str] | None = None) -> str:
    """`"this"` / `"other"` / `"unknown"` for the path a tool printed.

    The comparison is equality between two absolute paths, never a suffix
    match. A suffix match cannot tell a short path that is a tail of the target
    from a different file higher up the tree — `vendor/crates/foo/src/main.rs`
    "was" `crates/foo/src/main.rs` under one, and a foreign file's error was
    charged to the file under validation on a `rollback_on_fail` validator
    (#1037). A relative path is anchored to `base`, which the caller knows
    because the caller chose the directory the tool ran in.

    **Without a base a relative path names no file**, and the answer is
    `"unknown"` rather than a pick between the candidates. An absolute path
    needs no base and is still decided.

    **A resolution the OS refused is the third answer too.** `resolve()` is the
    only thing that sees through a `subst` drive or a symlinked root, so when it
    raises, the equality test ran on the unresolved spellings alone and a
    non-match no longer rules out an alias. `"other"` is a positive claim — the
    caller prints "another file in this package" — and this module's whole point
    is that only `"other"` is entitled to make it. A *match* under an incomplete
    comparison is still `"this"`: matching is positive evidence, not-matching
    under a comparison that was cut short is not negative evidence.
    """
    src = (reported or "").strip()
    if not src:
        return "unknown"

    folded = posixpath.normpath(canon(src, normcase))
    if is_abs(src):
        forms, refused = _spellings(src, normcase)
    else:
        if not str(base or "").strip():
            return "unknown"
        anchor = posixpath.normpath(canon(base, normcase))
        forms = {posixpath.normpath(posixpath.join(anchor, folded))}
        refused = False

    known, target_refused = _target_spellings(target, target_raw, normcase)
    if forms & known:
        return "this"
    if refused or target_refused:
        return "unknown"
    return "other"
