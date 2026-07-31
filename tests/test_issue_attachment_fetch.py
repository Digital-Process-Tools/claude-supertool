"""Attachment fetching must stay inside its output directory, and stay bounded.

The image URLs come from an issue description and from every comment on it —
text anyone can write. Three properties have to hold regardless of what that
text says:

1. a downloaded file lands inside the per-issue output directory, whatever the
   remote filename decodes to;
2. only the hosts we actually fetch attachments from are contacted;
3. the URL-extraction pass finishes in time proportional to the body, so one
   long comment cannot cost the whole op its budget.

Each test asserts the property rather than the current spelling of the fix.
"""

import subprocess
import time
from pathlib import Path

import presets.gitlab.issue as gl_issue


def _fake_glab(body: bytes = b"PNGDATA"):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, body, b"")
    return run


def test_a_percent_encoded_traversal_cannot_escape_the_output_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(gl_issue, "IMAGE_DIR", str(tmp_path / "images"))
    monkeypatch.setattr(gl_issue.subprocess, "run", _fake_glab())

    outside = tmp_path / "escaped.txt"
    # basename() sees one segment; only after decoding does it become ../../
    hostile = "/uploads/abc123/..%2F..%2Fescaped.txt"

    gl_issue._download_images([hostile], "42")

    assert not outside.exists(), "a decoded ../ escaped the per-issue directory"


def test_an_absolute_decoded_name_cannot_escape_either(tmp_path, monkeypatch):
    monkeypatch.setattr(gl_issue, "IMAGE_DIR", str(tmp_path / "images"))
    monkeypatch.setattr(gl_issue.subprocess, "run", _fake_glab())

    outside = tmp_path / "abs.txt"
    hostile = f"/uploads/abc123/%2F{outside}"

    gl_issue._download_images([hostile], "42")

    assert not outside.exists(), "an absolute decoded name discarded the output dir"


def test_everything_written_stays_under_the_issue_dir(tmp_path, monkeypatch):
    root = tmp_path / "images"
    monkeypatch.setattr(gl_issue, "IMAGE_DIR", str(root))
    monkeypatch.setattr(gl_issue.subprocess, "run", _fake_glab())

    paths = gl_issue._download_images(
        ["/uploads/abc/normal.png", "/uploads/abc/..%2Fsneaky.png"], "7"
    )

    expected_root = (root / "7").resolve()
    for p in paths:
        assert Path(p).resolve().is_relative_to(expected_root), f"{p} is outside {expected_root}"


def test_url_extraction_stays_proportional_on_a_long_body():
    """The extraction pass runs over the description and every comment.

    No super-linear growth has been reproduced against this pattern: 65 KB
    measures ~0.001s here. This is a regression pin on the growth curve, not a
    fix for an observed slowdown — the bound is deliberately loose so that a
    future pattern change cannot quietly make extraction cost the op its budget.
    """
    body = "![x](https://example.com/" + "a" * 65000 + ")"
    start = time.monotonic()
    gl_issue._extract_image_urls(body)
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, f"extraction took {elapsed:.1f}s on a 65 KB body"
