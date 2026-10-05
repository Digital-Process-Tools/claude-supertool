#!/usr/bin/env python3






















from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  
from _console import use_utf8_stdout  
import scanner  
import model  
import cache as classify_cache  


_FILE_PREFIX = "file://"


def resolve_text(arg: str) -> str:





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







    print(run(text, cache=classify_cache.default_cache()))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.stderr.write(
            "ERROR: usage classify:TEXT_OR_file://PATH\n")
        sys.exit(2)
    main(":".join(sys.argv[1:]))
