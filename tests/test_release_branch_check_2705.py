"""#2705 (ported from claude-remember#851) -- the check that runs on a built release
tree before anything is pushed.

`.github/scripts/check_release_tree.py` encodes the Anthropic directory's "Files in
the plugin folder" rules (every non-image, non-font file under 256 KiB; at most 512
files; only text plus PNG/JPEG/GIF/WebP and fonts; no `.gitattributes` with
export-ignore/export-subst/filter) plus this repo's own total budget and a refusal
of `.DS_Store`/`Thumbs.db`, plus a declared, issue-referenced exception list for a
path that is over `max_file_bytes` but cannot be denied (#2705's own `_supertool.py`
case, tracked in #2706).

Every failure class has a passing twin built the same way: a check that failed on
everything would pass every "must fail" test here, and the twins are what catch it.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / ".github" / "scripts" / "check_release_tree.py"

KIB = 1024
LIMIT = 256 * KIB

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 32
WOFF2 = b"wOF2" + b"\x00" * 32
TTF = b"\x00\x01\x00\x00" + b"\x00" * 32
ELF = b"\x7fELF\x02\x01\x01" + b"\x00" * 32


def _load():
    assert SCRIPT.exists(), f"{SCRIPT} does not exist (#2705)"
    spec = importlib.util.spec_from_file_location("check_release_tree", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_release_tree"] = mod
    spec.loader.exec_module(mod)
    return mod


BUDGET = {"max_file_bytes": LIMIT, "max_files": 512, "max_total_bytes": 3 * KIB * KIB}


def _tree(tmp_path: Path, files: dict) -> Path:
    root = tmp_path / "tree"
    root.mkdir()
    # A tree that passes every directory row (tests/test_release_branch_preflight_2705.py
    # covers those rows), so each test below varies only the property it is about.
    base = {
        ".claude-plugin/plugin.json": (b'{"name": "x", "description": "d", '
                                       b'"version": "1.0.0", "author": {"name": "a"}}\n'),
        "README.md": ("# x\n\n" + " ".join(["word"] * 40) + "\n").encode(),
        "LICENSE": b"license\n",
        "scripts/run.sh": b"#!/bin/sh\necho hi\n",
    }
    base.update(files)
    for rel, data in base.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root


def _check(root: Path, exceptions: dict | None = None, **budget):
    mod = _load()
    b = dict(BUDGET)
    b.update(budget)
    return mod.check_tree(root, b, exceptions)


def test_a_clean_tree_passes(tmp_path):
    result = _check(_tree(tmp_path, {}))
    assert result.offenders == []
    assert result.files == 4
    assert result.total_bytes > 0


# -- per-file size --------------------------------------------------------------

def test_a_text_file_at_256_kib_fails_and_is_named(tmp_path):
    root = _tree(tmp_path, {"CHANGELOG.md": b"a" * LIMIT})
    offenders = _check(root).offenders
    assert any("CHANGELOG.md" in o and "256" in o for o in offenders), offenders


def test_a_text_file_one_byte_under_256_kib_passes(tmp_path):
    root = _tree(tmp_path, {"CHANGELOG.md": b"a" * (LIMIT - 1)})
    assert _check(root).offenders == []


def test_a_large_real_image_is_exempt_from_the_size_limit(tmp_path):
    root = _tree(tmp_path, {"img/big.png": PNG + b"\x00" * LIMIT})
    assert _check(root).offenders == []


def test_a_large_text_file_named_png_is_not_exempt(tmp_path):
    """The exemption follows the bytes, not the extension."""
    root = _tree(tmp_path, {"img/fake.png": b"a" * LIMIT})
    offenders = _check(root).offenders
    assert any("img/fake.png" in o for o in offenders), offenders


# -- declared exceptions (#2705) -------------------------------------------------

def test_a_declared_exception_is_reported_as_review_not_fail(tmp_path):
    root = _tree(tmp_path, {"_supertool.py": b"a" * LIMIT})
    exceptions = {"_supertool.py": {"issue": 2706, "reason": "the tool itself"}}
    result = _check(root, exceptions)
    assert result.offenders == [], result.offenders
    assert any("_supertool.py" in r and "2706" in r for r in result.reviews), result.reviews


def test_an_undeclared_oversize_file_still_fails_even_with_other_exceptions_set(tmp_path):
    """A declared exception for one path must not quietly cover a different one."""
    root = _tree(tmp_path, {"_supertool.py": b"a" * LIMIT, "other-big.md": b"b" * LIMIT})
    exceptions = {"_supertool.py": {"issue": 2706, "reason": "the tool itself"}}
    result = _check(root, exceptions)
    assert any("other-big.md" in o for o in result.offenders), result.offenders
    assert not any("other-big.md" in r for r in result.reviews), result.reviews
    # Positive control: the declared path is still a REVIEW, not a FAIL, in the
    # same run as the undeclared one failing.
    assert not any("_supertool.py" in o for o in result.offenders), result.offenders
    assert any("_supertool.py" in r for r in result.reviews), result.reviews


def test_no_exceptions_argument_behaves_as_before(tmp_path):
    """check_tree(root, budget) with no third argument is still a plain FAIL --
    callers that have not been updated to pass exceptions see the old behaviour."""
    mod = _load()
    root = _tree(tmp_path, {"CHANGELOG.md": b"a" * LIMIT})
    result = mod.check_tree(root, BUDGET)
    assert any("CHANGELOG.md" in o for o in result.offenders), result.offenders
    assert result.reviews == []


# -- file count and total -------------------------------------------------------

def test_more_files_than_the_budget_fails(tmp_path):
    root = _tree(tmp_path, {f"f/{i}.txt": b"x" for i in range(3)})  # 7 files
    offenders = _check(root, max_files=6).offenders
    assert any("7 files" in o and "6" in o for o in offenders), offenders


def test_exactly_the_file_budget_passes(tmp_path):
    root = _tree(tmp_path, {f"f/{i}.txt": b"x" for i in range(3)})  # 7 files
    assert _check(root, max_files=7).offenders == []


def test_a_total_over_the_budget_fails(tmp_path):
    root = _tree(tmp_path, {"a.txt": b"a" * 1000, "b.txt": b"b" * 1000})
    total = _check(root).total_bytes
    offenders = _check(root, max_total_bytes=total - 1).offenders
    assert any("total" in o for o in offenders), offenders
    # Positive control: exactly at the budget is fine.
    assert _check(root, max_total_bytes=total).offenders == []


# -- junk files -----------------------------------------------------------------

@pytest.mark.parametrize("name", [".DS_Store", "sub/.DS_Store", "Thumbs.db", "sub/thumbs.db"])
def test_os_junk_files_fail(tmp_path, name):
    root = _tree(tmp_path, {name: b"x"})
    offenders = _check(root).offenders
    assert any(name in o for o in offenders), offenders


def test_a_file_merely_containing_ds_store_in_its_name_passes(tmp_path):
    root = _tree(tmp_path, {"docs/about-.DS_Store-files.md": b"# x\n"})
    assert _check(root).offenders == []


# -- .gitattributes -------------------------------------------------------------

@pytest.mark.parametrize("line,word", [
    ("tests/ export-ignore", "export-ignore"),
    ("VERSION export-subst", "export-subst"),
    ("*.psd filter=lfs diff=lfs merge=lfs -text", "filter"),
    ("*.bin -filter", "filter"),
])
def test_a_forbidden_gitattributes_fails(tmp_path, line, word):
    root = _tree(tmp_path, {"sub/.gitattributes": f"# comment\n{line}\n".encode()})
    offenders = _check(root).offenders
    assert any("sub/.gitattributes" in o and word in o for o in offenders), offenders


def test_a_benign_gitattributes_passes(tmp_path):
    root = _tree(tmp_path, {
        ".gitattributes": b"*.sh text eol=lf\n# export-ignore in a comment is fine\n",
    })
    assert _check(root).offenders == []


# -- file types -----------------------------------------------------------------

@pytest.mark.parametrize("name,data", [
    ("a.png", PNG), ("a.jpg", JPEG), ("a.gif", GIF), ("a.webp", WEBP),
    ("a.woff2", WOFF2), ("a.ttf", TTF), ("a.svg", b"<svg xmlns='x'/>\n"),
    ("a.md", "café — utf-8\n".encode()), ("empty.txt", b""),
])
def test_allowed_types_pass(tmp_path, name, data):
    root = _tree(tmp_path, {name: data})
    assert _check(root).offenders == []


@pytest.mark.parametrize("name,data", [
    ("tool", ELF),
    ("x.pyc", b"\x61\x0d\x0d\x0a" + b"\x00" * 12),
    ("notes.txt", b"text with a NUL\x00 in it\n"),
    ("bad.txt", b"\xff\xfe\xfa not utf-8\n"),
])
def test_binary_files_fail_and_are_named(tmp_path, name, data):
    root = _tree(tmp_path, {name: data})
    offenders = _check(root).offenders
    assert any(name in o and "binary" in o for o in offenders), offenders


def test_every_offender_is_named_not_just_the_first(tmp_path):
    root = _tree(tmp_path, {
        "big.md": b"a" * LIMIT, ".DS_Store": b"x", "tool": ELF,
        ".gitattributes": b"x export-ignore\n",
    })
    offenders = _check(root).offenders
    for name in ("big.md", ".DS_Store", "tool", ".gitattributes"):
        assert any(o.startswith(name + ":") for o in offenders), (name, offenders)


# -- CLI ------------------------------------------------------------------------

def _cli(root: Path, *extra: str):
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(root), *extra],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )


def test_cli_exits_non_zero_and_names_offenders(tmp_path):
    root = _tree(tmp_path, {"big.md": b"a" * LIMIT, "Thumbs.db": b"x"})
    r = _cli(root)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "big.md" in r.stdout and "Thumbs.db" in r.stdout


def test_cli_exits_zero_on_a_clean_tree(tmp_path):
    r = _cli(_tree(tmp_path, {}))
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "4 files" in r.stdout


def test_cli_budget_comes_from_the_config(tmp_path):
    root = _tree(tmp_path, {})
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"budget": {"max_file_bytes": 262144, "max_files": 3, '
                   '"max_total_bytes": 3145728}}', encoding="utf-8")
    assert _cli(root, "--config", str(cfg)).returncode == 1
    cfg.write_text('{"budget": {"max_file_bytes": 262144, "max_files": 4, '
                   '"max_total_bytes": 3145728}}', encoding="utf-8")
    assert _cli(root, "--config", str(cfg)).returncode == 0


def test_cli_exceptions_come_from_the_config_too(tmp_path):
    root = _tree(tmp_path, {"_supertool.py": b"a" * LIMIT})
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"budget": {"max_file_bytes": 262144, "max_files": 512, '
                   '"max_total_bytes": 3145728}, "exceptions": '
                   '[{"path": "_supertool.py", "issue": 2706, "reason": "the tool itself"}]}',
                   encoding="utf-8")
    r = _cli(root, "--config", str(cfg))
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "REVIEW _supertool.py" in r.stdout and "2706" in r.stdout


# -- #2732: no shipped script sources another, no typed heredoc operator --------

def test_a_sourced_sibling_script_fails(tmp_path):
    """COMMAND_SCRIPT_NOT_FOLLOWED's confirmed cause: a shipped script that
    `source`s/`.`s another file (claude-jit-context's own
    docs/directory-validator.md, playbook step 5)."""
    root = _tree(tmp_path, {
        "hooks/guard.sh": b'#!/bin/bash\n. "$(dirname "$0")/lib.sh"\necho ok\n',
        "hooks/lib.sh": b"#!/bin/bash\nfoo() { echo bar; }\n",
    })
    result = _check(root)
    assert any("hooks/guard.sh" in o and "sources" in o for o in result.offenders), (
        result.offenders)


def test_a_sourced_file_inside_a_loop_body_fails(tmp_path):
    """#2732 self-review: `do . "$f"; done` is a realistic way to source a
    sibling file per loop iteration, and the first draft of this regex
    missed it (no `do` keyword in its alternation)."""
    root = _tree(tmp_path, {
        "hooks/guard.sh": b'#!/bin/bash\nfor f in "$d"/*.sh; do . "$f"; done\n',
    })
    result = _check(root)
    assert any("hooks/guard.sh" in o and "sources" in o for o in result.offenders), (
        result.offenders)


def test_a_sourced_file_inside_command_substitution_fails(tmp_path):
    """#2732 self-review: `$(. "$conf" && ...)` sources inside a subshell,
    and the first draft missed the opening `(`."""
    root = _tree(tmp_path, {
        "hooks/guard.sh": b'#!/bin/bash\nOUT=$(. "$conf" && echo "$VAR")\n',
    })
    result = _check(root)
    assert any("hooks/guard.sh" in o and "sources" in o for o in result.offenders), (
        result.offenders)


def test_a_sourced_file_inside_an_if_then_fails(tmp_path):
    """#2732 self-review: `if . "$f"; then` -- the exact shape
    hooks/session-start.sh used before #2732's build-time inlining -- and
    the first draft missed the leading `if` keyword."""
    root = _tree(tmp_path, {
        "hooks/guard.sh": b'#!/bin/bash\nif . "$f" 2>/dev/null; then\n    true\nfi\n',
    })
    result = _check(root)
    assert any("hooks/guard.sh" in o and "sources" in o for o in result.offenders), (
        result.offenders)


def test_running_a_relative_script_is_not_sourcing_it(tmp_path):
    """Positive control for the `if`/dot overlap: `if ./build.sh; then` RUNS
    a script, it does not source one -- `.` immediately followed by `/`
    (no space) must not match, or every ordinary relative invocation would
    fail this guard."""
    root = _tree(tmp_path, {
        "hooks/guard.sh": b'#!/bin/bash\nif ./build.sh; then\n    true\nfi\n',
    })
    result = _check(root)
    assert not any("hooks/guard.sh" in o and "sources" in o for o in result.offenders), (
        result.offenders)


def test_source_as_a_word_inside_a_string_does_not_fail(tmp_path):
    """Positive control: a check that flagged the WORD `source` anywhere would
    pass every real build and fail on prose, which is the opposite defect."""
    root = _tree(tmp_path, {
        "hooks/guard.sh": b'#!/bin/bash\necho "the source of this decision is #1625"\n',
    })
    result = _check(root)
    assert not any("hooks/guard.sh" in o and "sources" in o for o in result.offenders), (
        result.offenders)


def test_a_typed_heredoc_operator_fails(tmp_path):
    """UNPINNED_NPX's own wording: a typed `<<`, even inside quotes or a
    regex, is a hard block in any shipped script."""
    root = _tree(tmp_path, {
        "hooks/guard.sh": b'#!/bin/bash\ncat <<EOF\nhi\nEOF\n',
    })
    result = _check(root)
    assert any("hooks/guard.sh" in o and "heredoc" in o for o in result.offenders), (
        result.offenders)


def test_a_herestring_does_not_fail(tmp_path):
    """Positive control: `<<<` is a here-string, explicitly exempted by the
    directory's own write-up -- a check that flagged it too would hold every
    script using the common `cmd <<<"$var"` idiom."""
    root = _tree(tmp_path, {
        "hooks/guard.sh": b'#!/bin/bash\ngrep foo <<<"$bar"\n',
    })
    result = _check(root)
    assert not any("hooks/guard.sh" in o and "heredoc" in o for o in result.offenders), (
        result.offenders)


def test_an_image_referenced_from_a_hook_script_fails(tmp_path):
    """#2732 item 5c: the banner (or any image) shipping while referenced by
    code. `_check_images` already implements this row ('a bundled image is
    not referenced from commands/, hooks/ or scripts/') -- no new guard was
    needed, only a test pinning it, since none existed."""
    root = _tree(tmp_path, {
        "docs/logo.png": PNG,
        "hooks/guard.sh": b'#!/bin/bash\necho docs/logo.png\n',
    })
    result = _check(root)
    assert any("docs/logo.png" in o and "referenced from" in o for o in result.offenders), (
        result.offenders)


def test_an_image_referenced_only_from_prose_does_not_fail(tmp_path):
    """Positive control: README.md's own `<img src="...">` -- a markdown/HTML
    reference outside commands/hooks/scripts and outside a code span -- is
    the ordinary, allowed way to show a logo."""
    root = _tree(tmp_path, {
        "docs/logo.png": PNG,
        "README.md": ('# x\n\n<img src="docs/logo.png">\n\n'
                       + " ".join(["word"] * 40) + "\n").encode(),
    })
    result = _check(root)
    assert not any("docs/logo.png" in o for o in result.offenders), result.offenders
