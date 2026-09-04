"""A malformed `changelog.d/` fragment must be refused at write time (#1132).

The rule is already fully implemented and its messages are good — four guards
in `assemble_changelog.py` state it precisely. Nothing about it was wired to
the moment a fragment is *written*, so `paste` returned

    [validators]
    git-status  : ok          (no new errors)   0.2s
    [result] 1 op run, 1 write

for a fragment CI refuses, and the cheapest thing that disagreed with that
receipt was a 20-leg matrix twenty minutes later. PR #1115 went red on 14 of
20 legs over one missing `- `.

The malformation pinned here is that one, not a synthetic stand-in: a fragment
body written as a bare paragraph.

**The local check and CI must not be two descriptions of one rule.** They
would drift, and a green local receipt followed by a red matrix is the current
complaint inverted. So the adapter imports the project's own
`assemble_changelog.py` and calls the same three checks `collect()` calls —
`parse_fragment_name`, the empty-body test, `scan_fragment_body` — and
publishes their messages verbatim. `test_local_finding_is_the_assemblers_own`
pins that they are the same string.

Would these pass if the code did nothing? No. Each asserts a refusal carrying
the assembler's own sentence and a line number, a registration read out of
supertool's real dispatch function, or the absence of the verdict keys on a
skip.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ADAPTER = REPO / "validators" / "changelog-fragment" / "changelog-fragment.py"
ASSEMBLER = REPO / ".github" / "scripts" / "assemble_changelog.py"
SUPERTOOL = REPO / "supertool.py"

#: `changelog.d/1109.fixed.md` as PR #1115 shipped it: the entry, correct in
#: every respect except the two characters that make it a list item.
BARE_PARAGRAPH = (
    "**Fragment written as a bare paragraph** "
    "([#1109](https://github.com/Digital-Process-Tools/claude-supertool/issues/1109)). "
    "One missing list marker, fourteen red legs.\n"
)
WELL_FORMED = "- " + BARE_PARAGRAPH


def _project(tmp_path: Path, *, with_assembler: bool = True) -> Path:
    """A minimal project that declares the fragment rules the way this repo does.

    Git-initialized (#2178): `_find_assembler` bounds its walk at the git
    repo root above the fragment, so a project fixture with no `.git` at all
    resolves to "not inside a git repository" and every case here would read
    as `skipped` regardless of where the assembler sits.
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "changelog.d").mkdir(parents=True, exist_ok=True)
    if with_assembler:
        scripts = tmp_path / ".github" / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ASSEMBLER, scripts / "assemble_changelog.py")
    return tmp_path


def _run(target: Path, env_extra: dict | None = None) -> dict:
    env = dict(os.environ)
    env.pop("SUPERTOOL_REQUIRE_VALIDATORS", None)
    env.update(env_extra or {})
    proc = subprocess.run([sys.executable, str(ADAPTER), str(target)],
                          capture_output=True, text=True, env=env,
                          encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _write(project: Path, name: str, body: str) -> Path:
    target = project / "changelog.d" / name
    target.write_text(body, encoding="utf-8")
    return target


def test_bare_paragraph_fragment_is_refused_at_write_time(tmp_path):
    result = _run(_write(_project(tmp_path), "1109.fixed.md", BARE_PARAGRAPH))
    assert result["ok"] is False
    assert result["count"] >= 1
    assert "skipped" not in result
    finding = result["errors"][0]
    assert finding["line"] == 1
    assert "not a single `- ` bullet list" in finding["msg"]


def test_the_same_fragment_with_its_list_marker_passes(tmp_path):
    result = _run(_write(_project(tmp_path), "1109.fixed.md", WELL_FORMED))
    assert result["ok"] is True
    assert result["count"] == 0
    assert result["errors"] == []


def test_local_finding_is_the_assemblers_own(tmp_path):
    """One rule, one description. Not a second, thinner explanation of it."""
    spec = importlib.util.spec_from_file_location("asm_1132", ASSEMBLER)
    assert spec is not None and spec.loader is not None
    asm = importlib.util.module_from_spec(spec)
    sys.modules["asm_1132"] = asm
    spec.loader.exec_module(asm)

    expected = asm.scan_fragment_body("1109.fixed.md", BARE_PARAGRAPH)
    assert expected, "the assembler itself must refuse this body"

    result = _run(_write(_project(tmp_path), "1109.fixed.md", BARE_PARAGRAPH))
    assert [e["msg"] for e in result["errors"]] == expected


def test_a_filename_ci_will_not_parse_is_refused(tmp_path):
    result = _run(_write(_project(tmp_path), "1109.fixt.md", WELL_FORMED))
    assert result["ok"] is False
    assert "unknown section" in result["errors"][0]["msg"]
    assert result["errors"][0]["line"] is None


def test_a_file_bad_in_two_ways_reports_what_ci_reports(tmp_path):
    """Not just the same verdict — the same findings, on the same file.

    `collect()` `continue`s past a fragment whose *name* will not parse, so it
    never reads that body. An adapter that carried on and body-scanned anyway
    returned two findings where `--check` returns one: a divergence nobody
    would see until they compared the two outputs, on the one file where the
    two are supposed to be the same sentence.
    """
    project = _project(tmp_path)
    target = _write(project, "1109.fixt.md", BARE_PARAGRAPH)

    result = _run(target)
    assert result["ok"] is False
    assert result["count"] == 1
    assert "unknown section" in result["errors"][0]["msg"]

    proc = subprocess.run(
        [sys.executable, str(project / ".github" / "scripts" / "assemble_changelog.py"),
         "--check", "--dir", str(project / "changelog.d")],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    ci_output = proc.stdout + proc.stderr
    assert "unknown section" in ci_output, ci_output
    assert "not a single `- ` bullet list" not in ci_output, ci_output


def test_the_directorys_own_readme_is_not_a_fragment(tmp_path):
    result = _run(_write(_project(tmp_path), "README.md", "# changelog.d\n"))
    assert "skipped" in result
    assert "ok" not in result and "count" not in result and "errors" not in result


def test_no_assembler_above_the_file_declines_rather_than_passing(tmp_path):
    """supertool runs against projects that have no such convention at all."""
    project = _project(tmp_path, with_assembler=False)
    result = _run(_write(project, "1109.fixed.md", BARE_PARAGRAPH))
    assert "skipped" in result
    assert "ok" not in result
    assert "assemble_changelog.py" in result["skipped"]


def test_no_markdown_parser_is_a_skip_not_an_ok(tmp_path):
    """The one guard whose absence would make this validator decorative."""
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "markdown_it.py").write_text(
        "raise ImportError('stubbed by test_changelog_fragment_write_receipt_1132')\n",
        encoding="utf-8")
    target = _write(_project(tmp_path), "1109.fixed.md", BARE_PARAGRAPH)
    result = _run(target, {"PYTHONPATH": str(shadow)})
    assert "skipped" in result, result
    assert "ok" not in result and "count" not in result and "errors" not in result
    assert "markdown-it-py" in result["skipped"]


def test_registered_so_it_actually_fires_on_a_write(monkeypatch):
    """An unregistered validator is a check that looks like it exists.

    Read out of supertool's own dispatch rather than out of the JSON: the
    question is not whether a key is present, it is whether a `paste` at this
    path would run it — the `match` glob, the `hooks_into` list and the
    `opt_in` flag all get a vote, and each of them can silence a registration
    that looks complete.

    `conftest` hands every test an empty `_CONFIG` on purpose, so this repo's
    real config is loaded explicitly. The matcher is still supertool's.
    """
    sys.path.insert(0, str(REPO))
    import _supertool

    config = json.loads((REPO / ".supertool.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(_supertool, "_CONFIG", config)

    for op in ("paste", "edit", "vim", "append", "replace", "replace_lines"):
        chosen = _supertool._applicable_validators(op, "changelog.d/1109.fixed.md")
        assert "changelog-fragment" in chosen, op

    unrelated = _supertool._applicable_validators("paste", "docs/validators.md")
    assert "changelog-fragment" not in unrelated


def test_the_match_glob_survives_windows_separators():
    """The one way this validator could be silently absent on half the matrix.

    `match` is fnmatched against the path as the op received it, and on Windows
    that path is spelled with backslashes. `fnmatch.fnmatch` runs both sides
    through `os.path.normcase` first, which on Windows also rewrites a forward
    slash into a backslash — so the same POSIX-looking glob covers both.
    Asserted here through `ntpath.normcase` explicitly rather than left to the
    Windows legs, because a rule that only fails where nobody is looking fails
    silently.
    """
    import fnmatch
    import ntpath

    config = json.loads((REPO / ".supertool.json").read_text(encoding="utf-8"))
    glob = config["validators"]["changelog-fragment"]["match"]
    sep = chr(92)

    windows_paths = [
        sep.join(["changelog.d", "1109.fixed.md"]),
        sep.join(["C:", "Users", "dev", "repo", "changelog.d", "1109.fixed.md"]),
        "changelog.d/1109.fixed.md",
    ]
    for path in windows_paths:
        assert fnmatch.fnmatchcase(ntpath.normcase(path), ntpath.normcase(glob)), path

    assert not fnmatch.fnmatchcase(
        ntpath.normcase(sep.join(["docs", "validators.md"])), ntpath.normcase(glob))


def test_a_paste_of_a_bad_fragment_no_longer_returns_a_green_receipt(tmp_path):
    """End to end, through the CLI, which is where #1132 was observed.

    cwd is THIS repo -- so the .supertool.json that wires this run lives
    here -- while `target` is a disjoint project under `tmp_path`. Before
    #2236, the convention-based location alone was enough for `target`'s
    own `.github/scripts/assemble_changelog.py` to be imported and
    executed automatically; #2236 requires an explicit
    SUPERTOOL_CHANGELOG_ASSEMBLER pin for that disjoint-tree shape, the
    same escape hatch this file's `test_explicit_override_bypasses_the_scope_check`
    already relies on for the ancestor case. Without the pin this call now
    reads `skipped`, not a finding -- proven separately by
    `test_changelog_fragment_untrusted_checkout_2228.py::test_a_disjoint_unrelated_project_needs_explicit_opt_in`
    -- so this end-to-end test supplies it to keep exercising the write-time
    refusal #1132 is actually about."""
    project = _project(tmp_path)
    target = project / "changelog.d" / "1109.fixed.md"
    quote = chr(39) * 3
    payload = "path = {0}\ncontent = {1}{2}{1}\n".format(
        json.dumps(str(target)), quote, BARE_PARAGRAPH)
    env = dict(os.environ)
    env["SUPERTOOL_CHANGELOG_ASSEMBLER"] = os.path.join(
        ".github", "scripts", "assemble_changelog.py")
    proc = subprocess.run([sys.executable, str(SUPERTOOL), "paste:@-"],
                          input=payload, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=str(REPO),
                          env=env)
    receipt = proc.stdout + proc.stderr
    assert "changelog-fragment" in receipt, receipt
    assert "not a single `- ` bullet list" in receipt, receipt
