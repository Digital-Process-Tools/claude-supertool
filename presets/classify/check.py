#!/usr/bin/env python3
"""classify:TEXT|file://PATH -- answer safe / suspect / could-not-classify
for a piece of untrusted text (#2046).

Two stages, and the short-circuit only ever runs toward `suspect`:

    scanner matches a known shape  -> suspect, no spawn (scanner.py)
    scanner matches nothing        -> not a verdict; the spawn produces one
    scanner could not run          -> could-not-classify

`could-not-classify` never renders as `safe` -- see `model.classify`'s
docstring for every path that leads there. This is a labeller, not a
gatekeeper: it returns a verdict and never drops, filters, or blocks
anything itself. Policy about what each level means belongs to the caller.

**`file://` only, no bare-path arm** (#2039 is the reason: a bare-path arm
on a sibling publish op was a documented convenience that became a
disclosure vector the moment untrusted text reached the slot, and this op's
entire input population IS untrusted text). A resolved path must stay inside
the current working directory -- the same containment every other file-path
op in this tree enforces -- so `file:///etc/passwd` or a path climbing out
via `..` is refused before anything is read.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # for _console
from _console import use_utf8_stdout  # noqa: E402  (glyphs on a cp437 console -- #2062)
import scanner  # noqa: E402
import model  # noqa: E402
import cache as classify_cache  # noqa: E402  (#2054 -- aliased so `run`'s own
# `cache` parameter never shadows the module it is passed instances of)

_FILE_PREFIX = "file://"


def resolve_text(arg: str) -> str:
    """`file://PATH` reads PATH (contained to cwd); anything else is the
    literal text to classify. Exits (2) on a bad `file://` path -- this is a
    usage error about the CALL, not a verdict about the text, so it is
    reported the same way every other preset op reports a bad path rather
    than folding into `could-not-classify`."""
    if not arg.startswith(_FILE_PREFIX):
        return arg
    raw_path = arg[len(_FILE_PREFIX):]
    if not raw_path:
        sys.stderr.write("ERROR: file:// with no path\n")
        sys.exit(2)
    cwd = Path.cwd().resolve()
    try:
        resolved = (cwd / raw_path).resolve()
    except OSError:
        sys.stderr.write(f"ERROR: cannot resolve path: {raw_path!r}\n")
        sys.exit(2)
    try:
        resolved.relative_to(cwd)
    except ValueError:
        sys.stderr.write(
            f"ERROR: classify file:// path escapes the working directory: "
            f"{raw_path!r}\n  resolved to: {resolved}\n  cwd: {cwd}\n")
        sys.exit(2)
    if not resolved.is_file():
        sys.stderr.write(f"ERROR: file not found: {raw_path!r}\n")
        sys.exit(2)
    try:
        return resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        sys.stderr.write(f"ERROR: cannot read {raw_path!r}: {exc}\n")
        sys.exit(2)


def run(text: str, *, cache: object = None) -> str:
    """The two-stage decision, returned as the op's printed report. Kept
    apart from `main()` so a test can call it directly with a stubbed
    `model.classify` and never spawn anything real.

    `cache` defaults to `None` -- forwarded verbatim to `model.classify`,
    which itself defaults to no caching (#2054). `run()` calling `classify`
    without it is unchanged from before this parameter existed; `main()`
    below is the one caller that passes `classify_cache.default_cache()`,
    so a real repeated invocation of this op (the case #2054 exists for)
    gets the file-backed cache and every direct `run()` call in this
    module's own test suite continues to exercise a bare `model.classify`
    the way it always has."""
    findings = scanner.scan(text)
    if findings:
        axes = ", ".join(f.axis for f in findings)
        lines = [
            "verdict: suspect",
            f"scanner: suspect ({axes})",
            "model: not-run",
        ]
        for f in findings:
            lines.append(f"  {f.axis}: {f.detail}")
        return "\n".join(lines)

    verdict = model.classify(text, cache=cache)
    lines = ["verdict: " + verdict.state, "scanner: clean"]
    if verdict.state == "safe":
        lines.append("model: safe")
    elif verdict.state == "suspect":
        lines.append(f"model: suspect ({', '.join(verdict.axes)})")
    else:
        lines.append(f"model: could-not-classify ({verdict.reason})")
    return "\n".join(lines)


def main(arg: str) -> None:
    use_utf8_stdout()
    text = resolve_text(arg)
    if not text.strip():
        sys.stderr.write("ERROR: nothing to classify (empty text)\n")
        sys.exit(2)
    # The real caller of `run()`, and the only one that opts into the
    # verdict cache (#2054): every invocation of this op from the CLI, a
    # human's or a repeated one, is a fresh subprocess -- see
    # `classify_cache`'s own module docstring for why an in-process cache
    # could never have helped here at all. `default_cache()` never raises;
    # a cache that cannot establish its directory degrades to "nothing
    # cached" rather than taking this call down with it.
    print(run(text, cache=classify_cache.default_cache()))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.stderr.write(
            "ERROR: usage classify:TEXT_OR_file://PATH\n")
        sys.exit(2)
    main(":".join(sys.argv[1:]))
