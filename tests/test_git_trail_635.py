"""git-trail caps the detail section at 10 commits — it must say so (#635).

Runs trail.py as a subprocess against a throwaway repo in tmp_path, the same
way the dispatcher runs it. No mocks: the cut is produced by real commits and
a real `git log -S`, so the test cannot pass by pinning the implementation.

Two directions, both required:
  - cut       -> the count and the cap are disclosed in the HEADER as well as
                 the footer, because the consumer that cuts the output loses
                 the footer and is exactly the reader the marker exists for.
  - uncut     -> nothing extra is printed, so that the absence of a marker is
                 a positive claim that the list is whole.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

TRAIL = Path(__file__).parent.parent / "presets" / "git" / "trail.py"
TOKEN = "WIDGET_TRAIL_TOKEN"

# (#810) `git log -S` walks every commit's tree/blob to pickaxe-search history,
# so it can surface "fatal: unable to read <sha>" for an object this fixture's
# own `git commit` calls just wrote -- seen once on a GitHub Actions ubuntu
# leg, cleared on an identical re-run, never reproduced on demand locally.
# trail.py relays that failure into its own stdout via `_format_error` and
# exits 1 -- it never invents a sha, only relays what git's log already
# returned -- so the signature is specific enough that retrying past it cannot
# hide a real trail.py regression: a genuine bug in this code does not print
# this string.
_UNREADABLE_OBJECT_RE = re.compile(r"unable to read [0-9a-f]{40}")


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "config", "user.email", "t@t.invalid"], check=True, cwd=path)
    subprocess.run(["git", "config", "user.name", "T"], check=True, cwd=path)


def _commits_touching(path: Path, n: int) -> None:
    """n commits that each change the occurrence count of TOKEN -> n pickaxe hits."""
    _init_repo(path)
    target = path / "widget.py"
    body = ""
    for i in range(n):
        body += f"{TOKEN} = {i}\n"
        target.write_text(body, encoding="utf-8")
        subprocess.run(["git", "add", "widget.py"], check=True, cwd=path)
        subprocess.run(["git", "commit", "-q", "-m", f"c{i}"], check=True, cwd=path)


def _run(
    repo: Path, *args: str, extra_env: dict[str, str] | None = None
) -> str:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if extra_env:
        env.update(extra_env)
    res = None
    for attempt in range(3):
        res = subprocess.run(
            [sys.executable, str(TRAIL), *args],
            capture_output=True, text=True, encoding="utf-8", cwd=repo, env=env,
        )
        if res.returncode == 0 or not _UNREADABLE_OBJECT_RE.search(res.stdout):
            break
        time.sleep(0.2 * (attempt + 1))
    assert res.returncode == 0, f"stdout:{chr(10)}{res.stdout}{chr(10)}stderr:{chr(10)}{res.stderr}"
    return res.stdout


def _details_header(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("## Details"):
            return line
    raise AssertionError(f"no '## Details' header in output:\n{out}")


def test_capped_detail_section_discloses_in_the_header(tmp_path: Path) -> None:
    _commits_touching(tmp_path, 14)
    out = _run(tmp_path, TOKEN)

    # The fixture really ran: 14 commits found, so the cap really bit.
    assert "## Timeline (14 commits)" in out, out

    header = _details_header(out)
    assert "10 of 14" in header, f"header does not disclose the cut: {header!r}"
    assert "SUPERTOOL_TRAIL_DETAIL_CAP" in header, header


def test_capped_detail_section_discloses_in_the_footer(tmp_path: Path) -> None:
    _commits_touching(tmp_path, 14)
    out = _run(tmp_path, TOKEN)

    assert "## Timeline (14 commits)" in out, out
    tail = out.rstrip().splitlines()[-1]
    assert "10 of 14" in tail, f"footer does not disclose the cut: {tail!r}"
    # A count cap did the cutting. Saying "size" would point at a knob that
    # does not govern it — a confidently wrong disclosure (#633).
    assert "count" in tail.lower(), tail


def test_uncut_detail_section_says_nothing_extra(tmp_path: Path) -> None:
    _commits_touching(tmp_path, 4)
    out = _run(tmp_path, TOKEN)

    assert "## Timeline (4 commits)" in out, out
    assert _details_header(out) == "## Details", out
    assert "of 4" not in out, out
    assert "SUPERTOOL_TRAIL_DETAIL_CAP" not in out, out


def test_cap_is_raisable_and_then_says_nothing(tmp_path: Path) -> None:
    _commits_touching(tmp_path, 14)
    out = _run(tmp_path, TOKEN, extra_env={"SUPERTOOL_TRAIL_DETAIL_CAP": "50"})
    assert "## Timeline (14 commits)" in out, out
    assert _details_header(out) == "## Details", out
    assert out.count("### ") == 14, out


def test_timeline_itself_discloses_when_max_commits_caps_it(tmp_path: Path) -> None:
    """The pool the detail cap draws from was silently bounded too (#635)."""
    _commits_touching(tmp_path, 14)
    out = _run(tmp_path, TOKEN, extra_env={"SUPERTOOL_MAX_COMMITS": "5"})

    timeline = next(l for l in out.splitlines() if l.startswith("## Timeline"))
    assert timeline.startswith("## Timeline (5 commits)"), timeline
    assert "more exist" in timeline, timeline
    assert "SUPERTOOL_MAX_COMMITS" in timeline, timeline
    # No invented total: the overshoot proves only that more exist.
    assert "of 14" not in out, out


def test_run_retries_past_a_transient_unreadable_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The #810 retry fires only for the exact object-store read failure."""
    calls: list[list[str]] = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        if len(calls) == 1:
            return subprocess.CompletedProcess(
                cmd, returncode=1,
                stdout="ERROR: git failed searching for %r: fatal: unable "
                       "to read %s" % (TOKEN, "a" * 40) + chr(10),
                stderr="",
            )
        return subprocess.CompletedProcess(
            cmd, returncode=0, stdout="ok" + chr(10), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr(time, "sleep", lambda _s: None)

    out = _run(tmp_path, TOKEN)

    assert out == "ok" + chr(10)
    assert len(calls) == 2, "expected exactly one retry, not a retry loop"


def test_run_does_not_retry_an_unrelated_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real trail.py bug must fail fast, not be absorbed by the #810 retry."""
    calls: list[list[str]] = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd, returncode=1,
            stdout="ERROR: not inside a git repository." + chr(10), stderr="",
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)

    with pytest.raises(AssertionError):
        _run(tmp_path, TOKEN)

    assert len(calls) == 1, "an unrelated failure must not be retried"


def test_uncapped_timeline_says_nothing_extra(tmp_path: Path) -> None:
    _commits_touching(tmp_path, 4)
    out = _run(tmp_path, TOKEN)
    timeline = next(l for l in out.splitlines() if l.startswith("## Timeline"))
    assert timeline == "## Timeline (4 commits)", timeline
    assert "SUPERTOOL_MAX_COMMITS" not in out, out
