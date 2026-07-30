#!/usr/bin/env python3
"""Re-emit pytest's failures from `junit.xml` as text a reader can act on (#546).

The problem this exists for is Windows-shaped and was measured, not guessed.
On `windows-latest` a failure line reaches the GitHub log through pytest's own
terminal writer, and that writer is attached to a stream whose codec is the
runner's console codepage. An em dash — ordinary in this repo's assertion
messages — is not in cp437, so it is replaced at *emit time*:

    run 30485881190, job 90691288041 (windows-latest, 3.9)
      ...cannot verify ownership of runtime dir C:\\...\\rt on this platform
      \\xEF\\xBF\\xBD os.geteuid does not exist...

`EF BF BD` is U+FFFD. The character was destroyed before the bytes left the
process, so nothing downstream can recover it. Two intact em dashes (`E2 80 94`)
sit in the same log file, echoed from this workflow's own step names — the log
transport is fine, and the mangling is CI's emitter.

There is a second readability defect in the same line, independent of encoding:
pytest elides the middle of a long summary message to fit the terminal width.

    FAILED ...::test_xml_lint - AssertionError: assert 'POST-EDIT LINT FAILED'
    in 'vim C:\\Users\\...\\x.xml (...int (5s) ---\\nlint did not run...

`(...` is pytest cutting the message, and on that particular failure the elided
part held the word `timeout`, which was the diagnosis.

So this script reads the failures back out of `junit.xml`, which pytest writes
as UTF-8 to disk regardless of what the console can encode, and which carries
the **untruncated** message. It then writes them to stdout on a stream it
reconfigures itself — not via `PYTHONIOENCODING`, so no environment a runner
happens to have can bypass or disarm it.

**It always exits 0, deliberately.** It runs under `if: always()`, next to a
pytest step that owns the verdict. A reporter that could fail would be able to
turn a green run red for a reason that has nothing to do with the diff. What it
must never do instead is report a *pass*: a missing file, an unparseable file
and a file with no failures print three different sentences, because the step
it replaced collapsed the first two into `(no junit.xml — pytest crashed before
generating it)` — which would have been printed, untruthfully, by any
`UnicodeEncodeError` inside the old one-line parser.
"""
from __future__ import annotations

import io
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

#: `backslashreplace` rather than `replace`: UTF-8 encodes every character, so
#: the only input that can reach the handler at all is a lone surrogate (a
#: `surrogateescape` decode upstream). Disclosing it as `\udcff` keeps the byte
#: recoverable. `replace` is what produced the U+FFFD this script exists to
#: undo, and using it here would reinstate the defect one layer out.
_ERRORS = "backslashreplace"


def use_utf8_stdout() -> None:
    """Pin this process's own stdout/stderr to UTF-8, whatever the console is.

    Done in-process on purpose. `PYTHONIOENCODING` in the workflow `env:` would
    reach this script *and* every subprocess the job later spawns, and this repo
    has tests that reproduce Windows encoding defects precisely by leaving the
    environment alone (`tests/test_encoding_seam.py`,
    `tests/test_git_commit_payload_route.py`). A fix whose blast radius includes
    the tests that would catch its own regression is not a fix.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors=_ERRORS)
            continue
        buffer = getattr(stream, "buffer", None)
        if buffer is not None:  # pragma: no cover - 3.7+ always has reconfigure
            setattr(sys, name, io.TextIOWrapper(
                buffer, encoding="utf-8", errors=_ERRORS, line_buffering=True))


def failures(root: ET.Element) -> list[tuple[str, str, str]]:
    """`(kind, test id, message)` for every failing or erroring testcase."""
    found = []
    for case in root.iter("testcase"):
        for child in case:
            if child.tag not in ("failure", "error"):
                continue
            classname = case.attrib.get("classname", "")
            name = case.attrib.get("name", "")
            ident = f"{classname}.{name}" if classname else name
            found.append((child.tag, ident, (child.attrib.get("message") or "")))
    return found


def render(path: Path) -> list[str]:
    """The lines to print for `path`. Separated from I/O so it is testable."""
    if not path.exists():
        return [f"(no {path} was written — pytest did not reach the end of its "
                f"run; its own output above is the only record)"]
    try:
        root = ET.parse(str(path)).getroot()
    except ET.ParseError as exc:
        return [f"({path} exists but is not parseable XML: {exc}. That is this "
                f"reporter failing, which is not the same thing as the suite "
                f"passing — read the pytest step's output above)"]
    found = failures(root)
    if not found:
        return [f"(no failures or errors recorded in {path})"]
    lines = [f"{len(found)} failing test(s), full messages from {path}:"]
    for kind, ident, message in found:
        lines.append(f"FAILED {ident}" if kind == "failure" else f"ERROR {ident}")
        body = message.strip().splitlines()
        if not body:
            body = [f"(<{kind}> carried no message attribute)"]
        lines.extend(f"    {line}" for line in body)
    return lines


def main(argv: list[str]) -> int:
    use_utf8_stdout()
    path = Path(argv[1]) if len(argv) > 1 else Path("junit.xml")
    for line in render(path):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
