"""#2256 -- gh-job's own em-dash literals (>=122 sites, presets/github/job.py)
survive a real cp437 console stream when the job is reached through main().

**Correction to the issue's own premise, stated here because it changes which
codec this test has to use.** The issue names cp1252 as the crashing
codepage. It is not: `'\u2014'.encode('cp1252')` succeeds (byte 0x97 -- cp1252
has an em dash at that byte) and this repo's own `tests/test_encoding_seam.py`
already documents the split -- "cp1252 encodes -, ..., -bullet- and middle
dot, so only arrow/star/hook raise there[;] cp437/cp850, the actual default
of a US Windows console, encode none of the em dashes, so all [non-ASCII
print sites] raise". Reproduced directly here:
encoding an em dash as cp1252 succeeds (byte 0x97); encoding it as cp437 raises UnicodeEncodeError. So a test built on
cp1252 cannot exercise this crash at all -- it was tried first, in an earlier
revision of this file, and it passed for the wrong reason (nothing ever
raised) rather than for the right one. cp437 is what actually reproduces
#2256's own crash shape, and cp850 shares the property; cp437 is used below
because it is this repo's own stated "actual default of a US Windows console".

Regression, not a new hazard otherwise: #1388 gave every preset entry point
printing a non-ASCII literal a `use_utf8_stdout()` call as the first
statement of `main()`, and `presets/github/job.py`'s own `main()` already has
it. This pins that the two sites #2256 names -- the download-cap-exceeded
ERROR branch and the size-unknown NOTE branch #2248 added, both carrying a
literal em dash -- actually go through that call and do not raise on a real
cp437-configured stream, rather than trusting the structural AST scan in
test_encoding_seam.py to be the whole story for this one file.

**What "survives" means here.** `use_utf8_stdout()` reconfigures the stream to
UTF-8 rather than degrading the glyph, which is `presets/_console.py`'s own
documented trade: on a genuinely cp437 console the bytes still land as
mojibake, but the process completes and reports what it actually did, instead
of dying mid-`print` after the work landed (#415's shape). So these tests
assert two things separately: the write does not raise (the crash #2256 is
about), and the content is still recoverable by decoding what was actually
written as UTF-8 (proving the mojibake, not a lost line).

**Why the console object is built inline, not returned by a fixture.**
Pytest re-resumes its own capture around each of setup/call/teardown
separately, so a `monkeypatch.setattr(sys, "stdout", ...)` done in a
*fixture* (which runs during setup) is silently overwritten again before the
*call* phase where the test body -- and `main()`'s prints -- actually run:
`sys.stdout is stream` reads `False` inside the test body even though the
fixture just set it. Doing the same `monkeypatch.setattr` call from directly
inside the test function (during call phase) does not have this problem, so
this file uses a plain helper rather than a fixture that returns the stream.
`tests/test_untrusted_console_encoding_863.py` already uses the same shape
for the identical reason.
"""
from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

PRESETS = Path(__file__).parent.parent / "presets"

#: This repo's own documented "actual default of a US Windows console"
#: (tests/test_encoding_seam.py) -- unlike cp1252, it has no mapping for an
#: em dash at all, so it is what actually reproduces #2256's crash.
_CONSOLE_CODEPAGE = "cp437"


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, PRESETS / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gh_job = _load("github/job.py", "github_job_2256")

_REAL_RUN = subprocess.run
GH_ID = "92792057296"


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _fake_gh(job_meta: dict, run_artifacts: list, zips: dict[int, bytes]):
    def fake_run(args: list[str], **kw: Any):
        if args and args[0] == "git":
            return _REAL_RUN(args, **kw)
        assert args and args[0] == "gh", f"unstubbed command: {args!r}"
        joined = " ".join(str(a) for a in args)
        if "actions/jobs/" in joined and "logs" not in joined:
            return subprocess.CompletedProcess(args, 0, json.dumps(job_meta), "")
        if "/artifacts?" in joined or joined.endswith("/artifacts"):
            return subprocess.CompletedProcess(
                args, 0, json.dumps({"artifacts": run_artifacts}), "")
        if "/zip" in joined:
            for artifact_id, data in zips.items():
                if f"artifacts/{artifact_id}/zip" in joined:
                    return subprocess.CompletedProcess(args, 0, data, b"")
            return subprocess.CompletedProcess(args, 1, b"", b"404 Not Found")
        raise AssertionError(f"unstubbed gh call: {args!r}")
    return fake_run


class _Cp437Console:
    """A real io.TextIOWrapper on a real cp437 codec -- not a mock.

    `reconfigure` is the same method a genuine Windows console-backed
    stdout exposes, so `use_utf8_stdout()` either fixes this the same way
    it fixes a live console, or the write below raises exactly the
    UnicodeEncodeError #2256 describes.
    """

    def __init__(self) -> None:
        self.buffer = io.BytesIO()
        self.stream = io.TextIOWrapper(
            self.buffer, encoding=_CONSOLE_CODEPAGE, errors="strict", newline="")

    def written_utf8(self) -> str:
        """What actually landed on the wire, read back as UTF-8.

        Not `cp437` -- after `use_utf8_stdout()` reconfigures the stream,
        the bytes on disk *are* UTF-8, and decoding them as `cp437` (the
        console's own codepage) is exactly the mojibake `_console.py`
        documents as the accepted trade, not a bug in this test.
        """
        self.stream.flush()
        return self.buffer.getvalue().decode("utf-8")


def _install_cp437_console(monkeypatch: pytest.MonkeyPatch) -> _Cp437Console:
    console = _Cp437Console()
    monkeypatch.setattr(sys, "stdout", console.stream)
    monkeypatch.setattr(sys, "stderr", console.stream)
    return console


def test_gh_job_artifact_cap_exceeded_em_dash_survives_a_cp437_console(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ERROR branch (pre-existing, line ~1123): archive over the download
    cap prints an em dash. Before #1388's use_utf8_stdout() fix this raised
    UnicodeEncodeError on a genuine cp437 stream, after printing nothing of
    the ERROR line -- the crash lands mid-`print`, not after it."""
    console = _install_cp437_console(monkeypatch)
    zip_data = _zip_bytes({"a.txt": b"CONTENT"})
    artifacts = [{"id": 1, "name": "huge", "size_in_bytes": 999_999_999}]
    monkeypatch.setattr(sys, "argv", ["job.py", GH_ID, "artifact", "a.txt"])
    monkeypatch.setattr(gh_job.subprocess, "run",
                        _fake_gh({"run_id": "555"}, artifacts, {1: zip_data}))
    monkeypatch.setenv("GH_JOB_ARTIFACT_DOWNLOAD_MAX_BYTES", "1000")

    rc = gh_job.main()  # must not raise UnicodeEncodeError

    rendered = console.written_utf8()
    assert rc == 1, rendered
    assert "ERROR" in rendered and "download cap" in rendered, rendered
    assert "—" in rendered, "the em dash itself must have been written: " + repr(rendered)


def test_gh_job_artifact_size_unknown_em_dash_survives_a_cp437_console(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The NOTE branch #2248 added: size_in_bytes missing prints an em dash
    disclosure and then still downloads. Same crash shape as the ERROR
    branch, on the newer of the two sites #2256 names."""
    console = _install_cp437_console(monkeypatch)
    zip_data = _zip_bytes({"a.txt": b"CONTENT"})
    artifacts = [{"id": 1, "name": "unsized"}]  # no size_in_bytes
    monkeypatch.setattr(sys, "argv", ["job.py", GH_ID, "artifact", "a.txt"])
    monkeypatch.setattr(gh_job.subprocess, "run",
                        _fake_gh({"run_id": "555"}, artifacts, {1: zip_data}))

    rc = gh_job.main()  # must not raise UnicodeEncodeError

    rendered = console.written_utf8()
    assert rc == 0, rendered
    assert "CONTENT" in rendered, rendered
    assert "—" in rendered, "the em dash itself must have been written: " + repr(rendered)
