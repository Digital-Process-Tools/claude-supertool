#!/usr/bin/env python3
"""What tool results actually cost, measured off the transcripts on disk (#1252).

Every claim this repo makes about batching is reasoning. This op is the
measurement: bytes of `tool_result` content, grouped by tool and — for
supertool calls, whose renders carry their own `--- op ---` section markers —
by op.

Three states, not two. A session that could not be parsed is `skipped` with a
reason and named on screen; a result whose `tool_use_id` matches no `tool_use`
is counted under `?` rather than dropped; a non-text result block (an image) is
excluded from the byte total and disclosed. A cost report that quietly omits
the expensive sessions is worse than no report.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))  # for _console (#1388)
from _console import use_utf8_stdout  # noqa: E402  (glyphs on a cp437 console -- #1388)
from _common import (  # noqa: E402
    Redactor,
    decline_lines,
    resolve_project_dir,
    session_path,
    source_note,
    wants_raw,
)

# Tools whose result is the content of a file, for the repeat-read question.
READ_TOOLS = {"Read", "NotebookRead"}
PATH_KEYS = ("file_path", "notebook_path")

# supertool renders one section per op, headed by this exact line.
_SECTION_RE = re.compile(r"^--- (.+) ---\Z")
_OP_RE = re.compile(r"^[a-z][a-z0-9_-]*(?::|$)")
_SUPERTOOL_RE = re.compile(r"\bsupertool(?:\.py)?\b")
_ARG_RE = re.compile(r"""\s+(?:'([^']*)'|"([^"]*)"|([^\s'"|;&<>()]+))""")
_SECTION_RE_INLINE = re.compile(r"---.*?---")


def command_ops(cmd):
    """The op tokens a Bash command actually passed to supertool.

    The section markers alone are not evidence that an op ran: agents write
    `echo "--- branch ---"` as a separator in plain shell, and measured over
    five live sessions that invented nine ops that do not exist and moved real
    bytes onto them. So a marker is only believed when the command line it came
    from named that op. Scanning stops at the first shell separator or heredoc
    after each `supertool` word, so a marker echoed later in the same command
    is not corroboration.
    """
    ops = set()
    if not isinstance(cmd, str):
        return ops
    for m in _SUPERTOOL_RE.finditer(cmd):
        pos = m.end()
        while True:
            m2 = _ARG_RE.match(cmd, pos)
            if not m2:
                break
            token = m2.group(1) or m2.group(2) or m2.group(3) or ""
            pos = m2.end()
            if token.startswith("-"):
                continue  # a flag, not an op
            ops.add(token.split(":", 1)[0].strip())
    return ops


def command_mentions(cmd, token):
    """True when the command names `token` somewhere other than in an echoed
    `--- ... ---` marker.

    This is what admits a nested op — `batch:@-` renders one `--- edit:… ---`
    section per sub-op, and those really are edits, so folding them into
    `batch` would hide the op whose render is fat. The marker text itself is
    removed first, so `echo "--- filing ---"` cannot corroborate itself.
    """
    if not isinstance(cmd, str):
        return False
    stripped = _SECTION_RE_INLINE.sub(" ", cmd)
    return re.search(r"(?<![\w-])" + re.escape(token) + r"(?![\w-])", stripped) is not None


def split_sections(text):
    """Split a supertool render into (header, body, header_line) triples.

    `header_line` is the raw marker line, kept so a caller folding a rejected
    sub-section back into its parent can return those bytes to the op that
    printed them instead of leaving them in overhead.

    Only a full line of the form `--- <op> ---` whose payload starts with an
    op-shaped token opens a section: file content is full of dashed lines, and
    treating one as a section header would move real bytes onto an op that
    never ran. Bytes before the first header belong to no op and are not
    returned — the caller reports them as overhead rather than spreading them.
    """
    sections = []
    current = None
    current_line = ""
    buf = []
    for line in text.splitlines(keepends=True):
        m = _SECTION_RE.match(line.strip())
        if m and _OP_RE.match(m.group(1)):
            if current is not None:
                sections.append((current, "".join(buf), current_line))
            current = m.group(1)
            current_line = line
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections.append((current, "".join(buf), current_line))
    return sections


def _nbytes(s):
    return len(s.encode("utf-8", errors="replace"))


def _percentile(sorted_sizes, q):
    """Nearest-rank percentile. No interpolation: these are real result sizes,
    and an interpolated 45,312.5-byte result never happened."""
    if not sorted_sizes:
        return 0
    idx = max(0, math.ceil(q * len(sorted_sizes)) - 1)
    return sorted_sizes[min(idx, len(sorted_sizes) - 1)]


class Stats:
    """Running totals for one measurement run.

    A plain class, not a dataclass: this module is loaded out of tree by the
    test suite's preset loader, and `@dataclass` resolves `cls.__module__`
    through `sys.modules`, which raises for a module loaded under a synthetic
    name on 3.14.
    """

    def __init__(self):
        self.sessions_measured = 0
        self.sessions_no_results = 0
        self.skipped = []            # (name, reason)
        self.malformed_lines = 0

        self.results = 0
        self.total_bytes = 0
        self.all_sizes = []

        self.per_tool = {}           # tool name -> [sizes]
        self.orphan_results = 0
        self.orphan_bytes = 0

        self.nontext_blocks = 0
        self.nontext_kinds = {}

        self.per_op = {}             # op name -> [sizes]
        self.op_marked_calls = 0
        self.unattributed_calls = 0
        self.unattributed_bytes = 0
        self.section_overhead_bytes = 0

        self.repeat_read_paths = 0
        self.repeat_read_results = 0
        self.repeat_read_bytes = 0
        self.unchanged_repeat_results = 0
        self.unchanged_repeat_bytes = 0
        self.top_repeat_paths = []

        self.error_results = 0
        self.error_bytes = 0
        self.empty_results = 0

    def add(self, tool, size):
        self.per_tool.setdefault(tool, []).append(size)
        self.all_sizes.append(size)
        self.results += 1
        self.total_bytes += size


def _iter_events(path, st):
    """Yield decoded events, counting undecodable lines instead of hiding them."""
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                st.malformed_lines += 1
                continue
            if isinstance(obj, dict):
                yield obj
            else:
                st.malformed_lines += 1


def _result_text(part, st):
    """Bytes of a tool_result that actually reach the model as text.

    A list-shaped content block can hold an image; its base64 payload is not
    comparable to text bytes, so it is excluded and counted separately rather
    than folded into a total that would then be quietly wrong.
    """
    content = part.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                out.append(b.get("text") or "")
            else:
                kind = b.get("type") or "?"
                st.nontext_blocks += 1
                st.nontext_kinds[kind] = st.nontext_kinds.get(kind, 0) + 1
        return "".join(out)
    return ""


_ABS_RE = re.compile(r"^(?:/|[A-Za-z]:[\\/]|~)")


def normalise_target(cwd, target):
    """Key a read by one path string, whichever route produced it.

    `Read` passes an absolute `file_path`; a supertool `read:` op is usually
    written relative to the session cwd, which the transcript records on every
    event. Left unjoined the same file lands under two keys and the repeat-read
    figure — the number this op exists to produce — reads low. String-level on
    purpose: the separator that matters is the one in the transcript, not the
    one on the machine running the report.
    """
    t = (target or "").replace("\\", "/")
    if not t or _ABS_RE.match(t):
        return t
    if not cwd:
        return t
    return cwd.replace("\\", "/").rstrip("/") + "/" + t


_READ_ARG_RE = re.compile(r"^(?:|full|-?\d+|\d+-\d+|\d+\+\d+)\Z")


def read_target(rest):
    """The path a `read:` section header names, with the op's own numeric args
    stripped.

    Splitting once more on ':' to drop `:OFF:LIM` also eats a Windows drive
    colon, keying every absolute path under `"C"` — unrelated files merged into
    one fabricated repeat read, and the join with `Read`'s absolute file_path
    broken, in the very figure this op exists to produce. So trailing tokens are
    dropped only when they look like arguments, and a leading single letter
    followed by a separator is put back.
    """
    parts = (rest or "").split(":")
    if len(parts) > 1 and len(parts[0]) == 1 and parts[0].isalpha() and parts[1][:1] in ("/", "\\"):
        parts = [parts[0] + ":" + parts[1]] + parts[2:]
    while len(parts) > 1 and _READ_ARG_RE.match(parts[-1]):
        parts.pop()
    return ":".join(parts)


def _record_read(reads, read_bytes, target, text, size):
    digest = hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()
    reads.setdefault(target, []).append((digest, size))
    read_bytes[target] = read_bytes.get(target, 0) + size


def measure_sessions(paths):
    """Measure one or more session transcripts. Returns a populated Stats."""
    st = Stats()
    reads = {}       # target path -> [(digest, size), ...]
    read_bytes = {}  # target path -> total bytes

    for sp in paths:
        try:
            events = list(_iter_events(sp, st))
        except OSError as exc:
            st.skipped.append((sp.name, f"unreadable: {exc}"))
            continue
        if not events:
            st.skipped.append((sp.name, "no parsable JSON events"))
            continue

        tool_names = {}
        session_results = 0
        cwd = ""
        for ev in events:
            if isinstance(ev.get("cwd"), str) and ev["cwd"]:
                cwd = ev["cwd"]
            msg = ev.get("message")
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "tool_use":
                    tool_names[part.get("id")] = (part.get("name") or "?", part.get("input") or {})
                    continue
                if part.get("type") != "tool_result":
                    continue

                session_results += 1
                known = tool_names.get(part.get("tool_use_id"))
                text = _result_text(part, st)
                size = _nbytes(text)
                if known is None:
                    tool, inp = "?", {}
                    st.orphan_results += 1
                    st.orphan_bytes += size
                else:
                    tool, inp = known
                st.add(tool, size)
                if part.get("is_error"):
                    st.error_results += 1
                    st.error_bytes += size
                if size == 0:
                    st.empty_results += 1

                cmd = inp.get("command") if isinstance(inp, dict) else None
                allowed = command_ops(cmd)
                kept = []
                for header, sec, header_line in split_sections(text):
                    op = header.split(":", 1)[0]
                    if op in allowed or (kept and command_mentions(cmd, op)):
                        kept.append([op, header, sec])
                    elif kept:
                        # A sub-header inside the render of the op above it. Its
                        # bytes belong to that op, not to an op of its own name.
                        kept[-1][2] += header_line + sec
                if kept:
                    st.op_marked_calls += 1
                    body = 0
                    for op, header, sec in kept:
                        n = _nbytes(sec)
                        st.per_op.setdefault(op, []).append(n)
                        body += n
                        if op == "read" and ":" in header:
                            target = read_target(header.split(":", 1)[1])
                            if target:
                                _record_read(
                                    reads, read_bytes, normalise_target(cwd, target), sec, n
                                )
                    st.section_overhead_bytes += max(0, size - body)
                else:
                    st.unattributed_calls += 1
                    st.unattributed_bytes += size

                if tool in READ_TOOLS:
                    for k in PATH_KEYS:
                        if isinstance(inp.get(k), str) and inp[k]:
                            _record_read(
                                reads, read_bytes, normalise_target(cwd, inp[k]), text, size
                            )
                            break

        st.sessions_measured += 1
        if session_results == 0:
            st.sessions_no_results += 1

    for target, digests in reads.items():
        if len(digests) < 2:
            continue
        st.repeat_read_paths += 1
        st.repeat_read_results += len(digests)
        st.repeat_read_bytes += read_bytes[target]
        seen = set()
        for digest, size in digests:
            if digest in seen:
                st.unchanged_repeat_results += 1
                st.unchanged_repeat_bytes += size
            seen.add(digest)
    st.top_repeat_paths = sorted(
        ((t, len(d), read_bytes[t]) for t, d in reads.items() if len(d) > 1),
        key=lambda row: row[2],
        reverse=True,
    )[:5]
    return st


def _share(part, whole):
    return (part / whole * 100) if whole else 0.0


def _table(title, rows_src, total, red, first_col):
    """rows_src: key -> [sizes]. Prints count, bytes, share, p50, p95, max."""
    if not rows_src:
        return
    print(title)
    print(f"{first_col:<22} {'CALLS':>7} {'BYTES':>12} {'SHARE':>7} {'P50':>8} {'P95':>8} {'MAX':>9}")
    for name, sizes in sorted(rows_src.items(), key=lambda kv: sum(kv[1]), reverse=True):
        s = sorted(sizes)
        print(
            f"{red(name)[:22]:<22} {len(s):>7} {sum(s):>12} "
            f"{_share(sum(s), total):>6.1f}% {_percentile(s, 0.5):>8} "
            f"{_percentile(s, 0.95):>8} {s[-1]:>9}"
        )
    print()


def render(st, header_lines, red):
    for line in header_lines:
        print(line)
    note = red.note()
    if note:
        print(note)

    sess_word = "session" if st.sessions_measured == 1 else "sessions"
    res_word = "tool result" if st.results == 1 else "tool results"
    print(
        f"Measured: {st.sessions_measured} {sess_word}, "
        f"{st.results} {res_word}, {st.total_bytes} bytes"
    )
    if st.skipped:
        print(f"Skipped:  {len(st.skipped)} sessions — named below, none folded into the total")
        for name, reason in st.skipped:
            print(f"  {red(name)} — {reason}")
    else:
        print("Skipped:  0 sessions")

    notes = []
    if st.malformed_lines:
        notes.append(f"{st.malformed_lines} malformed JSONL lines skipped")
    if st.orphan_results:
        notes.append(
            f"{st.orphan_results} results with no matching tool_use "
            f"({st.orphan_bytes} bytes) — counted under tool '?', not dropped"
        )
    if st.nontext_blocks:
        kinds = ", ".join(f"{k} x{v}" for k, v in sorted(st.nontext_kinds.items()))
        notes.append(
            f"{st.nontext_blocks} non-text result blocks ({kinds}) excluded — "
            "base64 bytes are not comparable to text bytes"
        )
    if st.sessions_no_results:
        notes.append(f"{st.sessions_no_results} measured sessions contained no tool results")
    if notes:
        print("Notes:")
        for n in notes:
            print(f"  {n}")
    print()

    if st.results == 0:
        print("No tool results to measure.")
        return

    _table("By tool", st.per_tool, st.total_bytes, red, "TOOL")

    if st.per_op:
        _table(
            "By supertool op (split on the '--- op ---' markers in the render)",
            st.per_op,
            st.total_bytes,
            red,
            "OP",
        )
        print(
            f"  section headers and preamble: {st.section_overhead_bytes} bytes "
            f"across {st.op_marked_calls} marked calls"
        )
    print(
        f"Results with no op markers: {st.unattributed_calls} calls, "
        f"{st.unattributed_bytes} bytes ({_share(st.unattributed_bytes, st.total_bytes):.1f}%) "
        "— not attributable to an op"
    )
    print()

    top = sorted(st.all_sizes, reverse=True)[:10]
    print("Concentration")
    print(
        f"  Top 10 results: {sum(top)} bytes "
        f"({_share(sum(top), st.total_bytes):.1f}% of total, from {st.results} results)"
    )
    print()

    print("Repeat reads")
    print(
        f"  Paths read more than once: {st.repeat_read_paths} "
        f"({st.repeat_read_results} reads, {st.repeat_read_bytes} bytes, "
        f"{_share(st.repeat_read_bytes, st.total_bytes):.1f}% of total)"
    )
    print(
        f"  Re-reads returning identical bytes: {st.unchanged_repeat_results} "
        f"({st.unchanged_repeat_bytes} bytes, "
        f"{_share(st.unchanged_repeat_bytes, st.total_bytes):.1f}% of total)"
    )
    for target, n, b in st.top_repeat_paths:
        print(f"    {n:>3}x {b:>10}  {red(target)}")
    print()

    print("Failures and empties")
    print(
        f"  Errors: {st.error_results} results, {st.error_bytes} bytes "
        f"({_share(st.error_bytes, st.total_bytes):.1f}%)"
    )
    print(f"  Empty:  {st.empty_results} results, 0 bytes")


def main():
    use_utf8_stdout()
    args = sys.argv[1:]
    red = Redactor(enabled=not wants_raw(args))
    limit = 10
    uuid = ""
    for a in args:
        if str(a).strip().lower() == "raw":
            continue
        if str(a).isdigit():
            limit = int(a)
        elif a:
            uuid = a

    if uuid:
        try:
            sp = session_path(uuid)
        except ValueError as exc:
            print(f"ERROR: {exc}")
            return 1
        if not sp.exists():
            print(f"ERROR: session not found: {sp}")
            return 1
        paths = [sp]
        header = [f"Tool result cost — session {red(uuid)}", f"File: {sp}"]
    else:
        source = resolve_project_dir()
        if source.kind == "missing":
            # See list.py: a sibling store is never an answer about this cwd (#1317).
            for line in decline_lines(source):
                print(line)
            return 1
        pdir = source.path
        found = sorted(pdir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not found:
            print(f"No sessions found in {pdir}")
            return 0
        paths = found[:limit]
        header = [
            "Tool result cost",
            f"Project: {pdir}",
            f"Selected: {len(paths)} most recent of {len(found)} sessions",
        ]
        note = source_note(source)
        if note:
            header.insert(1, note)

    st = measure_sessions(paths)
    render(st, header, red)
    return 0


if __name__ == "__main__":
    sys.exit(main())
