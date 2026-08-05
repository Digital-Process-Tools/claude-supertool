"""Adapters must not blame the file for their tool falling over (#753).

The sibling sweep #745 filed rather than half-fixed. Six adapters — and a
seventh, `ruby-check`, which that sweep recorded as CLEAN — turned "the tool
exited non-zero" into a syntax/format/compile finding about a file the tool may
never have opened, in `node-check`'s case with a line number lifted out of an
internal stack frame.

The rule, inherited from #745 rather than reinvented: **the tool is credited
with having spoken about the file when its output carries a recognisable
diagnostic** — for every tool here, a located diagnostic in its own format.
Anything else on a non-zero exit is `code: "adapter"` via `tool_fault()`,
keeping `ok: False` and `count: 1`. Reclassify, never silence. **Ambiguity falls
towards the file**, and a finding that cannot be placed reports `line: None`
rather than borrowing a number.

## Two layers, because the fixture cannot be portable and the rule must be

Each adapter's rule is string classification over its tool's output. That is
platform-independent by construction, so it is driven **in process** against
real transcripts — captured from gofmt (go1.x), node v22.22.1, GNU bash 3.2,
libxml 2.9, ruby 2.6, OpenTofu v1.12.5 and rust 1.9x — on every platform, with
no subprocess at all. Nothing below is invented to make a branch reachable.

Driving a whole adapter needs a tool that fails on demand, which means a fake
binary on `PATH`, and **that fixture is POSIX-only for the reason #752 found the
hard way.** Every adapter here spawns a list — `["gofmt", "-l", f]`,
`["cargo", "check", ...]` — so Python calls `CreateProcess` with an
extensionless program name. That API appends `.exe` and **does not consult
`PATHEXT`**, so a `gofmt.bat` shim in the first `PATH` entry is invisible: the
search walks straight past it and the real binary the `windows-latest` image
ships answers instead.

The first version of this file shipped that `.bat` launcher and went green on
macOS and ubuntu while 35 cases failed on all three Windows legs. **The
instructive part is what an unintercepted shim looks like, because it is not one
signature but two:**

- `terraform` and `cargo` exist on the runner, were handed a file they consider
  fine, and returned a *clean* verdict — `assert True is False`. A false clean
  is indistinguishable from a pass.
- `xmllint` also exists there, and the JSON-contract case handed it a
  `subject.txt` containing `noise` / `more noise`, which genuinely is not XML.
  The real libxml correctly reported `subject.txt:1: parser error : Start tag
  expected` — so that leg failed as `assert 'xml' == 'adapter'`, a *false
  finding* rather than a false clean.

Both mean the same thing: the fixture proved nothing. Hence
`test_the_fake_binary_is_the_one_that_answers`, parametrised over all seven —
without it a green run says nothing about whether the fakes were used at all.

What the Windows legs lose by skipping layer 2 is coverage of the spawn and
decode path, not of the rule. The rule — the thing this issue is about — is
proven on every platform by layer 1, and `test_validators.py` already exercises
the spawn path there against the real binaries.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _adapter_budget import adapter_budget  # noqa: E402
from _adapter_verdict import describe, verdict  # noqa: E402

VALIDATORS = Path(__file__).parent.parent / "validators"

ADAPTERS = {name: VALIDATORS / name / f"{name}.py" for name in (
    "xmllint", "bash-check", "node-check", "gofmt-check", "ruby-check",
    "terraform-check", "cargo-check")}

# The binary each adapter spawns, for the fixtures that stub it.
BINARIES = {
    "xmllint": "xmllint", "bash-check": "bash", "node-check": "node",
    "gofmt-check": "gofmt", "ruby-check": "ruby",
    "terraform-check": "terraform", "cargo-check": "cargo",
}

# A subject file per tool, so a leg that somehow reaches a real binary is handed
# input that tool considers valid rather than input it would rightly reject.
SUBJECTS = {
    "xmllint": ("subject.xml", "<a>\n  <b/>\n</a>\n"),
    "bash-check": ("subject.sh", "echo one\necho two\necho three\n"),
    "node-check": ("subject.js", "const a = 1;\nconst b = 2;\nconst c = 3;\n"),
    "gofmt-check": ("subject.go", 'package main\n\nfunc main() {\n\tprintln("hi")\n}\n'),
    "ruby-check": ("subject.rb", "x = 1\ny = 2\nz = 3\n"),
    "terraform-check": ("subject.tf", 'resource "null_resource" "a" {\n  count = 1\n}\n'),
    "cargo-check": ("src/main.rs", "fn main() {\n    let x = 1;\n    drop(x);\n}\n"),
}


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"{name.replace('-', '_')}_753", ADAPTERS[name])
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


xmllint = _load("xmllint")
bash_check = _load("bash-check")
node_check = _load("node-check")
gofmt_check = _load("gofmt-check")
ruby_check = _load("ruby-check")
terraform_check = _load("terraform-check")
cargo_check = _load("cargo-check")

sys.path.insert(0, str(VALIDATORS / "common"))
from refusal import tool_fault  # noqa: E402


# --- real transcripts ------------------------------------------------------
#
# xmllint --noout --nonet --noent (libxml 2.9)
XML_PARSER_ERROR = ("subject.xml:2: parser error : Opening and ending tag "
                    "mismatch: b line 2 and a\n  <b></a>\n         ^\n")
XML_NO_ENTITY = 'warning: failed to load external entity "subject.xml"\n'
XML_IO_ERROR = ('I/O error : Permission denied\n'
                'warning: failed to load external entity "subject.xml"\n')
XML_UNKNOWN_OPTION = ("Unknown option --bogusflag\n"
                      "Usage : xmllint [options] XMLfiles ...\n")

# bash -n (GNU bash 3.2)
SH_SYNTAX = "subject.sh: line 2: syntax error: unexpected end of file\n"
SH_TWO_DIAGNOSTICS = ("subject.sh: line 1: unexpected EOF while looking for matching `)'\n"
                      "subject.sh: line 2: syntax error: unexpected end of file\n")
SH_NO_SUCH_FILE = "bash: subject.sh: No such file or directory\n"      # exit 127
SH_PERMISSION = "bash: subject.sh: Permission denied\n"                # exit 126
SH_INVALID_OPTION = ("bash: --nope: invalid option\n"
                     "Usage:\tbash [GNU long option] [option] ...\n")  # exit 2

# gofmt -l (go1.x)
GO_PARSE_ERROR = "subject.go:3:12: expected ')', found '{'\n"
GO_STAT_FAILURE = "stat subject.go: no such file or directory\n"

# ruby -c (ruby 2.6)
RB_SYNTAX = "subject.rb:2: syntax error, unexpected end-of-input\n"
RB_LOAD_ERROR = "ruby: No such file or directory -- subject.rb (LoadError)\n"

# cargo check --message-format=short --quiet (rust 1.9x)
RS_MANIFEST_ERROR = ("error: unclosed table, expected `]`\n"
                     " --> Cargo.toml:1:9\n  |\n1 | [package\n  |         ^\n")
RS_SUMMARY_ONLY = 'error: could not compile `demo` (bin "demo") due to 1 previous error\n'

# node --check (v22.22.1). Node prints the path it resolved, so these are built
# against a real target path at call time rather than stored flat.
NODE_BAD_OPTION = "node: bad option: --nonexistent-flag\n"             # exit 9
NODE_SYNTAX_NO_LOCATION = "SyntaxError: Invalid or unexpected token\n"


def node_cannot_find_module(target: Path) -> str:
    """The filed bug's input. The loader frame on line 1 is what the old
    `re.search(r":(\\d+)")` mined for a line number."""
    return ("node:internal/modules/cjs/loader:1386\n"
            "  throw err;\n  ^\n\n"
            f"Error: Cannot find module '{target}'\n"
            "    at node:internal/modules/cjs/loader:1383:15\n"
            "    at node:internal/main/check_syntax:33:20 {\n"
            "  code: 'MODULE_NOT_FOUND',\n  requireStack: []\n}\n\n"
            "Node.js v22.22.1\n")


def node_syntax_error(target: Path, line: int) -> str:
    return (f"{target}:{line}\n"
            "const b = (;\n           ^\n\n"
            "SyntaxError: Unexpected token ';'\n"
            "    at wrapSafe (node:internal/modules/cjs/loader:1637:18)\n\n"
            "Node.js v22.22.1\n")


# terraform fmt -check -diff (OpenTofu v1.12.5). The diagnostics arrive wrapped
# in a box-drawing gutter and ANSI colour; both are real and both are stripped.
def tf_box(body: str) -> str:
    lines = "\n".join("\x1b[31m│\x1b[0m " + ln for ln in body.splitlines())
    return "\x1b[31m╷\x1b[0m\n" + lines + "\n\x1b[31m╵\x1b[0m\n"


TF_HCL_SYNTAX = tf_box("Error: Argument or block definition required\n\n"
                       '  on subject.tf line 2, in resource "null_resource":\n'
                       "   2:   count = \n\n"
                       "An argument or block definition is required here.\n")
TF_INVALID_PATH = tf_box("Error: Invalid file or directory path\n\n"
                         "No file or directory at subject.tf\n")
TF_FAILED_TO_OPEN = tf_box("Error: Failed to open file\n\n"
                           "Failed to read file subject.tf\n")
TF_BAD_FLAG = tf_box("Error: Failed to parse command-line flags\n\n"
                     "flag provided but not defined: -bogus\n")
TF_FMT_DIFF = ("subject.tf\n--- old/subject.tf\n+++ new/subject.tf\n"
               "@@ -1,3 +1,3 @@\n-count = 1\n+  count = 1\n")


# ===========================================================================
# Layer 1: the rule, in process, on every platform
# ===========================================================================

# The five adapters whose marker is "a located diagnostic in the tool's own
# format", expressed as one callable each so the rule can be asserted uniformly.
PARSERS = {
    "xmllint": lambda out, f: xmllint.parse_diagnostics(out, f),
    "bash-check": lambda out, f: bash_check.parse_diagnostics(out, f),
    "ruby-check": lambda out, f: ruby_check.parse_diagnostics(out, f),
    "gofmt-check": lambda out, f: gofmt_check.parse_diagnostics(out, f),
    "cargo-check": lambda out, f: cargo_check._parse_errors(out),
}


@pytest.mark.parametrize("tool, out, why", [
    ("xmllint", XML_NO_ENTITY, "a path libxml could not open was never parsed"),
    ("xmllint", XML_IO_ERROR, "an I/O error is not a statement about XML"),
    ("xmllint", XML_UNKNOWN_OPTION, "a usage dump is the least file-shaped output libxml emits"),
    ("xmllint", "", "a process that died without a word"),
    ("bash-check", SH_NO_SUCH_FILE, "exit 127 is 'could not execute', never a syntax verdict"),
    ("bash-check", SH_PERMISSION, "exit 126 likewise — the shell never parsed anything"),
    ("bash-check", SH_INVALID_OPTION, "bash rejected argv, not the script"),
    ("bash-check", "", "a process that died without a word"),
    ("ruby-check", RB_LOAD_ERROR, "a LoadError names no line in the file"),
    ("ruby-check", "", "the branch ruby-check already had right"),
    ("gofmt-check", GO_STAT_FAILURE, "gofmt could not stat the path, let alone parse it"),
    ("gofmt-check", "", "a process that died without a word"),
    ("cargo-check", RS_MANIFEST_ERROR, "the error is in Cargo.toml; no .rs file was reached"),
    ("cargo-check", RS_SUMMARY_ONLY, "cargo's summary carries no location and names no file"),
    ("cargo-check", "", "a process that died without a word"),
])
def test_output_with_no_located_diagnostic_is_not_a_verdict_about_the_file(
        tool: str, out: str, why: str) -> None:
    found = PARSERS[tool](out, SUBJECTS[tool][0])
    assert found == [], f"{tool}: {why} — got {found!r}"


@pytest.mark.parametrize("tool, out, lines", [
    ("xmllint", XML_PARSER_ERROR, [2]),
    ("bash-check", SH_SYNTAX, [2]),
    # bash emits one line per problem; the count must not collapse to one.
    ("bash-check", SH_TWO_DIAGNOSTICS, [1, 2]),
    ("ruby-check", RB_SYNTAX, [2]),
    ("gofmt-check", GO_PARSE_ERROR, [3]),
])
def test_a_located_diagnostic_is_a_finding_and_keeps_its_own_line(
        tool: str, out: str, lines: list[int]) -> None:
    found = PARSERS[tool](out, SUBJECTS[tool][0])
    assert [e["line"] for e in found] == lines, f"{tool}: wrong lines from {out!r}"
    assert all(e["code"] != "adapter" for e in found), f"{tool}: a finding was relabelled"


def test_gofmt_keeps_the_column_it_used_to_discard() -> None:
    """gofmt hands over `line:col` and the old branch reported neither."""
    found = gofmt_check.parse_diagnostics(GO_PARSE_ERROR, "subject.go")
    assert len(found) == 1
    assert found[0]["line"] == 3 and found[0]["col"] == 12
    assert found[0]["code"] == "syntax"


def test_ruby_does_not_count_its_own_summary_line_as_a_finding() -> None:
    """`N errors found` matches the diagnostic shape and is not one."""
    found = ruby_check.parse_diagnostics(
        "subject.rb:2: syntax error, unexpected end-of-input\n"
        "subject.rb:3: 1 error found\n", "subject.rb")
    assert [e["line"] for e in found] == [2]


def test_cargo_keeps_the_rustc_error_code() -> None:
    found = cargo_check._parse_errors(
        "src/main.rs:2:9: error[E0308]: mismatched types: expected `i32`\n"
        + RS_SUMMARY_ONLY)
    assert len(found) == 1
    assert found[0]["code"] == "E0308"
    assert found[0]["line"] == 2 and found[0]["col"] == 9


def test_cargo_ignores_warnings() -> None:
    """A warning is not a reason to fail the file."""
    assert cargo_check._parse_errors(
        "src/main.rs:2:9: warning[unused]: unused variable `x`\n") == []


# --- node-check: the location has to be matched, not merely found ----------

def test_node_does_not_lift_a_line_number_out_of_an_internal_frame(tmp_path: Path) -> None:
    """The filed bug, at the layer that decides it.

    `node:internal/modules/cjs/loader:1386` is the first digit-colon in the
    output and became the reported line, with source context rendered for line
    1386 of a three-line file.
    """
    target = tmp_path / "subject.js"
    target.write_text("const a = 1;\n", encoding="utf-8")
    out = node_cannot_find_module(target)
    line = node_check.diagnostic_line(out, str(target))
    assert line is None, f"a line was invented: {line}"
    assert node_check.spoke_about_file(out, line) is False


@pytest.mark.parametrize("out, why", [
    (NODE_BAD_OPTION, "node rejected argv; no file was opened"),
    ("", "a process that died without a word"),
])
def test_node_output_with_no_diagnostic_is_not_a_verdict(
        tmp_path: Path, out: str, why: str) -> None:
    target = tmp_path / "subject.js"
    target.write_text("const a = 1;\n", encoding="utf-8")
    line = node_check.diagnostic_line(out, str(target))
    assert line is None
    assert node_check.spoke_about_file(out, line) is False, why


def test_node_reads_the_line_from_the_location_naming_this_file(tmp_path: Path) -> None:
    target = tmp_path / "subject.js"
    target.write_text("const a = 1;\nconst b = 2;\n", encoding="utf-8")
    out = node_syntax_error(target, 2)
    line = node_check.diagnostic_line(out, str(target))
    assert line == 2
    assert node_check.spoke_about_file(out, line) is True


def test_node_ignores_a_location_naming_some_other_file(tmp_path: Path) -> None:
    """The exclusion is by identity, not by shape: a `path:digits` header is
    only this file's location when the path resolves to this file."""
    target = tmp_path / "subject.js"
    target.write_text("const a = 1;\n", encoding="utf-8")
    other = tmp_path / "elsewhere.js"
    other.write_text("const a = 1;\n", encoding="utf-8")
    assert node_check.diagnostic_line(f"{other}:9\n", str(target)) is None


def test_node_ambiguity_falls_towards_the_file(tmp_path: Path) -> None:
    """A `SyntaxError` node's report shape cannot place is still a finding —
    and is reported with no line rather than a borrowed one.

    The deliberate direction, not an accident of the regex. Reclassifying is
    one fix and inventing a location is another.
    """
    target = tmp_path / "subject.js"
    target.write_text("const a = 1;\n", encoding="utf-8")
    line = node_check.diagnostic_line(NODE_SYNTAX_NO_LOCATION, str(target))
    assert line is None
    assert node_check.spoke_about_file(NODE_SYNTAX_NO_LOCATION, line) is True


# --- terraform-check: two markers, and the exit codes are real -------------

@pytest.mark.parametrize("out, why", [
    (TF_INVALID_PATH, "terraform could not find the path it was given"),
    (TF_FAILED_TO_OPEN, "terraform could not read the file"),
    (TF_BAD_FLAG, "terraform could not parse its own command line"),
    ("", "a process that died without a word"),
])
def test_terraform_failures_are_not_located_and_are_not_formatting_claims(
        out: str, why: str) -> None:
    body = terraform_check.plain(out)
    assert terraform_check.diagnostic_line(body) is None, why
    assert terraform_check.is_fmt_verdict("", "subject.tf") is False


def test_terraform_reads_the_line_out_of_a_box_drawn_hcl_error() -> None:
    """`on <file> line N` is the only thing in an Error block that places it,
    and it arrives behind a gutter and ANSI colour."""
    body = terraform_check.plain(TF_HCL_SYNTAX)
    assert "\x1b" not in body and "│" not in body
    assert terraform_check.diagnostic_line(body) == 2


def test_terraform_recognises_the_fmt_diff_as_the_formatting_verdict() -> None:
    assert terraform_check.is_fmt_verdict(TF_FMT_DIFF, "subject.tf") is True


def test_terraform_does_not_read_stray_stdout_as_a_formatting_verdict() -> None:
    """Keyed on the diff body or the echoed path, not on "stdout is non-empty"
    — the latter is the mistake being fixed, one notch narrower."""
    assert terraform_check.is_fmt_verdict("noise\nmore noise\n", "subject.tf") is False


def test_terraform_plain_never_renders_blank_on_real_output() -> None:
    for out in (TF_HCL_SYNTAX, TF_INVALID_PATH, TF_FAILED_TO_OPEN, TF_BAD_FLAG):
        assert terraform_check.plain(out).strip()


# --- the fault message, shared by all seven --------------------------------

def test_the_fault_message_names_the_exit_code_and_the_output() -> None:
    msg = tool_fault("gofmt -l", 2, "stat subject.go: no such file or directory")
    assert "2" in msg and "no such file" in msg


def test_the_fault_message_never_renders_blank() -> None:
    """A tool that dies without a word is the input a template built around the
    output renders as an empty tail. It is stated instead."""
    msg = tool_fault("cargo check", 101, "")
    assert "101" in msg
    assert msg.strip()
    assert "no output" in msg


def test_every_reclassified_verdict_escapes_the_cache() -> None:
    """The reclassification only helps if the core stops freezing it.

    The validator cache is keyed on the file's content hash, so a verdict the
    adapter never obtained is by construction not a function of the file. #752
    put `"adapter"` in `_NONDETERMINISTIC_ERROR_CODES`; these seven adapters
    inherit it, and the guarantee is asserted rather than assumed because every
    code this PR moves lands on that path.
    """
    import supertool

    fault = {"ok": False, "count": 1, "errors": [
        {"line": None, "col": None, "severity": "error", "code": "adapter",
         "msg": "gofmt -l exited 2 without reporting anything about the file"}]}
    assert supertool._validator_result_is_cacheable(fault) is False

    for code in ("xml", "syntax", "formatting", "compile", "E0308"):
        finding = {"ok": False, "count": 1, "errors": [
            {"line": 3, "col": None, "severity": "error", "code": code,
             "msg": "a real finding about the file"}]}
        assert supertool._validator_result_is_cacheable(finding) is True, code


# ===========================================================================
# Layer 2: the whole adapter, driven by fake binaries on PATH
# ===========================================================================
#
# POSIX only. See the module docstring: `CreateProcess` ignores `PATHEXT` for an
# extensionless program name, so a `.bat` shim cannot intercept these spawns on
# Windows and the runner's real binaries answer instead — as a false clean where
# they accept the subject file, and as a false *finding* where they reject it.

posix_only = pytest.mark.skipif(
    os.name == "nt",
    reason=("a fake binary on PATH cannot intercept these adapters' "
            "extensionless list spawns on Windows: CreateProcess appends .exe "
            "and ignores PATHEXT, so a .bat shim is skipped and the runner's "
            "real gofmt/node/xmllint/terraform/cargo answers. The "
            "classification these cases drive is covered in process above, on "
            "every platform."))


def _fake_tool(tmp_path: Path, name: str, *, exit_code: int,
               stdout: str = "", stderr: str = "") -> Path:
    """A `name` on PATH printing the given streams and exiting `exit_code`."""
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
    launcher = bindir / name
    launcher.write_text(
        "#!/bin/sh\n"
        f"exec '{sys.executable}' '{script}' \"$@\"\n", encoding="utf-8")
    launcher.chmod(0o755)
    return bindir


def _target(tmp_path: Path, tool: str) -> Path:
    """The subject file, plus whatever the adapter needs to reach it."""
    rel, body = SUBJECTS[tool]
    f = tmp_path / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body, encoding="utf-8")
    if tool == "cargo-check":
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "demo"\nversion = "0.1.0"\n', encoding="utf-8")
    return f


def _spawn(bindir: Path, tool: str, target: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    return subprocess.run(
        [sys.executable, str(ADAPTERS[tool]), str(target)],
        capture_output=True, text=True, env=env,
        timeout=adapter_budget(ADAPTERS[tool]), encoding="utf-8", errors="replace")


def _run(tool: str, bindir: Path, target: Path) -> dict:
    return verdict(_spawn(bindir, tool, target), adapter=tool)


def _assert_tool_fault(data: dict, exit_code: int, needle: str) -> None:
    """`adapter` reclassifies; it must never silence, and never invent a line.

    Asserted identically for every adapter, because the guarantee belongs to the
    contract and not to any one tool: still one error, still `ok: False`, still
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


@posix_only
@pytest.mark.parametrize("tool", sorted(ADAPTERS))
def test_the_fake_binary_is_the_one_that_answers(tmp_path: Path, tool: str) -> None:
    """The guard the first version of this file lacked.

    Every case below is meaningless if the shim is not the binary that ran, and
    an unintercepted shim does not fail loudly — it produces a clean verdict
    where the real tool accepts the subject file, or a real finding where it
    rejects one. Both are indistinguishable from a pass unless something
    asserts the interception itself. So this uses an exit code and a marker no
    real gofmt, node, bash, xmllint, ruby, terraform or cargo would emit.
    """
    bindir = _fake_tool(tmp_path, BINARIES[tool], exit_code=42,
                        stdout="I-AM-THE-FAKE\n", stderr="I-AM-THE-FAKE\n")
    data = _run(tool, bindir, _target(tmp_path, tool))
    assert data["ok"] is False, describe(data)
    assert "I-AM-THE-FAKE" in json.dumps(data), describe(data)
    assert "42" in json.dumps(data), describe(data)


# --- xmllint ---------------------------------------------------------------

@posix_only
@pytest.mark.parametrize("stderr, exit_code, needle", [
    (XML_NO_ENTITY, 1, "failed to load external entity"),
    (XML_UNKNOWN_OPTION, 1, "Unknown option"),
    (XML_IO_ERROR, 1, "Permission denied"),
    ("", 139, "(no output)"),
])
def test_xmllint_non_verdict_exits_become_tool_faults(
        tmp_path: Path, stderr: str, exit_code: int, needle: str) -> None:
    bindir = _fake_tool(tmp_path, "xmllint", exit_code=exit_code, stderr=stderr)
    data = _run("xmllint", bindir, _target(tmp_path, "xmllint"))
    _assert_tool_fault(data, exit_code, needle)


@posix_only
def test_xmllint_a_real_parser_error_is_still_a_finding(tmp_path: Path) -> None:
    """The regression guard: reclassifying is only safe if genuine findings stay."""
    bindir = _fake_tool(tmp_path, "xmllint", exit_code=1, stderr=XML_PARSER_ERROR)
    data = _run("xmllint", bindir, _target(tmp_path, "xmllint"))
    assert data["ok"] is False, describe(data)
    assert data["errors"][0]["code"] == "xml", describe(data)
    assert data["errors"][0]["line"] == 2, describe(data)
    assert data["errors"][0]["source_context"], describe(data)


# --- bash-check ------------------------------------------------------------

@posix_only
@pytest.mark.parametrize("stderr, exit_code, needle", [
    (SH_NO_SUCH_FILE, 127, "No such file or directory"),
    (SH_PERMISSION, 126, "Permission denied"),
    (SH_INVALID_OPTION, 2, "invalid option"),
    ("", 139, "(no output)"),
])
def test_bash_non_verdict_exits_become_tool_faults(
        tmp_path: Path, stderr: str, exit_code: int, needle: str) -> None:
    bindir = _fake_tool(tmp_path, "bash", exit_code=exit_code, stderr=stderr)
    data = _run("bash-check", bindir, _target(tmp_path, "bash-check"))
    _assert_tool_fault(data, exit_code, needle)


@posix_only
def test_bash_a_real_syntax_error_is_still_a_finding(tmp_path: Path) -> None:
    bindir = _fake_tool(tmp_path, "bash", exit_code=2, stderr=SH_SYNTAX)
    data = _run("bash-check", bindir, _target(tmp_path, "bash-check"))
    assert data["errors"][0]["code"] == "syntax", describe(data)
    assert data["errors"][0]["line"] == 2, describe(data)
    assert data["errors"][0]["source_context"], describe(data)


@posix_only
def test_bash_reports_every_located_diagnostic(tmp_path: Path) -> None:
    bindir = _fake_tool(tmp_path, "bash", exit_code=2, stderr=SH_TWO_DIAGNOSTICS)
    data = _run("bash-check", bindir, _target(tmp_path, "bash-check"))
    assert data["count"] == 2, describe(data)
    assert [e["line"] for e in data["errors"]] == [1, 2], describe(data)


# --- node-check ------------------------------------------------------------

@posix_only
def test_node_cannot_find_module_does_not_become_a_syntax_error(tmp_path: Path) -> None:
    target = _target(tmp_path, "node-check")
    bindir = _fake_tool(tmp_path, "node", exit_code=1,
                        stderr=node_cannot_find_module(target))
    data = _run("node-check", bindir, target)
    _assert_tool_fault(data, 1, "Cannot find module")


@posix_only
@pytest.mark.parametrize("stderr, exit_code, needle", [
    (NODE_BAD_OPTION, 9, "bad option"),
    ("", 134, "(no output)"),
])
def test_node_non_verdict_exits_become_tool_faults(
        tmp_path: Path, stderr: str, exit_code: int, needle: str) -> None:
    bindir = _fake_tool(tmp_path, "node", exit_code=exit_code, stderr=stderr)
    data = _run("node-check", bindir, _target(tmp_path, "node-check"))
    _assert_tool_fault(data, exit_code, needle)


@posix_only
def test_node_a_real_syntax_error_keeps_its_line(tmp_path: Path) -> None:
    target = _target(tmp_path, "node-check")
    bindir = _fake_tool(tmp_path, "node", exit_code=1,
                        stderr=node_syntax_error(target, 2))
    data = _run("node-check", bindir, target)
    assert data["errors"][0]["code"] == "syntax", describe(data)
    assert data["errors"][0]["line"] == 2, describe(data)
    assert "SyntaxError" in data["errors"][0]["msg"], describe(data)
    assert data["errors"][0]["source_context"], describe(data)


@posix_only
def test_node_a_syntax_error_without_a_location_is_still_a_finding(tmp_path: Path) -> None:
    bindir = _fake_tool(tmp_path, "node", exit_code=1,
                        stderr=NODE_SYNTAX_NO_LOCATION)
    data = _run("node-check", bindir, _target(tmp_path, "node-check"))
    assert data["errors"][0]["code"] == "syntax", describe(data)
    assert data["errors"][0]["line"] is None, describe(data)


# --- gofmt-check -----------------------------------------------------------

@posix_only
@pytest.mark.parametrize("stderr, exit_code, needle", [
    (GO_STAT_FAILURE, 2, "no such file or directory"),
    ("", 2, "(no output)"),
])
def test_gofmt_non_verdict_exits_become_tool_faults(
        tmp_path: Path, stderr: str, exit_code: int, needle: str) -> None:
    bindir = _fake_tool(tmp_path, "gofmt", exit_code=exit_code, stderr=stderr)
    data = _run("gofmt-check", bindir, _target(tmp_path, "gofmt-check"))
    _assert_tool_fault(data, exit_code, needle)


@posix_only
def test_gofmt_a_real_parse_error_is_a_finding_with_its_line_and_col(tmp_path: Path) -> None:
    bindir = _fake_tool(tmp_path, "gofmt", exit_code=2, stderr=GO_PARSE_ERROR)
    data = _run("gofmt-check", bindir, _target(tmp_path, "gofmt-check"))
    assert data["ok"] is False, describe(data)
    assert data["errors"][0]["code"] == "syntax", describe(data)
    assert data["errors"][0]["line"] == 3, describe(data)
    assert data["errors"][0]["col"] == 12, describe(data)
    assert data["errors"][0]["source_context"], describe(data)


@posix_only
def test_gofmt_unformatted_file_is_untouched(tmp_path: Path) -> None:
    """The `-l` signal is stdout naming the path at exit **0** — the branch that
    was already right, and the one a returncode-driven rewrite could break."""
    bindir = _fake_tool(tmp_path, "gofmt", exit_code=0, stdout="subject.go\n")
    data = _run("gofmt-check", bindir, _target(tmp_path, "gofmt-check"))
    assert data["ok"] is False, describe(data)
    assert data["errors"][0]["code"] == "formatting", describe(data)


@posix_only
def test_gofmt_clean_file_is_untouched(tmp_path: Path) -> None:
    bindir = _fake_tool(tmp_path, "gofmt", exit_code=0)
    data = _run("gofmt-check", bindir, _target(tmp_path, "gofmt-check"))
    assert data["ok"] is True, describe(data)
    assert data["count"] == 0, describe(data)


# --- ruby-check ------------------------------------------------------------

@posix_only
@pytest.mark.parametrize("stderr, exit_code, needle", [
    (RB_LOAD_ERROR, 1, "LoadError"),
    ("", 139, "(no output)"),
])
def test_ruby_non_verdict_exits_become_tool_faults(
        tmp_path: Path, stderr: str, exit_code: int, needle: str) -> None:
    bindir = _fake_tool(tmp_path, "ruby", exit_code=exit_code, stderr=stderr)
    data = _run("ruby-check", bindir, _target(tmp_path, "ruby-check"))
    _assert_tool_fault(data, exit_code, needle)


@posix_only
def test_ruby_a_real_syntax_error_is_still_a_finding(tmp_path: Path) -> None:
    bindir = _fake_tool(tmp_path, "ruby", exit_code=1, stderr=RB_SYNTAX)
    data = _run("ruby-check", bindir, _target(tmp_path, "ruby-check"))
    assert data["errors"][0]["code"] == "syntax", describe(data)
    assert data["errors"][0]["line"] == 2, describe(data)


# --- terraform-check -------------------------------------------------------

@posix_only
@pytest.mark.parametrize("stderr, exit_code, needle", [
    (TF_INVALID_PATH, 2, "Invalid file or directory path"),
    (TF_FAILED_TO_OPEN, 2, "Failed to open file"),
    (TF_BAD_FLAG, 2, "command-line flags"),
    ("", 2, "(no output)"),
])
def test_terraform_non_verdict_exits_are_not_formatting_claims(
        tmp_path: Path, stderr: str, exit_code: int, needle: str) -> None:
    """A "needs terraform fmt formatting" verdict on a path terraform could not
    find is the false-and-specific claim the issue names."""
    bindir = _fake_tool(tmp_path, "terraform", exit_code=exit_code, stderr=stderr)
    data = _run("terraform-check", bindir, _target(tmp_path, "terraform-check"))
    _assert_tool_fault(data, exit_code, needle)


@posix_only
def test_terraform_hcl_syntax_error_is_a_located_finding_not_a_formatting_one(
        tmp_path: Path) -> None:
    """An unparseable .tf file is a finding — but telling its author to run
    `terraform fmt` is advice that cannot work, and the line was thrown away."""
    bindir = _fake_tool(tmp_path, "terraform", exit_code=2, stderr=TF_HCL_SYNTAX)
    data = _run("terraform-check", bindir, _target(tmp_path, "terraform-check"))
    assert data["ok"] is False, describe(data)
    assert data["errors"][0]["code"] == "syntax", describe(data)
    assert data["errors"][0]["line"] == 2, describe(data)
    assert "fmt" not in data["errors"][0]["msg"], describe(data)
    assert data["errors"][0]["source_context"], describe(data)


@posix_only
def test_terraform_unformatted_file_is_still_a_formatting_finding(tmp_path: Path) -> None:
    """The regression guard, at terraform's real exit code for it: 3, with the
    diff on stdout. This is the only branch that was ever right."""
    bindir = _fake_tool(tmp_path, "terraform", exit_code=3, stdout=TF_FMT_DIFF)
    data = _run("terraform-check", bindir, _target(tmp_path, "terraform-check"))
    assert data["ok"] is False, describe(data)
    assert data["errors"][0]["code"] == "formatting", describe(data)
    assert "count" in data["errors"][0]["msg"], describe(data)


@posix_only
def test_terraform_clean_file_is_untouched(tmp_path: Path) -> None:
    bindir = _fake_tool(tmp_path, "terraform", exit_code=0)
    data = _run("terraform-check", bindir, _target(tmp_path, "terraform-check"))
    assert data["ok"] is True, describe(data)
    assert data["count"] == 0, describe(data)


# --- cargo-check -----------------------------------------------------------

@posix_only
@pytest.mark.parametrize("stderr, exit_code, needle", [
    (RS_MANIFEST_ERROR, 101, "unclosed table"),
    (RS_SUMMARY_ONLY, 101, "could not compile"),
    ("", 101, "(no output)"),
])
def test_cargo_non_verdict_exits_become_tool_faults(
        tmp_path: Path, stderr: str, exit_code: int, needle: str) -> None:
    bindir = _fake_tool(tmp_path, "cargo", exit_code=exit_code, stderr=stderr)
    data = _run("cargo-check", bindir, _target(tmp_path, "cargo-check"))
    _assert_tool_fault(data, exit_code, needle)


@posix_only
def test_cargo_a_real_compile_error_is_still_a_finding(tmp_path: Path) -> None:
    target = _target(tmp_path, "cargo-check")
    bindir = _fake_tool(
        tmp_path, "cargo", exit_code=101,
        stderr=f"{target}:2:9: error[E0308]: mismatched types: expected `i32`\n"
               + RS_SUMMARY_ONLY)
    data = _run("cargo-check", bindir, target)
    assert data["ok"] is False, describe(data)
    assert data["errors"][0]["code"] == "E0308", describe(data)
    assert data["errors"][0]["line"] == 2, describe(data)
    assert data["errors"][0]["col"] == 9, describe(data)


@posix_only
def test_cargo_clean_crate_is_untouched(tmp_path: Path) -> None:
    bindir = _fake_tool(tmp_path, "cargo", exit_code=0)
    data = _run("cargo-check", bindir, _target(tmp_path, "cargo-check"))
    assert data["ok"] is True, describe(data)
    assert data["count"] == 0, describe(data)


# --- the contract, for every adapter touched -------------------------------

@posix_only
@pytest.mark.parametrize("tool", sorted(ADAPTERS))
def test_the_verdict_json_is_the_only_thing_on_stdout(tmp_path: Path, tool: str) -> None:
    """SCHEMA.md: one JSON object on stdout and nothing else — including on the
    new branch, whose input is arbitrary tool output that must not leak raw."""
    bindir = _fake_tool(tmp_path, BINARIES[tool], exit_code=1,
                        stdout="noise\nmore noise\n", stderr="noise\nmore noise\n")
    r = _spawn(bindir, tool, _target(tmp_path, tool))
    assert r.returncode == 0, f"{tool}: {r.stderr}"
    payload = json.loads(r.stdout.strip())
    assert r.stdout.strip().count("\n") == 0, f"{tool}: {r.stdout}"
    assert payload["errors"][0]["code"] == "adapter", describe(payload)
