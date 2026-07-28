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
from typing import List, Tuple

import pytest

ROOT = Path(__file__).resolve().parent.parent
SUPERTOOL = ROOT / "supertool.py"

# Shipped code — what a user runs. Held to both halves of the rule, reads and
# writes.
SHIPPED = ("supertool.py", "presets", "hooks", "validators", "formatters", "notifiers")

# `tests/` is scanned too, but for **reads only** (#461). The ~1670 `write_text()`
# fixture calls are the noise that made #418's blanket exclusion correct; the
# reads are not noise, because a test reading a real repo file as data decodes
# the project's accumulated non-ASCII with the locale codec — twice escaped to a
# Windows runner in one day. See test_no_test_file_decodes_a_read_by_locale.
TESTS_DIR = Path(__file__).resolve().parent

# Text-mode readers/writers that fall back to the locale when `encoding=` is
# omitted. `os.open` takes no encoding (raw fd) and is excluded by name below.
_PATH_TEXT_METHODS = frozenset({"read_text", "write_text"})


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
