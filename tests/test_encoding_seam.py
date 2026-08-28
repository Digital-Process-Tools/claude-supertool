"""The #418 encoding seam, driven as a real subprocess under a hostile locale.

Four defects came out of this seam one at a time (#400, #415, #431) because
nothing in the suite had ever run supertool as a **separate process** with a
non-UTF-8 environment. Every other test calls ``main()`` in-process under
pytest's UTF-8 capture, where all of them are invisible by construction.

Two halves, both reproducible on Linux and macOS — neither needs a Windows
runner, which matters because every member of this family so far was found by
a platform the author was not sitting on:

* **the read half** — a bare ``open()`` / ``read_text()`` decodes with
  ``locale.getpreferredencoding()``. A static AST scan enumerates every such
  call in shipped code, and a subprocess under ``-X warn_default_encoding``
  with ``EncodingWarning`` promoted to an error catches a reintroduced one on
  the paths actually executed. The technique is #431's, generalised.
* **the stdout half** — ``PYTHONIOENCODING=cp1252`` reproduces the Windows
  console failure, and a forced C locale reproduces the config-decode failure.

The static scan is the part that keeps working for code added later: the
dynamic pins only see the lines a given op executes.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path
from types import MappingProxyType
from typing import Dict, List, Tuple

import pytest

ROOT = Path(__file__).resolve().parent.parent
SUPERTOOL = ROOT / "supertool.py"

# Shipped code — what a user runs. Held to both halves of the rule, reads and
# writes.
SHIPPED = ("supertool.py", "_supertool.py", "presets", "hooks", "validators",
           "formatters", "notifiers")

# `tests/` is scanned too, but for **reads only** (#461). The ~1670 `write_text()`
# fixture calls are the noise that made #418's blanket exclusion correct; the
# reads are not noise, because a test reading a real repo file as data decodes
# the project's accumulated non-ASCII with the locale codec — twice escaped to a
# Windows runner in one day. See test_no_test_file_decodes_a_read_by_locale.
TESTS_DIR = Path(__file__).resolve().parent

# Text-mode readers/writers that fall back to the locale when `encoding=` is
# omitted. `os.open` takes no encoding (raw fd) and is excluded by name below.
_PATH_TEXT_METHODS = frozenset({"read_text", "write_text"})

# Every keyword `open`, `os.fdopen` and `Path.open` declare, unioned. A call
# naming anything else cannot be one of them, however it is spelled (#766) --
# `OpenerDirector.open(req, timeout=...)` opens no file and decodes nothing.
_OPEN_KEYWORDS = frozenset(
    {"file", "fd", "mode", "buffering", "encoding", "errors", "newline", "closefd", "opener"}
)


def _literal_mode(node: ast.Call, positional_index: int) -> str:
    """The call's mode as a literal, or "" when absent/non-literal."""
    for kw in node.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            return kw.value.value if isinstance(kw.value.value, str) else ""
    if len(node.args) > positional_index:
        arg = node.args[positional_index]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
    return ""


def _call_kind(node: ast.Call) -> Tuple[str, str]:
    """``(kind, name)`` for a text-mode file call that names no codec.

    ``kind`` is ``"read"``, ``"write"`` or ``""`` — the last meaning "not one of
    these calls, or already carrying ``encoding=``". Binary modes are exempt (no
    decoding happens) and so is ``os.open``, which returns a raw fd. A
    non-literal mode counts as a read, because the answer is unknown and the
    unknown answer that keeps a guard honest is the one that flags.

    A call is also not one of these when it names a keyword none of them
    declares (#766). That is a fact about the signatures rather than a guess
    about the receiver, which is why it can be trusted where the receiver's type
    cannot: `presets/_http.py` calls `OpenerDirector.open(req, timeout=...)`,
    and reading `timeout=` as a text-file open reported a codec defect in a line
    that touches no file -- the same confident-report-about-something-unchecked
    this scan exists to prevent. `**kwargs` names nothing at parse time and so
    keeps flagging, on the rule above.
    """
    func = node.func
    is_attr = isinstance(func, ast.Attribute)
    if is_attr:
        name = func.attr
    elif isinstance(func, ast.Name):
        name = func.id
    else:
        return "", ""
    if any(kw.arg == "encoding" for kw in node.keywords):
        return "", ""
    if name in _PATH_TEXT_METHODS:
        return ("read" if name == "read_text" else "write"), name
    if name not in ("open", "fdopen"):
        return "", ""
    # os.open(path, flags) — a raw fd, no codec involved.
    if (is_attr and isinstance(func.value, ast.Name)
            and func.value.id == "os" and name == "open"):
        return "", ""
    if any(kw.arg is not None and kw.arg not in _OPEN_KEYWORDS for kw in node.keywords):
        return "", ""
    # `Path.open(mode)` puts mode first; `open(path, mode)` puts it second.
    mode = _literal_mode(node, 0 if (is_attr and name == "open") else 1)
    if "b" in mode:
        return "", ""
    return ("write" if any(c in mode for c in "wax") else "read"), name


def encoding_violations(
    path: Path, kinds: Tuple[str, ...] = ("read", "write"),
) -> List[Tuple[int, str]]:
    """Every text-mode file call in `path` that leaves the codec to the locale.

    Parsed rather than grepped so prose about the defect does not count as an
    instance of it — same reasoning as ``_null_signal_kills``. ``kinds`` narrows
    the scan: shipped code is held to both halves, ``tests/`` to reads only.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        kind, name = _call_kind(node)
        if kind in kinds:
            found.append((node.lineno, name))
    return found


def _shipped_files() -> List[Path]:
    files: List[Path] = []
    for entry in SHIPPED:
        target = ROOT / entry
        if target.is_dir():
            files.extend(sorted(target.rglob("*.py")))
        elif target.is_file():
            files.append(target)
    return files


def test_no_shipped_file_decodes_by_locale() -> None:
    """The enumerator. Every text read/write in shipped code names its codec.

    This is the half that keeps enumerating: the subprocess pins below only see
    the lines the op they run happens to execute, so a bare ``open()`` on a
    cold path — which is exactly where all four #418 defects lived — would go
    on being invisible until a user on a cp1252 console hit it.
    """
    offenders = []
    for path in _shipped_files():
        for lineno, call in encoding_violations(path):
            offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {call}() without encoding=")
    assert not offenders, (
        "These calls decode with locale.getpreferredencoding() — cp1252 on a "
        "Windows console, ASCII under a C locale. Pass encoding=\"utf-8\" "
        "(or open in binary mode if no text is involved):\n  "
        + "\n  ".join(offenders)
    )


def test_no_test_file_decodes_a_read_by_locale() -> None:
    """The same rule inside ``tests/``, narrowed to reads. #461.

    #418 excluded ``tests/`` wholesale and the reason was sound: the suite
    holds ~1670 ``write_text()`` fixture calls emitting ASCII, and enforcing
    there would bury the signal. It does not hold for **reads**, because a test
    that reads a real repository file as data — source, docs, a manifest —
    decodes whatever non-ASCII the project has accumulated with the locale
    codec. That escaped to a Windows runner twice in one day (#431 scanning
    preset source, #460 reading ``docs/presets/watch.md``), both times in tests
    written after #418 landed.

    **No path analysis.** The obvious narrowing — flag only reads whose target
    resolves inside the repo, exempt ``tmp_path`` fixtures — is the version
    that would have missed #431: its read was ``path.read_text()`` on a
    *function parameter*, fed from ``PRESETS_DIR.rglob()`` at a call site in
    another function. Any target-based test is interprocedural or it is wrong,
    and a scan with silent false negatives that looks complete is worse than a
    blunt one. So every bare read in ``tests/`` is a violation, full stop,
    which is also the rule ``docs/contributing.md`` already states.

    Writes stay out of scope deliberately: that is where the ~1670 live, and a
    bare write of an ASCII fixture cannot fail. The one fixture in the suite
    that writes non-ASCII names its codec.
    """
    offenders = []
    for path in sorted(TESTS_DIR.rglob("*.py")):
        for lineno, call in encoding_violations(path, kinds=("read",)):
            offenders.append(
                f"{path.relative_to(ROOT)}:{lineno}: {call}() without encoding=")
    assert not offenders, (
        "a test reading a file without encoding= decodes with "
        "locale.getpreferredencoding() — cp1252 on Windows, ASCII under a C "
        "locale — so it passes here and dies on the Windows leg the moment the "
        "file it reads acquires an em dash. Pass encoding=\"utf-8\":\n  "
        + "\n  ".join(offenders)
    )


def test_the_tests_scan_catches_a_read_and_spares_the_rest(tmp_path: Path) -> None:
    """Guards the scan above from passing because it stopped scanning.

    Pins both directions on one fixture file: the two bare reads are reported
    with their line numbers, and the codec-naming read, the binary read and the
    bare write are not — the last of those is what keeps the ~1670 fixture
    writes out of the enumeration.
    """
    fixture = tmp_path / "test_fake_repo_read.py"
    fixture.write_text(
        "from pathlib import Path\n"
        "ROOT = Path(__file__).resolve().parent.parent\n"
        "def test_reads_a_repo_file(tmp_path):\n"
        "    a = (ROOT / 'README.md').read_text()\n"
        "    b = open(ROOT / 'README.md').read()\n"
        "    c = (ROOT / 'README.md').read_text(encoding='utf-8')\n"
        "    d = open(ROOT / 'x.bin', 'rb').read()\n"
        "    (tmp_path / 'out.txt').write_text('ascii')\n",
        encoding="utf-8",
    )
    assert encoding_violations(fixture, kinds=("read",)) == [
        (4, "read_text"), (5, "open"),
    ]


def test_a_call_naming_a_keyword_open_does_not_take_is_not_an_open(tmp_path: Path) -> None:
    """`.open()` on something that is not a path is not a text-file open (#766).

    The scan reads every `X.open(...)` as `Path.open` with one carve-out for
    `os.open`, so `OPENER.open(req, timeout=timeout)` in `presets/_http.py` --
    no file, no codec, no I/O -- was reported as `open() without encoding=`.
    That is this repo's own defect class pointed at itself: a confident finding
    about a property that was never checked.

    The discriminator is not a heuristic. A call passing a keyword that neither
    `open` nor `Path.open` declares cannot be either of them; `timeout=` is in
    neither signature. Every keyword they *do* declare stays flagged, so the
    escape hatch cannot be widened by accident, and `**kwargs` -- where the
    names are unknown at parse time -- stays flagged too, on the same rule the
    non-literal mode already follows: the unknown answer is the one that flags.
    """
    fixture = tmp_path / "test_open_lookalikes.py"
    fixture.write_text(
        "import urllib.request\n"
        "from pathlib import Path\n"
        "def f(p, req, opener, kw):\n"
        "    a = opener.open(req, timeout=30)\n"
        "    b = urllib.request.build_opener().open(req, timeout=30)\n"
        "    c = p.open()\n"
        "    d = p.open(buffering=1)\n"
        "    e = open(p, closefd=True)\n"
        "    g = p.open(**kw)\n",
        encoding="utf-8",
    )
    assert encoding_violations(fixture) == [(6, "open"), (7, "open"), (8, "open"), (9, "open")]


# ── the subprocess half (#501) ────────────────────────────────────────────
#
# `subprocess.run(..., text=True)` decodes the child's stdout/stderr with
# `subprocess._text_encoding()` — the locale codec — and the **strict** error
# handler. Two defects in one call, and the same edit cures both:
#
# * strict decode kills the op on the first byte that is not valid UTF-8.
#   #498 was the live one: `git merge-tree` writes conflicting blob content to
#   stdout, a conflicted PNG put an 0x89 on the stream, and `gl-mr` died after
#   having already printed half its answer.
# * no `encoding=` means the codec comes from the locale, so a C-locale runner
#   mangles accented paths, branch names and commit messages even when nothing
#   crashes. That is #418's defect at a seam #418's scan never covered.
#
# #461 listed this class as "~270 sites, runtime-only detection" and that
# framing is what kept it unfixed: `text=True` with no `errors=` is a purely
# syntactic property of the call site, and the AST already parsed for the read
# half enumerates every one of them without executing anything.

_SUBPROCESS_RUNNERS = frozenset({"run", "Popen", "check_output", "check_call", "call"})

# Kwargs whose *value* the rule reads. A non-literal for any of them makes the
# call unjudgeable rather than clean — see `subprocess_encoding_violations`.
_DECODE_KWARGS = ("text", "universal_newlines", "encoding", "errors")


def _kwarg(node: ast.Call, name: str):
    """The kwarg's AST value node, or ``None`` when the call does not pass it.

    ``x=None`` is reported as absent on purpose: ``encoding=None`` is exactly
    what the default does, so a call spelling it out is not pinning anything.
    """
    for kw in node.keywords:
        if kw.arg == name and not (isinstance(kw.value, ast.Constant)
                                   and kw.value.value is None):
            return kw.value
    return None


def _runner_name(node: ast.Call, aliases: "Dict[str, str]" = MappingProxyType({})) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return aliases.get(func.id, func.id)
    return ""


def _runner_aliases(tree: ast.Module) -> "Dict[str, str]":
    """Names bound to a ``subprocess`` runner by assignment or aliased import.

    The rule keys on the *called name*, so ``_REAL_POPEN(...)`` is invisible to
    it — and #862 is where that stopped being hypothetical. ``tests/`` holds 21
    such bindings, almost all of them the monkeypatch idiom that saves the real
    callable before replacing it, and one of them was a genuine unpinned
    ``Popen(..., text=True)`` that the scan walked straight past.

    Only module-level and function-level ``name = subprocess.X`` assignments and
    ``from subprocess import X as name`` imports are resolved. A binding built
    through ``getattr(subprocess, name)`` still is not, which is stated rather
    than fixed because there are none and a rule nobody exercises rots.
    """
    found: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Attribute):
            value = node.value
            if (value.attr in _SUBPROCESS_RUNNERS
                    and isinstance(value.value, ast.Name)
                    and value.value.id == "subprocess"):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        found[target.id] = value.attr
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for alias in node.names:
                if alias.name in _SUBPROCESS_RUNNERS:
                    found[alias.asname or alias.name] = alias.name
    return found


def subprocess_encoding_violations(
    path: Path,
) -> Tuple[List[Tuple[int, str, str]], List[Tuple[int, str, str]]]:
    """``(violations, undecidable)`` for the subprocess calls in ``path``.

    Three states, not two — the pattern ``docs/validators.md`` settles under
    "Declining instead of guessing". A call is a **violation** when it provably
    decodes (``text=True``, ``universal_newlines=True`` or any ``encoding=``)
    and leaves ``encoding=`` or ``errors=`` to the default. It is **clean**
    when it provably does not decode, or pins both. And it is **undecidable**
    when the kwargs are not literals the parser can read — ``**kwargs``
    forwarding, or ``text=some_flag``. Those are returned separately and
    asserted on separately, because a scan that silently counted them as clean
    would be a scan whose green means less than it looks like it means.

    An **aliased** runner — ``_REAL_POPEN = subprocess.Popen``, or
    ``from subprocess import run as _r`` — is resolved by ``_runner_aliases``
    and judged like any other call, but only when the call is one the rule can
    read. A trampoline that forwards ``*args, **kwargs`` to the saved original
    is the monkeypatch idiom, not a call site: it originates no kwargs of its
    own, and the call it stands in for is judged where that call is written.
    Flagging those would add 18 waivers for one real defect, and a guard with
    18 waivers is a guard somebody switches off.

    What it still cannot see, stated so the green is read correctly:

    * a call built through ``getattr(subprocess, name)``. None today.

    ``tests/`` was out of this scan's scope until #862, on the argument that a
    test decoding its own fixture output fails loudly on a runner rather than
    silently in a user's hands. #856 is the counter-example: it failed loudly,
    on four Windows legs at once, as a ``TypeError`` about ``None`` that named
    nothing — the decode died in ``subprocess``'s reader thread and the
    traceback identifying it was demoted to a warning. Loud is not the same as
    legible, and a red default branch blocked a release either way. Both trees
    are held to one rule now.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliases = _runner_aliases(tree)
    violations: List[Tuple[int, str, str]] = []
    undecidable: List[Tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        aliased = isinstance(node.func, ast.Name) and node.func.id in aliases
        name = _runner_name(node, aliases)
        if name not in _SUBPROCESS_RUNNERS:
            continue
        if any(kw.arg is None for kw in node.keywords):
            if aliased:
                # A monkeypatch trampoline forwarding its caller's kwargs.
                continue
            undecidable.append(
                (node.lineno, name, "**kwargs may carry text=/encoding=/errors="))
            continue
        values = {key: _kwarg(node, key) for key in _DECODE_KWARGS}
        opaque = [key for key in _DECODE_KWARGS
                  if values[key] is not None
                  and not isinstance(values[key], ast.Constant)]
        if opaque:
            undecidable.append(
                (node.lineno, name, f"{'/'.join(opaque)}= is not a literal"))
            continue
        decodes = (
            any(isinstance(values[k], ast.Constant) and values[k].value is True
                for k in ("text", "universal_newlines"))
            or values["encoding"] is not None
        )
        if not decodes:
            continue
        missing = [k for k in ("encoding", "errors") if values[k] is None]
        if missing:
            violations.append((node.lineno, name, " and ".join(missing)))
    return violations, undecidable


def test_the_subprocess_scan_catches_a_bare_text_call_and_spares_the_rest(
    tmp_path: Path,
) -> None:
    """Guards the two enumerators below from passing because they stopped looking.

    One fixture pins all three states at once, with line numbers, so a rule
    that returned ``([], [])`` — or that flagged everything — fails here.
    Note the two half-fixes: ``errors=`` alone is still a violation, because
    the codec is still the locale's, and ``encoding=`` alone is still a
    violation, because the handler is still strict. Both halves or neither.

    The last four lines pin the alias rule from #862, in both directions: a
    direct call through a saved runner is judged and reported under the runner's
    real name, and the trampoline forwarding ``**kw`` to the same alias is not —
    it originates no kwargs, and flagging it would have cost 18 waivers in
    ``tests/`` to catch the one real defect on the line above it.
    """
    fixture = tmp_path / "sample.py"
    fixture.write_text(
        "import subprocess\n"
        "def go(cmd, flag, opts):\n"
        "    subprocess.run(cmd, text=True)\n"
        "    subprocess.run(cmd, universal_newlines=True)\n"
        "    subprocess.check_output(cmd, encoding='utf-8')\n"
        "    subprocess.run(cmd, text=True, errors='replace')\n"
        "    subprocess.run(cmd, encoding='utf-8', errors='replace')\n"
        "    subprocess.run(cmd, capture_output=True)\n"
        "    subprocess.run(cmd, text=True, encoding=None)\n"
        "    subprocess.run(cmd, **opts)\n"
        "    subprocess.run(cmd, text=flag)\n"
        "    print(cmd)\n"
        "_REAL_POPEN = subprocess.Popen\n"
        "def patched(cmd, **kw):\n"
        "    _REAL_POPEN(cmd, text=True)\n"
        "    return _REAL_POPEN(cmd, **kw)\n",
        encoding="utf-8",
    )
    violations, undecidable = subprocess_encoding_violations(fixture)
    assert violations == [
        (3, "run", "encoding and errors"),
        (4, "run", "encoding and errors"),
        (5, "check_output", "errors"),
        (6, "run", "encoding"),
        (9, "run", "encoding and errors"),
        (15, "Popen", "encoding and errors"),
    ]
    assert undecidable == [
        (10, "run", "**kwargs may carry text=/encoding=/errors="),
        (11, "run", "text= is not a literal"),
    ]


def test_no_shipped_subprocess_decodes_by_locale() -> None:
    """The enumerator. Every decoding subprocess call in shipped code is pinned.

    ``encoding="utf-8", errors="replace"`` is the default answer: git, gh and
    glab write UTF-8 regardless of ``LANG``, and where the payload is somebody
    else's bytes — a blob, a CI log, a commit message — mojibake beats a
    traceback that lands after half the answer is already on screen.

    It is not the answer everywhere, and the exceptions are the substance of
    #501: where the decoded text is *written back to disk* or *turned into a
    filesystem path*, ``errors="replace"`` converts a crash into a wrong
    answer, so those sites decode with ``replace`` and then refuse on U+FFFD
    instead of proceeding — the shape #498 settled on with ``_is_binary_hunk``.
    """
    offenders = []
    for path in _shipped_files():
        violations, _ = subprocess_encoding_violations(path)
        for lineno, call, missing in violations:
            offenders.append(
                f"{path.relative_to(ROOT)}:{lineno}: subprocess {call}() decodes "
                f"text but leaves {missing} to the default")
    assert not offenders, (
        "these calls decode the child's output with the locale codec and the "
        "strict error handler — so they mangle accented paths under a C locale "
        "and die outright on the first byte of a binary blob (#498). Pass "
        'encoding="utf-8", errors="replace", or drop text=/encoding= and '
        "handle bytes:\n  " + "\n  ".join(offenders)
    )


def test_the_subprocess_scan_declines_rather_than_guessing() -> None:
    """The honesty half. A call the rule cannot read is not a call that passed.

    ``subprocess.run(cmd, **opts)`` and ``text=some_flag`` are invisible to a
    syntactic rule, and the tempting move — treat unreadable as clean — is how
    a scan ends up looking exhaustive while a whole style of call site walks
    through it. So they are enumerated here instead, and the list is empty
    today: a new one turns this red and has to be answered, either by pinning
    the kwargs at the call site or by arguing in review why it cannot be.
    """
    unreadable = []
    for path in _shipped_files():
        _, undecidable = subprocess_encoding_violations(path)
        for lineno, call, why in undecidable:
            unreadable.append(f"{path.relative_to(ROOT)}:{lineno}: {call}() — {why}")
    assert not unreadable, (
        "the encoding rule cannot judge these calls, so it declines rather "
        "than passing them. Spell encoding=/errors= out literally at the call "
        "site — a forwarded **kwargs hides the one property this rule "
        "exists to enforce:\n  " + "\n  ".join(unreadable)
    )


def _test_files() -> List[Path]:
    return sorted(TESTS_DIR.rglob("*.py"))


def _subprocess_call_sites(paths: List[Path]) -> int:
    """How many ``subprocess`` runner calls the rule actually looked at.

    The enumerators below assert an empty offender list, and an empty list is
    also what a scan returns once it has quietly stopped finding anything — a
    renamed runner, a moved directory, an ``rglob`` that matches no file.
    Counting the calls seen makes the difference between the two visible.
    """
    seen = 0
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _runner_name(node) in _SUBPROCESS_RUNNERS:
                seen += 1
    return seen


def test_no_test_subprocess_decodes_by_locale() -> None:
    """The same rule inside ``tests/``. #862, generalising #856.

    Every behavioural assertion in these files passes on a UTF-8 platform
    whether or not the decoding is pinned, so this class of defect cannot be
    detected here by running the suite — only by reading the source. Which is
    what this does, and why it fails wherever it runs rather than only on the
    one platform that can see the constraint.

    The #856 trigger is worth restating because it is self-sustaining: CI
    checks out ``--depth 1``, so the only commit present is the one under
    test, and ``git-trail`` prints its *commit message*. The PR that
    introduced a Control Picture put ``\\xe2\\x90\\x9b`` in its own subject line;
    cp1252 has no mapping for ``0x90``; and a squash message is built from the
    PR title and body, so the PR documenting the fix reintroduced it.
    """
    paths = _test_files()
    seen = _subprocess_call_sites(paths)
    assert seen > 50, (
        f"the scan looked at {seen} subprocess calls in tests/ — it has drifted "
        "into matching nothing, and its green would mean nothing"
    )
    offenders = []
    for path in paths:
        violations, _ = subprocess_encoding_violations(path)
        for lineno, call, missing in violations:
            offenders.append(
                f"{path.relative_to(ROOT)}:{lineno}: subprocess {call}() decodes "
                f"text but leaves {missing} to the default")
    assert not offenders, (
        f"{len(offenders)} test call sites decode the child's output with the "
        "locale codec — cp1252 on the Windows runners, where the decode raises "
        "inside subprocess's reader thread, communicate() hands back None, and "
        "the caller's `proc.stdout + proc.stderr` fails with a TypeError that "
        'names nothing (#856). Pass encoding="utf-8", errors="replace":\n  '
        + "\n  ".join(offenders)
    )


def test_no_test_subprocess_call_is_unreadable_to_the_scan() -> None:
    """The honesty half, applied to ``tests/`` as well as to shipped code.

    A forwarded ``**kwargs`` hides the one property the rule exists to read,
    so the enumerator above would pass a whole style of call site without ever
    judging it. Listed here rather than silently counted as clean.
    """
    unreadable = []
    for path in _test_files():
        _, undecidable = subprocess_encoding_violations(path)
        for lineno, call, why in undecidable:
            unreadable.append(f"{path.relative_to(ROOT)}:{lineno}: {call}() — {why}")
    assert not unreadable, (
        "the encoding rule cannot judge these calls, so it declines rather "
        "than passing them. Spell encoding=/errors= out literally at the call "
        "site:\n  " + "\n  ".join(unreadable)
    )


# The child writes its own UTF-8 bytes instead of printing, so the demonstration
# is about the *parent's* decode and nothing else — and the parents write bytes
# back for the same reason: under an ASCII locale a bare `print` of the glyph
# would raise UnicodeEncodeError on the way out and prove the wrong thing.
_GLYPH_CHILD = (
    "import sys\n"
    "sys.stdout.buffer.write('alpha ␛ omega'.encode('utf-8'))\n"
)
_BARE_PARENT = (
    "import subprocess, sys\n"
    "r = subprocess.run([sys.executable, sys.argv[1]], capture_output=True, text=True)\n"
    "sys.stdout.buffer.write(('OK ' + r.stdout).encode('utf-8'))\n"
)
_PINNED_PARENT = (
    "import subprocess, sys\n"
    "r = subprocess.run([sys.executable, sys.argv[1]], capture_output=True, text=True,\n"
    "                   encoding='utf-8', errors='replace')\n"
    "sys.stdout.buffer.write(('OK ' + r.stdout).encode('utf-8'))\n"
)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX locale env vars")
def test_a_bare_text_call_dies_under_an_ascii_locale_and_a_pinned_one_does_not(
    tmp_path: Path,
) -> None:
    """The hazard itself, forced on a machine whose locale codec is UTF-8.

    This is the part a Mac cannot otherwise see. ``LC_ALL=C`` with PEP 538 and
    PEP 540 disabled makes ``locale.getpreferredencoding()`` ASCII, which is
    the Windows cp1252 failure in a form that reproduces here — the codec
    differs, the defect does not. The two spellings run side by side under it:
    the bare one dies on the first byte of the glyph, the pinned one does not.

    If the bare call ever stops raising, the forcing has stopped working and
    the enumerators above are the only thing still holding this seam. Worth
    knowing, which is why it is asserted rather than assumed.
    """
    child = tmp_path / "child.py"
    child.write_text(_GLYPH_CHILD, encoding="utf-8")
    env = dict(os.environ)
    env.update(ASCII_LOCALE)

    def _run_parent(source: str, name: str):
        parent = tmp_path / name
        parent.write_text(source, encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(parent), str(child)],
            capture_output=True, env=env, timeout=120)

    bare = _run_parent(_BARE_PARENT, "bare.py")
    bare_err = bare.stderr.decode("utf-8", "replace")
    assert bare.returncode != 0, bare.stdout.decode("utf-8", "replace")
    assert "UnicodeDecodeError" in bare_err, bare_err

    pinned = _run_parent(_PINNED_PARENT, "pinned.py")
    pinned_err = pinned.stderr.decode("utf-8", "replace")
    assert pinned.returncode == 0, pinned_err
    assert pinned.stdout.decode("utf-8") == "OK alpha ␛ omega", pinned_err


GIT_PRESETS = sorted(
    p for p in (ROOT / "presets" / "git").glob("*.py")
    if not p.name.startswith("_")
)


def test_every_git_preset_reconfigures_its_own_stdout() -> None:
    """The stdout half, enumerated. Every git entry point calls the one helper.

    Scoped to ``presets/git/`` on purpose. Supertool now pins
    ``PYTHONIOENCODING=utf-8`` for every child it spawns, which covers all ~50
    presets on the path that matters — so this is about the other path: the git
    scripts are the ones a developer runs straight from a shell mid-conflict,
    with no supertool in front of them, and they are where the family started
    (#308 in diff.py, #415 in commit.py). ``use_utf8_stdout()`` must be the
    first statement, because anything above it may print.
    """
    missing = []
    for path in GIT_PRESETS:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        main = next((n for n in tree.body
                     if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
        if main is None:
            continue
        first = main.body[0] if main.body else None
        called = (isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)
                  and getattr(first.value.func, "id", None) == "use_utf8_stdout")
        if not called:
            missing.append(path.name)
    assert not missing, (
        "these git presets print non-ASCII but leave stdout on the console "
        "default — a cp1252 console kills them with UnicodeEncodeError while "
        "they print their own success line, so the work lands and the receipt "
        "says it crashed. Call use_utf8_stdout() first in main(): "
        + ", ".join(missing)
    )


# ── the glyph half, past presets/git/ (#1388) ─────────────────────────────
#
# The scan above holds one directory to the rule. #1388 was filed against two
# lines in `presets/github/` that print `→` and `★` straight to stdout, and the
# census that answers "is that all of them" says no by a wide margin: **27**
# preset entry points outside `presets/git/` print a non-ASCII literal of their
# own — github, gitlab, watch, mcp, dashboard, devto and claude-log.
#
# Observed, on macOS, against a real cp1252 stream — not reasoned:
#
#     $ PYTHONIOENCODING=cp1252 python3 presets/github/starred.py 5
#     (starred 1 repos)
#     [Repository names and descriptions below come from GitHub - data, ...]
#     UnicodeEncodeError: 'charmap' codec can't encode character U+2192
#
# Two lines of output, then death: #415's shape, where the work lands and the
# receipt says it crashed.
#
# The other half of that measurement is why this rule is about the direct path
# and only the direct path. `_supertool.py` sets `PYTHONIOENCODING=utf-8` in
# `_main` before any op dispatches, so a preset spawned as an op has a UTF-8
# stdout whatever the console codepage is. The same command through
# `supertool 'gh-starred:5'`, under the same cp1252 parent environment,
# completes and renders the arrow. So the exposure is exactly the one
# `presets/git/` was given `use_utf8_stdout()` for (#308, #415): a script a
# human runs straight from a shell, with no supertool in front of it.
#
# Which codepage decides which files die, so neither number is *the* number:
#
# * **cp1252** encodes `—`, `…`, `•` and `·`, so only `→` (starred.py),
#   `★` (find_starable.py) and `↳` (mcp/status.py) raise there — three, where
#   #1388 named two.
# * **cp437 / cp850**, the actual default of a US Windows console, encode none
#   of the em dashes, so all 27 raise.
#
# **No exemption route, and that is the design rather than an omission.** #1388
# asked for a per-literal rule — a glyph must go through a degrading writer or
# declare an exemption — and worried, correctly, that a loose exemption makes
# the zero meaningless. It is the per-literal framing that does not survive:
# there are 325 non-ASCII print literals in shipped code; `_untrusted` exposes
# no writer for a glyph the *tool* wrote, since `flat`/`fence`/`scrub` mark
# somebody else's text and would be a lie applied to an arrow this repo chose;
# and the property that decides whether a `print` survives is not a property of
# the literal at all. It is `sys.stdout.encoding`, settled once per process. A
# rule at that granularity needs no exemption, because there is nothing an entry
# point could state that would make a raising `print()` acceptable, and the
# precondition costs one line.
#
# What it does change is stated here rather than hidden: after
# `use_utf8_stdout()`, `_untrusted._stream()` reads `utf-8` and prints the
# guillemet markers instead of the `<|`/`|>` fallback #863 added. That is the
# trade `_untrusted`'s own docstring argues against — on a genuinely cp437
# console those are mojibake. Accepted here, for two reasons: it is already what
# every supertool-spawned preset does, because `PYTHONIOENCODING=utf-8` reaches
# `_stream()` by the same road; and the alternative on this path is not a
# degraded marker but a process that prints a correctly-degraded banner and then
# dies four lines later, which is worth nothing to the reader the banner exists
# for.


def preset_entry_points() -> List[Path]:
    """Every preset script that is an op's entry point.

    Leading-underscore files are the shared modules the entry points import;
    they print nothing on their own behalf and have no `main()` to pin.
    """
    return sorted(p for p in (ROOT / "presets").rglob("*.py")
                  if not p.name.startswith("_"))


def non_ascii_print_literals(tree: ast.Module) -> List[Tuple[int, str]]:
    """``(lineno, glyphs)`` for every non-ASCII string literal reaching `print`.

    Literals only — an interpolated value is somebody else's text, which is
    `_untrusted`'s question and not answerable syntactically anyway. Parsed
    rather than grepped so a docstring *about* an em dash is not an instance of
    one, the same reasoning `encoding_violations` above is built on.
    """
    found: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "print"):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                glyphs = sorted({c for c in sub.value if ord(c) > 127})
                if glyphs:
                    found.append((sub.lineno, "".join(glyphs)))
    return sorted(found)


def stdout_pin_state(tree: ast.Module) -> str:
    """``"pinned"``, ``"unpinned"`` or ``"unreadable"`` for one entry point.

    Three states, and the third is the reason this is not just a copy of the
    git scan: that one ``continue``s past a module with no top-level ``main``,
    so such a file reads as compliant and is counted by nothing. Ten preset
    entry points are in that shape today — the watch pollers, the tier
    snapshotters, `transport.py` — and every one of them happens to print no
    non-ASCII literal, which is the only reason it has never bitten. An empty
    set nobody enumerates is indistinguishable from a rule that stopped looking.
    """
    main = next((n for n in tree.body
                 if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    if main is None or not main.body:
        return "unreadable"
    first = main.body[0]
    if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)
            and getattr(first.value.func, "id", None) == "use_utf8_stdout"):
        return "pinned"
    return "unpinned"


def computed_print_sites(tree: ast.Module) -> List[int]:
    """Line numbers of `print()` calls whose argument is not a plain literal.

    `non_ascii_print_literals` can only judge a string literal by inspection --
    a `Constant` node it can read the characters of. Anything else reaching
    `print` -- a name, a call, an f-string's interpolated half -- is opaque at
    parse time: the census cannot say whether it carries a codepoint the
    console cannot encode. #2065 is exactly this shape: `print(run(text))` in
    `presets/classify/check.py` carried U+27E8 from a matched fence glyph at
    runtime, no literal anywhere in the call, and both the literal scan and
    `_untrusted` (which only covers interpolation, #2065's own reading) had
    nothing to say about it -- which read as nothing being wrong.

    Deliberately wider than #2065's own crash: an f-string's literal segments
    are walked by `non_ascii_print_literals` (they are `Constant` nodes inside
    the `JoinedStr`), but the f-string as a whole is not itself a `Constant`,
    so `print(f'{x} y')` is computed here even though it carries no non-ASCII
    literal segment of its own -- the interpolated value is exactly the part
    neither guard can read.
    """
    sites: List[int] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "print"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                continue
            sites.append(node.lineno)
            break
    return sites


def _glyph_census() -> List[Tuple[Path, List[Tuple[int, str]], str]]:
    rows = []
    for path in preset_entry_points():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rows.append((path, non_ascii_print_literals(tree), stdout_pin_state(tree)))
    return rows


def test_every_preset_printing_a_non_ascii_literal_pins_its_stdout() -> None:
    """The enumerator (#1388). One line at the top of `main()`, everywhere."""
    offenders = []
    for path, literals, state in _glyph_census():
        if literals and state == "unpinned":
            lineno, glyphs = literals[0]
            offenders.append(
                f"{path.relative_to(ROOT)}:{lineno}: prints {glyphs} "
                f"({len(literals)} literal site(s)) with stdout left on the "
                f"console default")
    assert not offenders, (
        f"{len(offenders)} preset entry points print a glyph of their own and "
        "leave stdout on the console default. Run straight from a shell on a "
        "cp437/cp850/cp1252 console — no supertool in front to pin "
        "PYTHONIOENCODING — the process dies with UnicodeEncodeError partway "
        "through its own output, so the work lands and the receipt says it "
        "crashed (#415, #1388). Call use_utf8_stdout() as the first statement "
        "of main():\n  " + "\n  ".join(offenders)
    )


def test_the_glyph_scan_declines_rather_than_guessing() -> None:
    """The honesty half. An entry point the rule cannot read is not a pass.

    A preset with no module-level ``main()`` — a dispatcher called some other
    way, a script doing its work at import time — cannot be judged by reading
    ``main.body[0]``. Counting those as clean is how a scan looks exhaustive
    while a whole shape of file walks through it. Empty today; a new one turns
    this red and has to be answered rather than absorbed.
    """
    unreadable = [
        f"{path.relative_to(ROOT)}:{literals[0][0]}: prints {literals[0][1]} "
        f"but has no main() whose first statement the rule can read"
        for path, literals, state in _glyph_census()
        if literals and state == "unreadable"
    ]
    assert not unreadable, (
        "these entry points print a glyph and the stdout rule cannot judge "
        "them, so it declines rather than passing them:\n  "
        + "\n  ".join(unreadable)
    )


def test_the_glyph_scan_is_still_looking() -> None:
    """An empty offender list is also what a scan returns once it stopped.

    Both floors matter and they fail differently: the first catches an
    ``rglob`` that matches nothing after a directory move, the second catches a
    literal predicate that has quietly stopped recognising anything — which
    would leave the enumerator above green with every preset unpinned.
    """
    census = _glyph_census()
    assert len(census) > 80, (
        f"the scan found {len(census)} preset entry points — it has drifted "
        "into matching almost nothing, and its green would mean nothing")
    with_glyphs = [row for row in census if row[1]]
    assert len(with_glyphs) > 30, (
        f"only {len(with_glyphs)} entry points were seen to print a non-ASCII "
        "literal, against 40 when this was written — the literal predicate has "
        "stopped recognising what it is for")


def test_the_glyph_scan_reads_all_three_states(tmp_path: Path) -> None:
    """Guards the three tests above from passing because they stopped looking.

    One fixture per state, plus the two shapes that must NOT be reported: an
    ASCII arrow, and an f-string whose only non-ASCII could come from an
    interpolated value the parser cannot see.
    """
    cases = {
        "pinned.py": "def main():\n    use_utf8_stdout()\n    print('a → b')\n",
        "unpinned.py": "def main():\n    print('a → b')\n",
        "ascii_only.py": "def main():\n    print('a -> b')\n",
        "no_main.py": "print('a → b')\n",
        "interpolated.py": "def main():\n    print(f'{x} y')\n",
    }
    parsed = {}
    for name, src in cases.items():
        target = tmp_path / name
        target.write_text(src, encoding="utf-8")
        parsed[name] = ast.parse(target.read_text(encoding="utf-8"))

    assert non_ascii_print_literals(parsed["unpinned.py"]) == [(2, "→")]
    assert non_ascii_print_literals(parsed["pinned.py"]) == [(3, "→")]
    assert non_ascii_print_literals(parsed["no_main.py"]) == [(1, "→")]
    assert non_ascii_print_literals(parsed["ascii_only.py"]) == []
    assert non_ascii_print_literals(parsed["interpolated.py"]) == []

    assert stdout_pin_state(parsed["pinned.py"]) == "pinned"
    assert stdout_pin_state(parsed["unpinned.py"]) == "unpinned"
    assert stdout_pin_state(parsed["no_main.py"]) == "unreadable"


def test_computed_print_sites_catches_what_the_literal_scan_cedes(
    tmp_path: Path,
) -> None:
    """The positive and negative control for #2065's own detector.

    `print(run(text))` -- the shape that actually crashed -- has to be
    flagged; a plain ASCII literal and a non-ASCII literal both have to be
    spared, because both are already fully judged by `non_ascii_print_literals`
    and flagging them here too would double-count them under a different
    name. The interpolated case is the one worth stating explicitly: it
    carries no non-ASCII literal segment of its own, so the literal scan
    (correctly) spares it, and this detector (deliberately) does not -- an
    interpolated value is exactly the thing neither guard can read.
    """
    cases = {
        "computed.py": "def main():\n    print(run(text))\n",
        "literal_ascii.py": "def main():\n    print('a -> b')\n",
        "literal_non_ascii.py": "def main():\n    print('a → b')\n",
        "interpolated.py": "def main():\n    print(f'{x} y')\n",
        "no_args.py": "def main():\n    print()\n",
    }
    parsed = {}
    for name, src in cases.items():
        target = tmp_path / name
        target.write_text(src, encoding="utf-8")
        parsed[name] = ast.parse(target.read_text(encoding="utf-8"))

    assert computed_print_sites(parsed["computed.py"]) == [2]
    assert computed_print_sites(parsed["literal_ascii.py"]) == []
    assert computed_print_sites(parsed["literal_non_ascii.py"]) == []
    assert computed_print_sites(parsed["interpolated.py"]) == [2]
    assert computed_print_sites(parsed["no_args.py"]) == []


def test_the_glyph_scan_reports_what_it_cannot_verify() -> None:
    """#2065, the coverage half: `literals=[]` reads the same for a file that
    prints only ASCII and one whose print is computed and could carry
    anything at runtime -- `presets/classify/check.py` was the second shape,
    it crashed on a real console, and the census had nothing to say about it.

    Widening `test_every_preset_printing_a_non_ascii_literal_pins_its_stdout`
    itself to also fail on every computed print was measured before landing,
    not assumed: 41 of 103 preset entry points -- about 4 in 10 -- have an
    unpinned `print()` call whose argument is not a literal. That is not a
    small number to triage by hand in the same change that adds the
    detector, and #2065 names the exact risk of doing it anyway: a guard
    that fires on 4 in 10 files trains people to add the pin without
    thinking, or to silence it, which is worse than the guard that says
    nothing at all. So this stays a report rather than a second hard
    failure -- the pin for each of the 41 is a decision for that entry
    point's own author, weighing whether its computed value can plausibly
    carry non-ASCII, not a decision this test is positioned to make for all
    of them at once.

    What is asserted, so the measurement cannot silently drift back to
    unwritten: the count itself. A future preset added with an unpinned
    computed print moves this number and turns it red here, which is
    the only difference between a report and a guard nobody reads --
    update the number (and re-read the file it names) rather than papering
    over the assertion.
    """
    unpinned_with_computed = [
        path for path, literals, state in _glyph_census()
        if state == "unpinned"
        and computed_print_sites(ast.parse(path.read_text(encoding="utf-8")))
    ]
    names = sorted(str(p.relative_to(ROOT)) for p in unpinned_with_computed)
    assert len(unpinned_with_computed) == 41, (
        f"{len(unpinned_with_computed)} unpinned entry points now have a "
        "print() call whose argument is not a literal (was 41 when this was "
        "written) -- update this count if the change is deliberate, or "
        "investigate why it moved if it is not:\n  " + "\n  ".join(names)
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell shim on PATH")
def test_a_preset_run_from_a_shell_survives_a_cp1252_console(tmp_path: Path) -> None:
    """The hazard itself, on a real preset, against a real cp1252 stream (#1388).

    The scan above is syntactic and would stay green if `use_utf8_stdout()`
    stopped working. This runs `gh-starred`'s script the way the defect is
    reached — directly, no supertool, `gh` stubbed so nothing touches the
    network — and asserts the arrow does not kill it. Before the fix this exits
    1 with `UnicodeEncodeError` **after** printing two lines.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "gh"
    shim.write_text(
        "#!/bin/sh\n"
        "cat <<'JSON'\n"
        '[{"full_name":"a/b","html_url":"https://x/y","description":"d"}]\n'
        "JSON\n",
        encoding="utf-8")
    shim.chmod(0o755)

    env = dict(os.environ)
    for key in ("PYTHONIOENCODING", "PYTHONUTF8", "PYTHONCOERCECLOCALE",
                "LC_ALL", "LC_CTYPE", "LANG"):
        env.pop(key, None)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["PYTHONIOENCODING"] = "cp1252"

    result = subprocess.run(
        [sys.executable, str(ROOT / "presets" / "github" / "starred.py"), "5"],
        capture_output=True, env=env, timeout=120)
    stderr = result.stderr.decode("utf-8", "replace")
    assert "UnicodeEncodeError" not in stderr, stderr
    assert result.returncode == 0, stderr
    assert b"a/b" in result.stdout, result.stdout


def _run(args, cwd, env_overrides, extra_python_flags=()):
    env = dict(os.environ)
    for key in ("PYTHONIOENCODING", "PYTHONUTF8", "PYTHONCOERCECLOCALE",
                "LC_ALL", "LC_CTYPE", "LANG"):
        env.pop(key, None)
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, *extra_python_flags, str(SUPERTOOL), *args],
        cwd=str(cwd), capture_output=True, env=env,
    )


def _project(tmp_path: Path, config_bytes: bytes, script: str = "print('ready')") -> Path:
    """A throwaway project dir with a .supertool.json and one custom op."""
    (tmp_path / "hello.py").write_text(script, encoding="utf-8")
    (tmp_path / ".supertool.json").write_bytes(config_bytes)
    return tmp_path


# The em dash below is a literal character, so the file written to disk really
# holds multi-byte UTF-8. A "\\u2014" escape would leave it pure ASCII on disk
# and decode fine under any locale — which is how a locale pin quietly stops
# testing anything.
CONFIG_WITH_GLYPHS = (
    '{"ops": {"hello": {"cmd": "{python} hello.py",'
    ' "description": "prints a receipt — with an em dash"}}}'
).encode("utf-8")

# LC_ALL=C alone is not enough: PEP 538 coerces it to C.UTF-8 and PEP 540 would
# then hide the very defect being reproduced. Both are disabled explicitly.
ASCII_LOCALE = {
    "LC_ALL": "C", "LANG": "C",
    "PYTHONCOERCECLOCALE": "0", "PYTHONUTF8": "0",
}


@pytest.mark.skipif(sys.version_info < (3, 10),
                    reason="EncodingWarning / -X warn_default_encoding are 3.10+")
def test_running_an_op_never_relies_on_the_default_encoding(tmp_path: Path) -> None:
    """The read half, live. #431's technique applied to supertool itself.

    ``-X warn_default_encoding`` makes every text open without ``encoding=``
    raise ``EncodingWarning``; promoting it to an error turns a locale-decoded
    read into a hard failure on a Linux runner instead of a Windows-only
    ``UnicodeDecodeError`` weeks later. Against the pre-fix loader this dies in
    ``_load_config`` before the op ever dispatches.
    """
    project = _project(tmp_path, CONFIG_WITH_GLYPHS)
    result = _run(["hello"], project, {},
                  extra_python_flags=["-X", "warn_default_encoding",
                                      "-W", "error::EncodingWarning"])
    stderr = result.stderr.decode("utf-8", "replace")
    assert "EncodingWarning" not in stderr, stderr
    assert result.returncode == 0, stderr
    assert b"ready" in result.stdout


def test_a_preset_printing_glyphs_survives_a_cp1252_console(tmp_path: Path) -> None:
    """The stdout half. A child process inherits none of supertool's UTF-8.

    ``PYTHONIOENCODING=cp1252`` is the Windows console default reproduced on
    any OS. Before the fix the child dies with ``UnicodeEncodeError`` while
    writing its own success line — the #415 failure mode, where the work
    completes and the receipt says it crashed, which invites the operator to
    run it again.
    """
    project = _project(tmp_path, CONFIG_WITH_GLYPHS,
                       script="print('\\u2713 committed \\u2014 done')")
    result = _run(["hello"], project, {"PYTHONIOENCODING": "cp1252"})
    stdout = result.stdout.decode("utf-8", "replace")
    assert "UnicodeEncodeError" not in stdout + result.stderr.decode("utf-8", "replace")
    assert "PASS" in stdout, stdout
    assert "✓ committed — done" in stdout, stdout


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX locale env vars")
def test_a_utf8_config_loads_under_an_ascii_locale(tmp_path: Path) -> None:
    """A C locale is the default in a great many cron jobs and containers.

    ``.supertool.json`` is UTF-8 on disk. Read with the ASCII locale codec it
    raises ``UnicodeDecodeError``, which is a ``ValueError`` — so the loader's
    ``except (JSONDecodeError, OSError)`` does not catch it and supertool dies
    at startup with a traceback, for every op, including the ones that would
    have worked.
    """
    project = _project(tmp_path, CONFIG_WITH_GLYPHS)
    result = _run(["hello"], project, ASCII_LOCALE)
    stderr = result.stderr.decode("utf-8", "replace")
    assert "Traceback" not in stderr, stderr
    assert result.returncode == 0, stderr
    assert b"ready" in result.stdout


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX locale env vars")
def test_a_shipped_preset_loads_under_an_ascii_locale(tmp_path: Path) -> None:
    """Same defect one layer down: ``presets/git.json`` itself holds ``—``.

    A project that declares a preset therefore cannot start under a C locale
    even when its own config is pure ASCII — the undecodable file is one we
    ship, not one the user wrote.
    """
    project = _project(
        tmp_path,
        b'{"presets": ["git"], "ops": {"hello": {"cmd": "{python} hello.py"}}}',
    )
    result = _run(["hello"], project, ASCII_LOCALE)
    stderr = result.stderr.decode("utf-8", "replace")
    assert "Traceback" not in stderr, stderr
    assert result.returncode == 0, stderr
    assert b"ready" in result.stdout


def test_an_undecodable_config_is_reported_not_silently_dropped(tmp_path: Path) -> None:
    """Policy: a config that is genuinely not UTF-8 warns and is skipped.

    Two ways to lose here. A traceback blocks every op over one bad file; a
    silent skip starts fine but the user's ops are gone with nothing to explain
    it — a support question with no evidence attached. So: skip like the rest
    of the loader does with unusable state, and say so on stderr. The built-in
    ops must keep working, because they never needed the config.
    """
    project = _project(tmp_path, b'{"ops": {"hello": {"cmd": "caf\xe9"}}}')
    result = _run(["read:hello.py"], project, {})
    stderr = result.stderr.decode("utf-8", "replace")
    assert "Traceback" not in stderr, stderr
    assert result.returncode == 0, stderr
    assert b"print" in result.stdout
    assert ".supertool.json" in stderr, (
        "the skipped config must name itself on stderr — a config that "
        "silently does not apply is undiagnosable\n" + stderr
    )
