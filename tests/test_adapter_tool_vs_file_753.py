"""Adapters must not blame the file for their tool falling over (#753).

The sweep in #752 (issue #745) fixed `phplint` and filed the siblings. This is
the same defect in five more adapters: a non-zero exit is published as a
syntax/format/compile finding about a file the tool may never have opened, and
in `node-check`'s case with a line number lifted out of an internal stack frame.

The rule, inherited from #745 rather than reinvented: **the tool is credited
with having spoken about the file when its output carries a recognisable
diagnostic** — for every tool here, a located `file:line:` diagnostic in its own
format. Anything else on a non-zero exit becomes `code: "adapter"` via
`refusal.tool_fault()`, keeping `ok: False` and `count: 1`. Reclassify, never
silence.

**Ambiguity falls towards the file.** A located diagnostic whose banner the
classifier does not recognise stays a finding. Nothing is dropped either way —
both branches emit `ok: False`, and an `adapter` message names the exit code and
the raw output, so a fault misread as a finding stays fully legible while a
finding relabelled `adapter` sends the reader to audit a healthy toolchain.

Every fixture below is a fake binary on PATH. The exits that matter here are
precisely the ones a healthy toolchain cannot be asked to produce on demand, and
stubbing only the spawned process leaves every line of the adapter under test.
The real output shapes the classifiers are written against were captured from
the actual tools (gofmt 1.x, node v22, GNU bash 3.2, libxml 2.9, ruby 2.6) and
are quoted in each section.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _adapter_budget import adapter_budget  # noqa: E402
from _adapter_verdict import describe, verdict  # noqa: E402

VALIDATORS = Path(__file__).parent.parent / "validators"

ADAPTERS = {
    "xmllint": VALIDATORS / "xmllint" / "xmllint.py",
    "bash-check": VALIDATORS / "bash-check" / "bash-check.py",
    "node-check": VALIDATORS / "node-check" / "node-check.py",
    "gofmt-check": VALIDATORS / "gofmt-check" / "gofmt-check.py",
    "ruby-check": VALIDATORS / "ruby-check" / "ruby-check.py",
    "terraform-check": VALIDATORS / "terraform-check" / "terraform-check.py",
    "cargo-check": VALIDATORS / "cargo-check" / "cargo-check.py",
}

# The binary each adapter spawns, for the fixtures that stub it.
BINARIES = {
    "xmllint": "xmllint", "bash-check": "bash", "node-check": "node",
    "gofmt-check": "gofmt", "ruby-check": "ruby",
    "terraform-check": "terraform", "cargo-check": "cargo",
}


def _fake_tool(tmp_path: Path, name: str, *, exit_code: int,
               stdout: str = "", stderr: str = "") -> Path:
    """A `name` on PATH printing the given streams and exiting `exit_code`.

    A Python script plus a per-platform launcher, so the same fixture works on
    POSIX and on Windows, where `subprocess.run([name, ...])` resolves through
    PATHEXT rather than a shebang (the shape #752 settled on).
    """
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    script = bindir / f"fake_{name.replace('-', '_')}.py"
    script.write_text(
        "import sys\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.stderr.write({stderr!r})\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        launcher = bindir / f"{name}.bat"
        launcher.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        launcher = bindir / name
        launcher.write_text(
            "#!/bin/sh\n"
            f"exec '{sys.executable}' '{script}' \"$@\"\n", encoding="utf-8")
        launcher.chmod(0o755)
    return bindir


def _run(adapter: str, bindir: Path, target: Path) -> dict:
    env = dict(os.environ)
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    path = ADAPTERS[adapter]
    r = subprocess.run(
        [sys.executable, str(path), str(target)],
        capture_output=True, text=True, env=env, timeout=adapter_budget(path),
    )
    return verdict(r, adapter=adapter)


def _target(tmp_path: Path, name: str, body: str) -> Path:
    f = tmp_path / name
    f.write_text(body, encoding="utf-8")
    return f


def _assert_tool_fault(data: dict, exit_code: int, needle: str) -> None:
    """`adapter` reclassifies; it must never silence, and never invent a line.

    Asserted identically for every adapter, because the guarantee is the
    contract's and not any one tool's: still one error, still `ok: False`, still
    rollback-triggering, with the exit code and the raw output in the message.
    """
    assert data["ok"] is False, describe(data)
    assert data["count"] == 1, describe(data)
    assert len(data["errors"]) == 1, describe(data)
    err = data["errors"][0]
    assert err["code"] == "adapter", describe(data)
    assert err["severity"] == "error", describe(data)
    assert err["line"] is None, describe(data)
    assert "source_context" not in err, describe(data)
    assert str(exit_code) in err["msg"], f"exit code missing from: {err['msg']}"
    assert needle in err["msg"], f"raw output missing from: {err['msg']}"


# --- xmllint ---------------------------------------------------------------
#
# Real libxml 2.9, `xmllint --noout --nonet --noent`:
#
#   malformed     rc=1  broken.xml:1: parser error : Opening and ending tag
#                       mismatch: b line 1 and a
#   missing path  rc=1  warning: failed to load external entity "/nope/x.xml"
#   unreadable    rc=1  I/O error : Permission denied
#                       warning: failed to load external entity "noperm.xml"
#   bad flag      rc=1  Unknown option --bogusflag  + ~70 lines of usage
#
# Only the first carries a `file:line:` diagnostic. The other three were
# published as `code: "xml"` findings — the usage dump most absurdly, as a
# 300-char "XML error" in a file libxml never opened.

def _xml_target(tmp_path: Path) -> Path:
    return _target(tmp_path, "subject.xml", "<a>\n  <b/>\n</a>\n")


def test_xmllint_failed_to_load_external_entity_is_a_tool_fault(tmp_path: Path) -> None:
    """The shape a missing or unreadable path produces. Nothing was parsed."""
    bindir = _fake_tool(tmp_path, "xmllint", exit_code=1,
                        stderr='warning: failed to load external entity "subject.xml"\n')
    data = _run("xmllint", bindir, _xml_target(tmp_path))
    _assert_tool_fault(data, 1, "failed to load external entity")


def test_xmllint_unknown_option_is_a_tool_fault_not_an_xml_error(tmp_path: Path) -> None:
    """A usage dump is the least file-shaped output libxml can emit."""
    bindir = _fake_tool(tmp_path, "xmllint", exit_code=1,
                        stderr="Unknown option --bogusflag\nUsage : xmllint [options] XMLfiles ...\n")
    data = _run("xmllint", bindir, _xml_target(tmp_path))
    _assert_tool_fault(data, 1, "Unknown option")


def test_xmllint_io_error_is_a_tool_fault(tmp_path: Path) -> None:
    bindir = _fake_tool(tmp_path, "xmllint", exit_code=1,
                        stderr='I/O error : Permission denied\nwarning: failed to load external entity "subject.xml"\n')
    data = _run("xmllint", bindir, _xml_target(tmp_path))
    _assert_tool_fault(data, 1, "Permission denied")


def test_xmllint_silent_non_zero_exit_says_so(tmp_path: Path) -> None:
    bindir = _fake_tool(tmp_path, "xmllint", exit_code=139)
    data = _run("xmllint", bindir, _xml_target(tmp_path))
    _assert_tool_fault(data, 139, "(no output)")


def test_xmllint_a_real_parser_error_is_still_a_finding(tmp_path: Path) -> None:
    """The regression guard: reclassifying is only safe if genuine findings stay."""
    bindir = _fake_tool(
        tmp_path, "xmllint", exit_code=1,
        stderr=("subject.xml:2: parser error : Opening and ending tag mismatch: b line 2 and a\n"
                "  <b></a>\n         ^\n"))
    data = _run("xmllint", bindir, _xml_target(tmp_path))
    assert data["ok"] is False, describe(data)
    assert data["errors"][0]["code"] == "xml", describe(data)
    assert data["errors"][0]["line"] == 2, describe(data)
    assert data["errors"][0]["source_context"], describe(data)


# --- bash-check ------------------------------------------------------------
#
# Real GNU bash 3.2, `bash -n`:
#
#   malformed    rc=2    broken.sh: line 3: syntax error: unexpected end of file
#   missing      rc=127  bash: /nope/missing.sh: No such file or directory
#   a directory  rc=126  .: .: is a directory
#   unreadable   rc=126  bash: noperm.sh: Permission denied
#   bad option   rc=2    bash: --nope: invalid option  + ~25 lines of usage
#
# 126 and 127 are the shell's own "could not execute" codes and can never be a
# syntax verdict. Only the first line carries `: line N:`.

def _sh_target(tmp_path: Path) -> Path:
    return _target(tmp_path, "subject.sh", "echo one\necho two\necho three\n")


def test_bash_missing_file_is_a_tool_fault(tmp_path: Path) -> None:
    """Exit 127 is "could not execute", which is never a statement about syntax."""
    bindir = _fake_tool(tmp_path, "bash", exit_code=127,
                        stderr="bash: subject.sh: No such file or directory\n")
    data = _run("bash-check", bindir, _sh_target(tmp_path))
    _assert_tool_fault(data, 127, "No such file or directory")


def test_bash_permission_denied_is_a_tool_fault(tmp_path: Path) -> None:
    bindir = _fake_tool(tmp_path, "bash", exit_code=126,
                        stderr="bash: subject.sh: Permission denied\n")
    data = _run("bash-check", bindir, _sh_target(tmp_path))
    _assert_tool_fault(data, 126, "Permission denied")


def test_bash_invalid_option_is_a_tool_fault(tmp_path: Path) -> None:
    bindir = _fake_tool(tmp_path, "bash", exit_code=2,
                        stderr="bash: --nope: invalid option\nUsage:\tbash [GNU long option] [option] ...\n")
    data = _run("bash-check", bindir, _sh_target(tmp_path))
    _assert_tool_fault(data, 2, "invalid option")


def test_bash_silent_non_zero_exit_says_so(tmp_path: Path) -> None:
    bindir = _fake_tool(tmp_path, "bash", exit_code=139)
    data = _run("bash-check", bindir, _sh_target(tmp_path))
    _assert_tool_fault(data, 139, "(no output)")


def test_bash_a_real_syntax_error_is_still_a_finding(tmp_path: Path) -> None:
    bindir = _fake_tool(tmp_path, "bash", exit_code=2,
                        stderr="subject.sh: line 2: syntax error: unexpected end of file\n")
    data = _run("bash-check", bindir, _sh_target(tmp_path))
    assert data["errors"][0]["code"] == "syntax", describe(data)
    assert data["errors"][0]["line"] == 2, describe(data)
    assert data["errors"][0]["source_context"], describe(data)


def test_bash_reports_every_located_diagnostic(tmp_path: Path) -> None:
    """bash emits one line per problem; the count must not collapse to one."""
    bindir = _fake_tool(
        tmp_path, "bash", exit_code=2,
        stderr=("subject.sh: line 1: unexpected EOF while looking for matching `)'\n"
                "subject.sh: line 2: syntax error: unexpected end of file\n"))
    data = _run("bash-check", bindir, _sh_target(tmp_path))
    assert data["count"] == 2, describe(data)
    assert [e["line"] for e in data["errors"]] == [1, 2], describe(data)


# --- node-check ------------------------------------------------------------
#
# Real node v22.22.1, `node --check`:
#
#   malformed   rc=1  /abs/path/broken.js:1
#                     const a = (;
#                                ^
#                     SyntaxError: Unexpected token ';'
#                         at wrapSafe (node:internal/modules/cjs/loader:1637:18)
#
#   missing     rc=1  node:internal/modules/cjs/loader:1386
#                       throw err;
#                     Error: Cannot find module '/nope/missing.js'
#                         at node:internal/modules/cjs/loader:1383:15
#
#   bad option  rc=9  node: bad option: --nonexistent-flag
#
# `re.search(r":(\d+)(?::(\d+))?\b", out)` takes the FIRST digit-colon anywhere,
# so the missing-file case was published as `code: "syntax"` at **line 1386** —
# the loader's own frame — with `source_context` rendered for it. That is the
# fabricated-location failure in its purest form.

def _js_target(tmp_path: Path) -> Path:
    return _target(tmp_path, "subject.js", "const a = 1;\nconst b = 2;\nconst c = 3;\n")


def test_node_cannot_find_module_does_not_become_a_syntax_error(tmp_path: Path) -> None:
    """The filed bug. Note the internal frame the old regex mined for a line."""
    target = _js_target(tmp_path)
    bindir = _fake_tool(
        tmp_path, "node", exit_code=1,
        stderr=("node:internal/modules/cjs/loader:1386\n"
                "  throw err;\n  ^\n\n"
                f"Error: Cannot find module '{target}'\n"
                "    at node:internal/modules/cjs/loader:1383:15\n"
                "    at node:internal/main/check_syntax:33:20 {\n"
                "  code: 'MODULE_NOT_FOUND',\n  requireStack: []\n}\n\n"
                "Node.js v22.22.1\n"))
    data = _run("node-check", bindir, target)
    _assert_tool_fault(data, 1, "Cannot find module")


def test_node_does_not_lift_a_line_number_out_of_an_internal_frame(tmp_path: Path) -> None:
    """Stated separately from the classification: even were this a finding, 1386
    is a line in node's own loader, not in a three-line source file."""
    target = _js_target(tmp_path)
    bindir = _fake_tool(
        tmp_path, "node", exit_code=1,
        stderr=("node:internal/modules/cjs/loader:1386\n"
                f"Error: Cannot find module '{target}'\n"))
    data = _run("node-check", bindir, target)
    assert data["errors"][0]["line"] != 1386, describe(data)
    assert data["errors"][0]["line"] is None, describe(data)


def test_node_bad_option_is_a_tool_fault(tmp_path: Path) -> None:
    """Exit 9 with a one-line complaint about argv. No file was opened."""
    bindir = _fake_tool(tmp_path, "node", exit_code=9,
                        stderr="node: bad option: --nonexistent-flag\n")
    data = _run("node-check", bindir, _js_target(tmp_path))
    _assert_tool_fault(data, 9, "bad option")


def test_node_silent_non_zero_exit_says_so(tmp_path: Path) -> None:
    bindir = _fake_tool(tmp_path, "node", exit_code=134)
    data = _run("node-check", bindir, _js_target(tmp_path))
    _assert_tool_fault(data, 134, "(no output)")


def test_node_a_real_syntax_error_keeps_its_line(tmp_path: Path) -> None:
    """The regression guard, with node's real report shape: the location line is
    the resolved absolute path, and the internal frames follow it."""
    target = _js_target(tmp_path)
    bindir = _fake_tool(
        tmp_path, "node", exit_code=1,
        stderr=(f"{target}:2\n"
                "const b = (;\n           ^\n\n"
                "SyntaxError: Unexpected token ';'\n"
                "    at wrapSafe (node:internal/modules/cjs/loader:1637:18)\n\n"
                "Node.js v22.22.1\n"))
    data = _run("node-check", bindir, target)
    assert data["errors"][0]["code"] == "syntax", describe(data)
    assert data["errors"][0]["line"] == 2, describe(data)
    assert "SyntaxError" in data["errors"][0]["msg"], describe(data)
    assert data["errors"][0]["source_context"], describe(data)


def test_node_a_syntax_error_without_a_location_is_still_a_finding(tmp_path: Path) -> None:
    """Ambiguity falls towards the file: the banner alone keeps it a finding.

    But no line is invented for it — an unlocatable finding reports `line: None`
    rather than borrowing a number from whatever else is in the output.
    """
    bindir = _fake_tool(tmp_path, "node", exit_code=1,
                        stderr="SyntaxError: Invalid or unexpected token\n")
    data = _run("node-check", bindir, _js_target(tmp_path))
    assert data["errors"][0]["code"] == "syntax", describe(data)
    assert data["errors"][0]["line"] is None, describe(data)


# --- gofmt-check -----------------------------------------------------------
#
# Real gofmt (go1.x), `gofmt -l`:
#
#   unformatted  rc=0  stdout: unformatted.go        <- the -l signal
#   malformed    rc=2  stderr: broken.go:3:12: expected ')', found '{'
#   missing      rc=2  stderr: stat /nope/x.go: no such file or directory
#
# Two things were wrong. `if r.returncode != 0` published every non-zero exit as
# `code: "syntax"` with no parsing at all, so `stat ...: no such file` became a
# Go syntax error. And even for a genuine parse error the adapter threw away the
# `line:col` gofmt had handed it, reporting `line: None` and no source context.

def _go_target(tmp_path: Path) -> Path:
    return _target(tmp_path, "subject.go",
                   "package main\n\nfunc main() {\n\tprintln(\"hi\")\n}\n")


def test_gofmt_stat_failure_is_a_tool_fault_not_a_syntax_error(tmp_path: Path) -> None:
    """gofmt reports an unopenable path through `stat`, which says nothing
    about Go syntax and carries no line."""
    bindir = _fake_tool(tmp_path, "gofmt", exit_code=2,
                        stderr="stat subject.go: no such file or directory\n")
    data = _run("gofmt-check", bindir, _go_target(tmp_path))
    _assert_tool_fault(data, 2, "no such file or directory")


def test_gofmt_silent_non_zero_exit_says_so(tmp_path: Path) -> None:
    bindir = _fake_tool(tmp_path, "gofmt", exit_code=2)
    data = _run("gofmt-check", bindir, _go_target(tmp_path))
    _assert_tool_fault(data, 2, "(no output)")


def test_gofmt_a_real_parse_error_is_a_finding_with_its_line_and_col(tmp_path: Path) -> None:
    """gofmt hands over `line:col` and the adapter discarded both."""
    bindir = _fake_tool(tmp_path, "gofmt", exit_code=2,
                        stderr="subject.go:3:12: expected ')', found '{'\n")
    data = _run("gofmt-check", bindir, _go_target(tmp_path))
    assert data["ok"] is False, describe(data)
    assert data["errors"][0]["code"] == "syntax", describe(data)
    assert data["errors"][0]["line"] == 3, describe(data)
    assert data["errors"][0]["col"] == 12, describe(data)
    assert data["errors"][0]["source_context"], describe(data)


def test_gofmt_unformatted_file_is_untouched(tmp_path: Path) -> None:
    """The `-l` signal is stdout naming the path at exit **0** — the one branch
    that was already right, and the one a returncode-driven rewrite could break."""
    bindir = _fake_tool(tmp_path, "gofmt", exit_code=0, stdout="subject.go\n")
    data = _run("gofmt-check", bindir, _go_target(tmp_path))
    assert data["ok"] is False, describe(data)
    assert data["errors"][0]["code"] == "formatting", describe(data)


def test_gofmt_clean_file_is_untouched(tmp_path: Path) -> None:
    bindir = _fake_tool(tmp_path, "gofmt", exit_code=0)
    data = _run("gofmt-check", bindir, _go_target(tmp_path))
    assert data["ok"] is True, describe(data)
    assert data["count"] == 0, describe(data)


# --- ruby-check ------------------------------------------------------------
#
# #752's sweep recorded ruby-check as CLEAN — "the model" — with a noted
# residual: "unmatched-but-nonempty stderr still becomes `syntax`". That
# residual is live, and reproduces on the first thing anyone would try:
#
#   ruby -c /nope/missing.rb   rc=1  ruby: No such file or directory -- ... (LoadError)
#   ruby -c .                  rc=1  ruby: Is a directory -- . (LoadError)
#
# Neither carries `file:line:`, so both fall through to `code: "syntax"`. The
# adapter models the *empty*-stderr case correctly and the non-empty one not at
# all, which is the same defect the other five have — so it is fixed here rather
# than left because a table said CLEAN.

def _rb_target(tmp_path: Path) -> Path:
    return _target(tmp_path, "subject.rb", "x = 1\ny = 2\nz = 3\n")


def test_ruby_load_error_is_a_tool_fault_not_a_syntax_error(tmp_path: Path) -> None:
    bindir = _fake_tool(tmp_path, "ruby", exit_code=1,
                        stderr="ruby: No such file or directory -- subject.rb (LoadError)\n")
    data = _run("ruby-check", bindir, _rb_target(tmp_path))
    _assert_tool_fault(data, 1, "LoadError")


def test_ruby_silent_non_zero_exit_still_says_so(tmp_path: Path) -> None:
    """The branch ruby-check already had right — it must survive the change,
    and it now routes through the shared `tool_fault()` message."""
    bindir = _fake_tool(tmp_path, "ruby", exit_code=139)
    data = _run("ruby-check", bindir, _rb_target(tmp_path))
    _assert_tool_fault(data, 139, "(no output)")


def test_ruby_a_real_syntax_error_is_still_a_finding(tmp_path: Path) -> None:
    bindir = _fake_tool(tmp_path, "ruby", exit_code=1,
                        stderr="subject.rb:2: syntax error, unexpected end-of-input\n")
    data = _run("ruby-check", bindir, _rb_target(tmp_path))
    assert data["errors"][0]["code"] == "syntax", describe(data)
    assert data["errors"][0]["line"] == 2, describe(data)


# --- terraform-check -------------------------------------------------------
#
# Real `terraform fmt -check -diff` (captured on OpenTofu v1.12.5, whose fmt is
# the terraform 1.5.7 implementation). The exit codes are **distinct**, which
# the adapter never looked at:
#
#   clean         rc=0  (nothing)
#   unformatted   rc=3  stdout: subject.tf
#                       stdout: --- old/subject.tf +++ new/subject.tf @@ ...
#   HCL syntax    rc=2  stderr: | Error: Argument or block definition required
#                       stderr: |   on subject.tf line 1, in resource "x":
#   missing path  rc=2  stderr: | Error: Invalid file or directory path
#                       stderr: | No file or directory at /nope/missing.tf
#   unreadable    rc=2  stderr: | Error: Failed to open file
#   bad flag      rc=2  stderr: | Error: Failed to parse command-line flags
#
# The adapter collapsed all four non-zero cases into `code: "formatting"` with
# the message "file needs terraform fmt formatting", which is a specific claim
# and false in three of them. The diagnostics are wrapped in a box-drawing
# gutter and ANSI colour, so the classifier strips escapes before matching.
#
# Two markers, not one: a **diff body on stdout** is the fmt verdict; an
# `on <file> line N` inside an Error block is a genuine HCL finding — and it is
# a syntax error, not a formatting one, so it stops being told to run `fmt`.

def _tf_target(tmp_path: Path) -> Path:
    return _target(tmp_path, "subject.tf",
                   'resource "null_resource" "a" {\n  count = 1\n}\n')


def _tf_box(body: str) -> str:
    """terraform's diagnostic renderer: a box-drawing gutter plus ANSI colour."""
    lines = "\n".join("\x1b[31m│\x1b[0m " + ln for ln in body.splitlines())
    return "\x1b[31m╷\x1b[0m\n" + lines + "\n\x1b[31m╵\x1b[0m\n"


def test_terraform_missing_path_is_not_a_formatting_claim(tmp_path: Path) -> None:
    """A "needs terraform fmt formatting" verdict on a path terraform could not
    find is the false-and-specific claim the issue names."""
    bindir = _fake_tool(
        tmp_path, "terraform", exit_code=2,
        stderr=_tf_box("Error: Invalid file or directory path\n\n"
                       "No file or directory at subject.tf\n"))
    data = _run("terraform-check", bindir, _tf_target(tmp_path))
    _assert_tool_fault(data, 2, "Invalid file or directory path")


def test_terraform_failed_to_open_file_is_a_tool_fault(tmp_path: Path) -> None:
    bindir = _fake_tool(
        tmp_path, "terraform", exit_code=2,
        stderr=_tf_box("Error: Failed to open file\n\nFailed to read file subject.tf\n"))
    data = _run("terraform-check", bindir, _tf_target(tmp_path))
    _assert_tool_fault(data, 2, "Failed to open file")


def test_terraform_bad_flag_is_a_tool_fault(tmp_path: Path) -> None:
    bindir = _fake_tool(
        tmp_path, "terraform", exit_code=2,
        stderr=_tf_box("Error: Failed to parse command-line flags\n\n"
                       "flag provided but not defined: -bogus\n"))
    data = _run("terraform-check", bindir, _tf_target(tmp_path))
    _assert_tool_fault(data, 2, "command-line flags")


def test_terraform_silent_non_zero_exit_says_so(tmp_path: Path) -> None:
    bindir = _fake_tool(tmp_path, "terraform", exit_code=2)
    data = _run("terraform-check", bindir, _tf_target(tmp_path))
    _assert_tool_fault(data, 2, "(no output)")


def test_terraform_hcl_syntax_error_is_a_located_finding_not_a_formatting_one(
        tmp_path: Path) -> None:
    """An unparseable .tf file is a finding — but telling its author to run
    `terraform fmt` is advice that cannot work, and the line was thrown away."""
    bindir = _fake_tool(
        tmp_path, "terraform", exit_code=2,
        stderr=_tf_box("Error: Argument or block definition required\n\n"
                       '  on subject.tf line 2, in resource "null_resource":\n'
                       "   2:   count = \n\n"
                       "An argument or block definition is required here.\n"))
    data = _run("terraform-check", bindir, _tf_target(tmp_path))
    assert data["ok"] is False, describe(data)
    assert data["errors"][0]["code"] == "syntax", describe(data)
    assert data["errors"][0]["line"] == 2, describe(data)
    assert "fmt" not in data["errors"][0]["msg"], describe(data)
    assert data["errors"][0]["source_context"], describe(data)


def test_terraform_unformatted_file_is_still_a_formatting_finding(tmp_path: Path) -> None:
    """The regression guard, at terraform's real exit code for it: 3, with the
    diff on stdout. This is the only branch that was ever right."""
    bindir = _fake_tool(
        tmp_path, "terraform", exit_code=3,
        stdout=("subject.tf\n--- old/subject.tf\n+++ new/subject.tf\n"
                "@@ -1,3 +1,3 @@\n-count = 1\n+  count = 1\n"))
    data = _run("terraform-check", bindir, _tf_target(tmp_path))
    assert data["ok"] is False, describe(data)
    assert data["errors"][0]["code"] == "formatting", describe(data)
    assert "count" in data["errors"][0]["msg"], describe(data)


def test_terraform_clean_file_is_untouched(tmp_path: Path) -> None:
    bindir = _fake_tool(tmp_path, "terraform", exit_code=0)
    data = _run("terraform-check", bindir, _tf_target(tmp_path))
    assert data["ok"] is True, describe(data)
    assert data["count"] == 0, describe(data)


# --- cargo-check -----------------------------------------------------------
#
# Real `cargo check --message-format=short --quiet` (rust 1.9x):
#
#   type error in src/sibling.rs, rc=101:
#     src/sibling.rs:1:27: error[E0308]: mismatched types: expected `i32` ...
#     error: could not compile `demo` (bin "demo") due to 1 previous error
#
#   unparseable Cargo.toml, rc=101:
#     error: unclosed table, expected `]`
#      --> Cargo.toml:1:9
#
# The manifest failure matches neither the short-format pattern nor anything
# about the .rs file under validation, and became `code: "compile"` attributed
# to it — a Rust compile error reported in a file the compiler never reached.
#
# The **cross-file attribution** visible in the first shape (a sibling's error
# published as this file's, with `source_context` read out of the sibling) is a
# misattribution rather than a misclassification, and is filed separately: every
# change in this PR reclassifies without removing anything from a caller's error
# list, and a `src_file == file` filter would remove errors. Different risk,
# different review.

def _rs_crate(tmp_path: Path) -> Path:
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n', encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    f = src / "main.rs"
    f.write_text("fn main() {\n    let x = 1;\n    drop(x);\n}\n", encoding="utf-8")
    return f


def test_cargo_manifest_error_is_a_tool_fault_not_a_compile_finding(tmp_path: Path) -> None:
    """cargo never reached the file; the error is in Cargo.toml."""
    bindir = _fake_tool(
        tmp_path, "cargo", exit_code=101,
        stderr=("error: unclosed table, expected `]`\n"
                " --> Cargo.toml:1:9\n  |\n1 | [package\n  |         ^\n"))
    data = _run("cargo-check", bindir, _rs_crate(tmp_path))
    _assert_tool_fault(data, 101, "unclosed table")


def test_cargo_summary_line_alone_is_a_tool_fault(tmp_path: Path) -> None:
    """cargo's own summary carries no location and names no source file."""
    bindir = _fake_tool(
        tmp_path, "cargo", exit_code=101,
        stderr='error: could not compile `demo` (bin "demo") due to 1 previous error\n')
    data = _run("cargo-check", bindir, _rs_crate(tmp_path))
    _assert_tool_fault(data, 101, "could not compile")


def test_cargo_silent_non_zero_exit_says_so(tmp_path: Path) -> None:
    bindir = _fake_tool(tmp_path, "cargo", exit_code=101)
    data = _run("cargo-check", bindir, _rs_crate(tmp_path))
    _assert_tool_fault(data, 101, "(no output)")


def test_cargo_a_real_compile_error_is_still_a_finding(tmp_path: Path) -> None:
    """The regression guard: the short-format diagnostic keeps its line, column
    and rustc error code."""
    target = _rs_crate(tmp_path)
    bindir = _fake_tool(
        tmp_path, "cargo", exit_code=101,
        stderr=(f"{target}:2:9: error[E0308]: mismatched types: expected `i32`\n"
                'error: could not compile `demo` (bin "demo") due to 1 previous error\n'))
    data = _run("cargo-check", bindir, target)
    assert data["ok"] is False, describe(data)
    assert data["errors"][0]["code"] == "E0308", describe(data)
    assert data["errors"][0]["line"] == 2, describe(data)
    assert data["errors"][0]["col"] == 9, describe(data)


def test_cargo_clean_crate_is_untouched(tmp_path: Path) -> None:
    bindir = _fake_tool(tmp_path, "cargo", exit_code=0)
    data = _run("cargo-check", bindir, _rs_crate(tmp_path))
    assert data["ok"] is True, describe(data)
    assert data["count"] == 0, describe(data)


# --- the contract, for every adapter touched -------------------------------

def test_the_verdict_json_is_the_only_thing_on_stdout(tmp_path: Path) -> None:
    """SCHEMA.md: one JSON object on stdout and nothing else — including on the
    new branch, whose input is arbitrary tool output that must not leak raw."""
    for name, binname in BINARIES.items():
        sub = tmp_path / name
        sub.mkdir()
        (sub / "Cargo.toml").write_text("[package]\\n", encoding="utf-8")
        bindir = _fake_tool(sub, binname, exit_code=1,
                            stdout="noise\nmore noise\n", stderr="noise\nmore noise\n")
        target = _target(sub, "subject.txt", "one\ntwo\n")
        env = dict(os.environ)
        env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
        r = subprocess.run(
            [sys.executable, str(ADAPTERS[name]), str(target)],
            capture_output=True, text=True, env=env,
            timeout=adapter_budget(ADAPTERS[name]))
        assert r.returncode == 0, f"{name}: {r.stderr}"
        payload = json.loads(r.stdout.strip())
        assert r.stdout.strip().count("\n") == 0, f"{name}: {r.stdout}"
        assert payload["errors"][0]["code"] == "adapter", describe(payload)


def test_every_reclassified_verdict_escapes_the_cache() -> None:
    """The reclassification only helps if the core stops freezing it.

    The validator cache is keyed on the file's content hash, so a verdict the
    adapter never obtained is by construction not a function of the file. #752
    put `"adapter"` in `_NONDETERMINISTIC_ERROR_CODES` for exactly this; these
    five adapters inherit it, and the guarantee is asserted here rather than
    assumed because every code this PR moves lands on that path.
    """
    import supertool

    for code in ("adapter",):
        fault = {"ok": False, "count": 1, "errors": [
            {"line": None, "col": None, "severity": "error", "code": code,
             "msg": "gofmt exited 2 without reporting anything about the file"}]}
        assert supertool._validator_result_is_cacheable(fault) is False

    for code in ("xml", "syntax", "formatting"):
        finding = {"ok": False, "count": 1, "errors": [
            {"line": 3, "col": None, "severity": "error", "code": code,
             "msg": "a real finding about the file"}]}
        assert supertool._validator_result_is_cacheable(finding) is True, code
