"""#1130 - a diff's own content must not decide where `git-diff`'s review gates fall.

The `presets/git/` splitlines audit (#1130) reads 44 `str.splitlines()` call sites
and narrows two, both in `presets/git/diff.py`. They are the two that parse a
stream `git-diff` deliberately asks git NOT to quote: both readers run
`git -c core.quotepath=false`, which is what makes a U+2028 in a path or in an
added line reach the parser as raw bytes instead of as an octal escape.

Neither is a misattribution. Both SUPPRESS a review gate:

* `_scan_red_flags` keys on `+++ b/` at column 0 to know which file the added
  lines belong to, and every red-flag pattern may be scoped to an extension. An
  added line carrying `U+2028+++ b/notes.txt` retargets `cur_path`, so a
  `.py`-scoped secret pattern stops matching for every added line after it. The
  scan is turned off by the content it is scanning. This is `_pr_diff.parse`
  (#1081) restated in the local-diff op.
* `_check_test_pairing` asks whether the expected test for a newly added source
  file is in the changed set. `_changed_files` splits `--name-status` the same
  way, so a file NAMED `z<U+2028>tests/test_a.py` yields a second, fabricated
  record whose path is `tests/test_a.py` - and the "new source file has no test"
  warning goes quiet for a file nobody wrote a test for.

Narrowing the split alone would trade a forged parse boundary for a forged
render line (#1105's finding), so both fixes are two-part: split on LF/CR/CRLF
via `_untrusted.split_lines`, then flatten the path and the matched content
where they are interpolated into a line this op owns at column 0 - the same
pairing `presets/gitlab/job.py::_log_lines` uses.

`test_two_files_in_one_diff_still_get_their_own_attribution` was green before
the fix and is a guard, not a pin: the forgery is the fragment, never the
`+++ b/` branch itself, and gating that branch would break every multi-file
diff. It is here on purpose to pass whatever the fix does.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

DIFF = Path(__file__).parent.parent / "presets" / "git" / "diff.py"

#: The separator this audit is about: `str.splitlines()` breaks on it, every
#: line-oriented format git speaks does not, and `git check-ref-format` and
#: `core.quotepath=false` both let it through (#1119).
SEP = chr(0x2028)

SECRET_RULE = json.dumps([
    {"pattern": "secret_token", "label": "hardcoded secret", "ext": ".py"}
])
PAIRING_RULE = json.dumps([
    {"src": r"src/(?P<name>[^/]+)\.py$", "test": "tests/test_{name}.py"}
])


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "config", "user.email", "t@t.invalid"], check=True, cwd=path)
    subprocess.run(["git", "config", "user.name", "T"], check=True, cwd=path)
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "seed.txt"], check=True, cwd=path)
    subprocess.run(["git", "commit", "-q", "-m", "init"], check=True, cwd=path)


def _write(path: Path, rel: str, content: str) -> None:
    target = path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _run(repo: Path, *args: str, env_extra: dict | None = None) -> str:
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    res = subprocess.run(
        [sys.executable, str(DIFF), *args],
        capture_output=True, text=True, encoding="utf-8", cwd=repo, env=env,
        errors="replace",
    )
    assert res.returncode == 0, res.stderr
    return res.stdout


def test_an_added_line_cannot_relabel_the_file_the_red_flag_scan_is_reading(
        tmp_path: Path) -> None:
    """The scan must not be switchable off by the diff it is scanning."""
    _init_repo(tmp_path)
    _write(tmp_path, "app.py",
           "start = 1\n"
           f"decoy = 1{SEP}+++ b/notes.txt\n"
           'secret_token = "hunter2"\n')
    subprocess.run(["git", "add", "-A"], check=True, cwd=tmp_path)

    out = _run(tmp_path, "staged",
               env_extra={"SUPERTOOL_RED_FLAGS_EXTRA": SECRET_RULE})

    assert "hardcoded secret" in out, (
        "the `.py`-scoped red-flag pattern stopped matching after an added line "
        "forged a `+++ b/` header - the scan was disabled by its own input"
    )
    assert "app.py" in out


def test_the_forged_separator_is_named_in_the_render_not_carried_into_it(
        tmp_path: Path) -> None:
    """Narrowing the split must not leave the separator loose in the receipt."""
    _init_repo(tmp_path)
    _write(tmp_path, "app.py", f'secret_token = "a{SEP}b"\n')
    subprocess.run(["git", "add", "-A"], check=True, cwd=tmp_path)

    out = _run(tmp_path, "staged",
               env_extra={"SUPERTOOL_RED_FLAGS_EXTRA": SECRET_RULE})

    assert "hardcoded secret" in out
    assert SEP not in out, (
        "a raw U+2028 reached the receipt, where it forges a line in whatever "
        "reads it next - the split was narrowed without the render fence"
    )


def test_a_filename_cannot_forge_the_test_file_the_pairing_gate_looks_for(
        tmp_path: Path) -> None:
    """A second, fabricated `--name-status` record must not answer for a real one."""
    _init_repo(tmp_path)
    _write(tmp_path, "src/a.py", "def a():\n    return 1\n")
    try:
        _write(tmp_path, f"z{SEP}tests/test_a.py", "ok\n")
    except (OSError, UnicodeError) as exc:
        pytest.skip(f"this filesystem will not hold U+2028 in a path ({exc})")
    subprocess.run(["git", "add", "-A"], check=True, cwd=tmp_path)

    out = _run(tmp_path, "staged",
               env_extra={"SUPERTOOL_TEST_PAIRING": PAIRING_RULE})

    assert "tests/test_a.py" in out and "no test" in out, (
        "the missing-test warning for src/a.py went quiet because a filename "
        "forged a changed-file record naming its expected test"
    )
    assert SEP not in out


def test_two_files_in_one_diff_still_get_their_own_attribution(
        tmp_path: Path) -> None:
    """Guard, not a pin: green before the fix, and must stay green after it."""
    _init_repo(tmp_path)
    _write(tmp_path, "one.py", 'secret_token = "x"\n')
    _write(tmp_path, "two.py", 'secret_token = "y"\n')
    subprocess.run(["git", "add", "-A"], check=True, cwd=tmp_path)

    out = _run(tmp_path, "staged",
               env_extra={"SUPERTOOL_RED_FLAGS_EXTRA": SECRET_RULE})

    assert "one.py:1" in out and "two.py:1" in out
