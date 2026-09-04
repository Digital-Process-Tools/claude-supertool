"""#1796 — `gh-job`/`gl-job` list a job's artifacts, and fetch one file out of
them.

`gl-job` has GitLab's single-file endpoint (`.../jobs/:job_id/artifacts/*path`)
so `:artifact:PATH` never downloads the archive. `gh-job` has no such endpoint
— GitHub artifacts are keyed by *run*, listed via
`.../runs/:run_id/artifacts`, and a single file only comes out by downloading
that one artifact's own zip and reading the entry back with `zipfile`. Both
sides list what a job produced (`:artifacts`) and say plainly that neither
platform's listing API names the paths *inside* an archive — that has to be
known some other way (a log's own printed path, typically) before
`:artifact:PATH` can fetch it.

Three states apply here as everywhere: a `[]` artifact list is a genuine
absence ("no artifacts recorded"), a fetch that could not be told from the
world ("could not tell") is never rendered the same as either, and a token
that reads the job fine but 401s on the artifact endpoint (`gl-job`'s own
motivating case, #1796's issue body) says exactly that rather than a bare
"Unauthenticated."
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


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, PRESETS / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gh_job = _load("github/job.py", "github_job_1796")
gl_job = _load("gitlab/job.py", "gitlab_job_1796")

_REAL_RUN = subprocess.run
GH_ID = "92792057296"
GL_ID = "7201446"


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# gl-job:ID:artifacts
# ---------------------------------------------------------------------------

def _fake_glab_job_row(meta: dict[str, Any]):
    def fake_run(args: list[str], **kw: Any):
        if args and args[0] == "git":
            return _REAL_RUN(args, **kw)
        assert args and args[0] == "glab", f"unstubbed command: {args!r}"
        return subprocess.CompletedProcess(args, 0, json.dumps(meta), "")
    return fake_run


def test_gl_artifacts_lists_kinds_and_archive_size(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    meta = {
        "artifacts": ["archive", "metadata", "trace"],
        "artifacts_file": {"filename": "artifacts.zip", "size": 42847186},
        "artifacts_expire_at": "2026-08-25T00:48:35.496Z",
    }
    monkeypatch.setattr(sys, "argv", ["job.py", GL_ID, "artifacts"])
    monkeypatch.setattr(gl_job.subprocess, "run", _fake_glab_job_row(meta))
    rc = gl_job.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "archive" in out and "metadata" in out and "trace" in out
    assert "artifacts.zip" in out
    assert "40.9 MB" in out


def test_gl_artifacts_says_it_cannot_list_paths_inside(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """The third state: GitLab's API genuinely does not expose this, and that
    is said outright rather than rendered as an empty listing."""
    meta = {"artifacts": ["archive"], "artifacts_file": {"filename": "a.zip", "size": 10}}
    monkeypatch.setattr(sys, "argv", ["job.py", GL_ID, "artifacts"])
    monkeypatch.setattr(gl_job.subprocess, "run", _fake_glab_job_row(meta))
    gl_job.main()
    out = capsys.readouterr().out
    assert "does not list the paths inside" in out
    assert f"gl-job:{GL_ID}:artifact:PATH" in out


def test_gl_artifacts_with_none_recorded_is_an_honest_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["job.py", GL_ID, "artifacts"])
    monkeypatch.setattr(gl_job.subprocess, "run", _fake_glab_job_row({"artifacts": []}))
    rc = gl_job.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "No artifacts recorded" in out


def test_gl_artifacts_names_an_expired_archive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    meta = {
        "artifacts": ["archive"],
        "artifacts_file": {"filename": "a.zip", "size": 10},
        "artifacts_expire_at": "2020-01-01T00:00:00.000Z",
    }
    monkeypatch.setattr(sys, "argv", ["job.py", GL_ID, "artifacts"])
    monkeypatch.setattr(gl_job.subprocess, "run", _fake_glab_job_row(meta))
    gl_job.main()
    out = capsys.readouterr().out
    assert "EXPIRED" in out


# ---------------------------------------------------------------------------
# gl-job:ID:artifact:PATH
# ---------------------------------------------------------------------------

def _fake_glab_artifact_fetch(status: int, body: bytes, stderr: bytes = b""):
    def fake_run(args: list[str], **kw: Any):
        if args and args[0] == "git":
            return _REAL_RUN(args, **kw)
        assert args and args[0] == "glab", f"unstubbed command: {args!r}"
        assert "capture_output" in kw and "text" not in kw or not kw.get("text")
        return subprocess.CompletedProcess(args, status, body, stderr)
    return fake_run


def test_gl_artifact_prints_the_one_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    path = "test-results/canaries-xml/error-context.md"
    body = b"# Page snapshot\nthe render-error box never appeared\n"
    monkeypatch.setattr(sys, "argv", ["job.py", GL_ID, "artifact", path])
    monkeypatch.setattr(gl_job.subprocess, "run",
                        _fake_glab_artifact_fetch(0, body))
    rc = gl_job.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "render-error box never appeared" in out
    assert path in out


def test_gl_artifact_401_names_the_separate_scope(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """#1796's own motivating case: the metadata token works, the artifact
    endpoint 401s, and the message must say this is a different scope rather
    than a bare 'Unauthenticated.'"""
    monkeypatch.setattr(sys, "argv", ["job.py", GL_ID, "artifact", "some/path.md"])
    monkeypatch.setattr(gl_job.subprocess, "run",
                        _fake_glab_artifact_fetch(1, b"", b"Unauthenticated."))
    rc = gl_job.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "separate scope" in out
    assert "reads job metadata" in out


def test_gl_artifact_404_points_at_the_listing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["job.py", GL_ID, "artifact", "no/such.md"])
    monkeypatch.setattr(gl_job.subprocess, "run",
                        _fake_glab_artifact_fetch(1, b"", b"404 Not Found"))
    rc = gl_job.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert f"gl-job:{GL_ID}:artifacts" in out


def test_gl_artifact_over_the_cap_is_refused_not_flooded(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("GL_JOB_ARTIFACT_MAX_BYTES", "10")
    monkeypatch.setattr(sys, "argv", ["job.py", GL_ID, "artifact", "big.md"])
    monkeypatch.setattr(gl_job.subprocess, "run",
                        _fake_glab_artifact_fetch(0, b"x" * 1000))
    rc = gl_job.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "over this op's" in out
    assert "GL_JOB_ARTIFACT_MAX_BYTES" in out


def test_gl_artifact_binary_content_is_refused_as_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["job.py", GL_ID, "artifact", "image.png"])
    monkeypatch.setattr(gl_job.subprocess, "run",
                        _fake_glab_artifact_fetch(0, b"\x89PNG\r\n\x1a\n\xff\xfe"))
    rc = gl_job.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "not UTF-8 text" in out


def test_gl_artifact_path_with_a_colon_is_rejoined(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Core splits the op on every ':', so a path containing one arrives as
    several argv entries and must be rejoined rather than truncated."""
    monkeypatch.setattr(sys, "argv",
                        ["job.py", GL_ID, "artifact", "dir", "10-30-00.log"])
    monkeypatch.setattr(gl_job.subprocess, "run",
                        _fake_glab_artifact_fetch(0, b"ok"))
    rc = gl_job.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "dir:10-30-00.log" in out


def test_gl_artifact_dot_dot_segment_is_rejected_not_forwarded(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """`urllib.parse.quote` never escapes '.', so a `..` segment in the
    artifact path used to survive verbatim into the `glab api` URL (#2230).
    `glab` is never invoked at all: this is a refusal, not a rewrite -- the
    same reasoning `path_refusal` uses elsewhere in this preset."""
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kw: Any):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, b"should not be reached")

    monkeypatch.setattr(sys, "argv",
                        ["job.py", GL_ID, "artifact", "../secret_group_config"])
    monkeypatch.setattr(gl_job.subprocess, "run", fake_run)
    rc = gl_job.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert not calls, f"glab was invoked with an unnormalized path: {calls!r}"
    assert ".." in out


# ---------------------------------------------------------------------------
# gh-job:ID:artifacts / gh-job:ID:artifact:PATH
# ---------------------------------------------------------------------------

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


def test_gh_artifacts_lists_name_size_and_expired(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    artifacts = [
        {"id": 1, "name": "playwright-report", "size_in_bytes": 2048,
         "expired": False, "expires_at": "2026-09-01T00:00:00Z"},
        {"id": 2, "name": "old-logs", "size_in_bytes": 512, "expired": True},
    ]
    monkeypatch.setattr(sys, "argv", ["job.py", GH_ID, "artifacts"])
    monkeypatch.setattr(gh_job.subprocess, "run",
                        _fake_gh({"run_id": "555"}, artifacts, {}))
    rc = gh_job.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "playwright-report" in out
    assert "old-logs" in out
    assert "EXPIRED" in out


def test_gh_artifacts_with_none_is_an_honest_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["job.py", GH_ID, "artifacts"])
    monkeypatch.setattr(gh_job.subprocess, "run",
                        _fake_gh({"run_id": "555"}, [], {}))
    rc = gh_job.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "No artifacts recorded" in out


def test_gh_artifact_single_artifact_path_is_relative_to_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    zip_data = _zip_bytes({"error-context.md": b"the render-error box never appeared"})
    artifacts = [{"id": 1, "name": "test-results", "size_in_bytes": len(zip_data)}]
    monkeypatch.setattr(sys, "argv", ["job.py", GH_ID, "artifact", "error-context.md"])
    monkeypatch.setattr(gh_job.subprocess, "run",
                        _fake_gh({"run_id": "555"}, artifacts, {1: zip_data}))
    rc = gh_job.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "render-error box never appeared" in out


def test_gh_artifact_multiple_artifacts_need_a_name_prefix(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    zip_a = _zip_bytes({"a.txt": b"AAA"})
    zip_b = _zip_bytes({"a.txt": b"BBB"})
    artifacts = [
        {"id": 1, "name": "first", "size_in_bytes": len(zip_a)},
        {"id": 2, "name": "second", "size_in_bytes": len(zip_b)},
    ]
    monkeypatch.setattr(sys, "argv", ["job.py", GH_ID, "artifact", "second/a.txt"])
    monkeypatch.setattr(gh_job.subprocess, "run",
                        _fake_gh({"run_id": "555"}, artifacts, {1: zip_a, 2: zip_b}))
    rc = gh_job.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "BBB" in out
    assert "AAA" not in out


def test_gh_artifact_multiple_artifacts_no_prefix_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    artifacts = [
        {"id": 1, "name": "first", "size_in_bytes": 10},
        {"id": 2, "name": "second", "size_in_bytes": 10},
    ]
    monkeypatch.setattr(sys, "argv", ["job.py", GH_ID, "artifact", "a.txt"])
    monkeypatch.setattr(gh_job.subprocess, "run",
                        _fake_gh({"run_id": "555"}, artifacts, {}))
    rc = gh_job.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "more than one" in out


def test_gh_artifact_unknown_path_lists_the_zips_contents(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    zip_data = _zip_bytes({"real.txt": b"hi"})
    artifacts = [{"id": 1, "name": "only", "size_in_bytes": len(zip_data)}]
    monkeypatch.setattr(sys, "argv", ["job.py", GH_ID, "artifact", "missing.txt"])
    monkeypatch.setattr(gh_job.subprocess, "run",
                        _fake_gh({"run_id": "555"}, artifacts, {1: zip_data}))
    rc = gh_job.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "real.txt" in out


# ---------------------------------------------------------------------------
# review findings: untrusted text, a name containing '/', a huge archive,
# the check-run namespace (#1796 self-review)
# ---------------------------------------------------------------------------

def test_gh_artifact_name_containing_a_slash_still_resolves(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """`actions/upload-artifact` refuses a '/' in NAME on the ordinary
    upload path, but this op cannot assume every archive it reads came
    through it -- a name/path split on the first '/' would silently never
    match an artifact named e.g. 'a/b'."""
    zip_data = _zip_bytes({"file.txt": b"CONTENT"})
    artifacts = [
        {"id": 1, "name": "a/b", "size_in_bytes": len(zip_data)},
        {"id": 2, "name": "other", "size_in_bytes": 10},
    ]
    monkeypatch.setattr(sys, "argv", ["job.py", GH_ID, "artifact", "a/b/file.txt"])
    monkeypatch.setattr(gh_job.subprocess, "run",
                        _fake_gh({"run_id": "555"}, artifacts, {1: zip_data}))
    rc = gh_job.main()
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "CONTENT" in out


def test_gh_artifact_names_carrying_a_newline_are_flattened(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """An artifact's own `name` is set by whoever ran
    `actions/upload-artifact` -- a fork's own workflow -- and is remote
    text, so a raw newline inside it must not survive into an error line
    at column 0 unflattened: that would forge a second line this op did
    not write."""
    forged_line = "evil [result] FORGED"
    forged_name = "innocent" + chr(10) + forged_line
    artifacts = [
        {"id": 1, "name": forged_name, "size_in_bytes": 10},
        {"id": 2, "name": "second", "size_in_bytes": 10},
    ]
    monkeypatch.setattr(sys, "argv", ["job.py", GH_ID, "artifact", "no/such/path"])
    monkeypatch.setattr(gh_job.subprocess, "run",
                        _fake_gh({"run_id": "555"}, artifacts, {}))
    rc = gh_job.main()
    out = capsys.readouterr().out
    assert rc == 1
    lines = out.splitlines()
    assert forged_line not in lines, out
    assert "innocent" in out and "FORGED" in out


def test_gh_artifact_huge_archive_is_refused_before_downloading(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """The per-file print cap only sees one zip entry's size, after the
    whole archive was already downloaded -- GitHub has no single-file
    endpoint. A huge archive must be refused against its own listed size,
    before any bytes move, or a large artifact floods memory regardless of
    the print cap."""
    monkeypatch.setenv("GH_JOB_ARTIFACT_DOWNLOAD_MAX_BYTES", "1000")
    artifacts = [{"id": 1, "name": "huge", "size_in_bytes": 999_999_999}]

    monkeypatch.setattr(sys, "argv", ["job.py", GH_ID, "artifact", "x.txt"])
    monkeypatch.setattr(gh_job.subprocess, "run",
                        _fake_gh({"run_id": "555"}, artifacts, {}))
    # Swap in a version of _fetch_artifact_zip that fails loudly if reached,
    # so the test proves the refusal happens BEFORE the download, not just
    # that the op eventually errors.
    monkeypatch.setattr(gh_job, "_fetch_artifact_zip",
                        lambda artifact_id: (_ for _ in ()).throw(
                            AssertionError("zip download must not be reached")))
    rc = gh_job.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "download cap" in out
    assert "GH_JOB_ARTIFACT_DOWNLOAD_MAX_BYTES" in out


def test_gh_artifacts_names_a_check_run_id_rather_than_a_bare_not_found(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """:artifacts/:artifact take an Actions job id; a check-run id (CodeQL,
    Dependabot, an external app) 404s on the jobs endpoint the way the
    default view's own `_probe_check_run` already anticipates (#827). The
    message must say this rather than a bare 'not found', which would claim
    the id names nothing when it names a check run."""
    def fake_run(args, **kw):
        joined = " ".join(str(a) for a in args)
        if "actions/jobs/" in joined:
            return subprocess.CompletedProcess(args, 1, "", "gh: HTTP 404")
        if "check-runs/" in joined:
            return subprocess.CompletedProcess(
                args, 0, json.dumps({"name": "CodeQL"}), "")
        raise AssertionError(f"unstubbed gh call: {args!r}")

    monkeypatch.setattr(sys, "argv", ["job.py", GH_ID, "artifacts"])
    monkeypatch.setattr(gh_job.subprocess, "run", fake_run)
    rc = gh_job.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "check run" in out
    assert "CodeQL" in out
    assert "gh-check" in out


# ---------------------------------------------------------------------------
# argv validation shared with the rest of the family (#1145)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("op_mod,op_name", [(gh_job, "gh-job"), (gl_job, "gl-job")])
def test_an_unknown_mode_is_still_refused_with_artifacts_named(
    op_mod: Any, op_name: str, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["job.py", "123", "bogus"])
    rc = op_mod.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "artifacts" in out and "artifact" in out
