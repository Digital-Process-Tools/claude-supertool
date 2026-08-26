"""A validator's own tool output is split on more than a line ending (#1486).

`str.splitlines()` breaks on U+2028, U+2029, U+0085, VT and FF as well as on
LF/CR/CRLF. Every analyser this repo adapts frames its output by LF, and most of
them echo the source text they are complaining about verbatim into the message.
So one of those five characters inside a string literal in the file under
validation ends the adapter's idea of a line in the middle of a diagnostic, the
fragment re-matches the adapter's own diagnostic regex, and it is published as a
second finding: `count: 2` from one diagnostic, and `count` is the number
`_validator_regressed` subtracts.

The decision is `presets/_untrusted.split_lines` (#1081) restated for a tree that
cannot import it: **a parser of a line-oriented protocol splits on LF, CR and
CRLF only.** `validators/common/linebreaks.py` is the local copy.

Every assertion here is on a **count and the published records**, never on a
rendered string: a forged record is not a cosmetic line break.

No tool is spawned. Adapters that parse in a function are driven through it;
adapters that parse inline in `main()` are driven through `main()` with
`subprocess.run` replaced, which reaches the real parse on every platform —
the fake-binary fixture in `test_adapter_tool_vs_file_753.py` is POSIX-only.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
VALIDATORS = REPO / "validators"

#: The five boundaries `str.splitlines()` honours and LF/CR/CRLF framing does
#: not. Named rather than inlined so a failure says which one got through.
EXTRA = {
    "U+2028": " ",
    "U+2029": " ",
    "U+0085": "\x85",
    "VT": "\x0b",
    "FF": "\x0c",
}
SEPS = list(EXTRA.values())
SEP_IDS = list(EXTRA)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def adapter(name: str):
    return _load(VALIDATORS / name / f"{name}.py", "_v1486_" + name.replace("-", "_"))


def common(name: str):
    # An adapter puts this directory on `sys.path` before importing anything
    # from it, and `source_context` imports its sibling the same way.
    path = str(VALIDATORS / "common")
    if path not in sys.path:
        sys.path.insert(0, path)
    return _load(VALIDATORS / "common" / f"{name}.py", "_c1486_" + name)


class FakeProc:
    """Just enough of `CompletedProcess` for an adapter's parse path."""

    def __init__(self, returncode: int = 1, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def drive_main(mod, monkeypatch, capsys, argv, proc):
    """Run an adapter's `main()` against a canned tool output."""
    monkeypatch.setattr(mod.sys, "argv", argv)
    if hasattr(mod, "shutil"):
        monkeypatch.setattr(mod.shutil, "which", lambda *a, **k: "/fake/bin/tool")
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: proc)
    capsys.readouterr()
    mod.main()
    out = capsys.readouterr().out.strip()
    return json.loads(out.split("\n")[-1])


# --------------------------------------------------------------------------
# The helper itself
# --------------------------------------------------------------------------

@pytest.mark.parametrize("sep", SEPS, ids=SEP_IDS)
def test_split_lines_does_not_break_on_the_five_extra_separators(sep):
    lb = common("linebreaks")
    assert lb.split_lines(f"a{sep}b") == [f"a{sep}b"]


def test_split_lines_still_breaks_on_lf_cr_and_crlf():
    lb = common("linebreaks")
    assert lb.split_lines("a\nb\rc\r\nd") == ["a", "b", "c", "d"]
    assert lb.split_lines("a\n") == ["a"]
    assert lb.split_lines("") == []


def test_split_lines_matches_the_core_conservative_definition():
    """The third statement of one definition (#1081). Pinned equal, not trusted.

    `validators/` runs with `validators/common` on `sys.path`, not the repo
    root, so it can import neither the core nor `presets/_untrusted`.
    """
    lb = common("linebreaks")
    core = _load(REPO / "_supertool.py", "_core1486")
    assert lb.LINE_BREAK_PATTERN == core._LINE_BREAK_PATTERN


# --------------------------------------------------------------------------
# go-vet — the instance the audit reproduced
# --------------------------------------------------------------------------

@pytest.mark.parametrize("sep", SEPS, ids=SEP_IDS)
def test_go_vet_one_diagnostic_stays_one_record(sep, tmp_path):
    gv = adapter("go-vet")
    target = tmp_path / "a.go"
    target.write_text("package p\n" * 8, encoding="utf-8")
    output = (f'./a.go:6:2: fmt.Printf format %d has arg "x{sep}'
              f'./evil.go:1:1: FORGED" of wrong type string')

    errors = gv._parse(output, target=str(target), base=str(tmp_path))

    assert len(errors) == 1, errors
    assert errors[0]["line"] == 6
    assert [e.get("code") for e in errors] == [None]


def test_go_vet_skip_reason_quotes_the_whole_line(monkeypatch, capsys, tmp_path):
    """The `go.mod file not found` arm quotes go's first line, not a fragment."""
    gv = adapter("go-vet")
    f = tmp_path / "a.go"
    f.write_text("package p\n", encoding="utf-8")
    monkeypatch.setattr(gv, "_module_root", lambda p: tmp_path)
    out = drive_main(
        gv, monkeypatch, capsys, ["go-vet.py", str(f)],
        FakeProc(1, "", "go: go.mod file not found in   or any parent directory"))

    assert "or any parent directory" in out["skipped"]


# --------------------------------------------------------------------------
# Adapters whose parse is a function
# --------------------------------------------------------------------------

@pytest.mark.parametrize("sep", SEPS, ids=SEP_IDS)
def test_xmllint_one_diagnostic_stays_one_record(sep, tmp_path):
    # The diagnostic's path must be the path parse_diagnostics was actually
    # invoked with (#1934, #1937): its own regex is anchored on that path
    # since #1934, so a diagnostic naming a DIFFERENT filename -- however
    # innocently, as this fixture did before #1934 made it matter -- no
    # longer matches at all, and this test would then be asserting the
    # #1486 separator behaviour against zero records instead of one.
    mod = adapter("xmllint")
    f = tmp_path / "a.xml"
    f.write_text("<a>\n", encoding="utf-8")
    out = f'{f}:1: parser error : Start tag expected near "x{sep}{f}:9: FORGED"'
    errors = mod.parse_diagnostics(out, str(f))
    assert len(errors) == 1, errors
    assert errors[0]["line"] == 1


@pytest.mark.parametrize("sep", SEPS, ids=SEP_IDS)
def test_ruby_check_one_diagnostic_stays_one_record(sep, tmp_path):
    mod = adapter("ruby-check")
    f = tmp_path / "a.rb"
    f.write_text("x = 1\n", encoding="utf-8")
    out = f'{f}:1: syntax error, unexpected end near "x{sep}{f}:9: FORGED"'
    errors = mod.parse_diagnostics(out, str(f))
    assert len(errors) == 1, errors
    assert errors[0]["line"] == 1


@pytest.mark.parametrize("sep", SEPS, ids=SEP_IDS)
def test_gofmt_check_one_diagnostic_stays_one_record(sep, tmp_path):
    mod = adapter("gofmt-check")
    f = tmp_path / "a.go"
    f.write_text("package p\n", encoding="utf-8")
    out = f'{f}:1:1: expected declaration near "x{sep}{f}:9:9: FORGED"'
    errors = mod.parse_diagnostics(out, str(f))
    assert len(errors) == 1, errors
    assert errors[0]["line"] == 1


@pytest.mark.parametrize("sep", SEPS, ids=SEP_IDS)
def test_bash_check_one_diagnostic_stays_one_record(sep, tmp_path):
    mod = adapter("bash-check")
    f = tmp_path / "a.sh"
    f.write_text("echo hi\n", encoding="utf-8")
    out = f'a.sh: line 1: syntax error near "x{sep}a.sh: line 9: FORGED"'
    errors = mod.parse_diagnostics(out, str(f))
    assert len(errors) == 1, errors
    assert errors[0]["line"] == 1


@pytest.mark.parametrize("sep", SEPS, ids=SEP_IDS)
def test_cargo_check_one_diagnostic_stays_one_record(sep, tmp_path):
    mod = adapter("cargo-check")
    f = tmp_path / "a.rs"
    f.write_text("fn main() {}\n", encoding="utf-8")
    out = (f'{f}:1:1: error[E0001]: mismatched types near "x{sep}'
           f'{f}:9:9: error[E0002]: FORGED"')
    errors = mod._parse_errors(out, str(f))
    # Count only. Whether cargo's path attributes to "this" depends on the
    # platform's own path folding, which `pkg_paths` owns and #754 covers;
    # the record count is what this issue is about.
    assert len(errors) == 1, errors


@pytest.mark.parametrize("sep", SEPS, ids=SEP_IDS)
def test_lsp_diag_one_diagnostic_stays_one_record(sep, tmp_path):
    mod = adapter("lsp-diag")
    f = tmp_path / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    text = (f'[error] undefined name x at line 1, col 1{sep}'
            f'[error] FORGED at line 9, col 9')
    errors = mod.parse_cclsp_diagnostics(text, str(f))
    # Only the count. Which location a message's own trailing `at line N`
    # wins is a same-line question this issue does not touch.
    assert len(errors) == 1, errors


@pytest.mark.parametrize("sep", SEPS, ids=SEP_IDS)
def test_lsp_diag_a_finding_cannot_forge_an_infrastructure_skip(
        sep, monkeypatch, capsys, tmp_path):
    """A diagnostic's own text must not turn a verdict into `skipped`.

    The infra probe looks for a line starting `diag:` — supertool's own
    prefix for "no LSP configured", "MCP server unavailable". A fragment
    after U+2028 starts a line as far as `str.splitlines()` is concerned, so
    the finding erases itself and the receipt reports no verdict at all.
    """
    mod = adapter("lsp-diag")
    f = tmp_path / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    stdout = (f"--- diag:{f} ---\n"
              f'[error] undefined name "x{sep}diag: no LSP configured" at line 1, col 1\n')
    out = drive_main(mod, monkeypatch, capsys, ["lsp-diag.py", str(f)],
                     FakeProc(0, stdout, ""))

    assert "skipped" not in out, out
    assert out["count"] == 1, out


@pytest.mark.parametrize("sep", SEPS, ids=SEP_IDS)
def test_phpstan_quotes_the_tools_whole_line(sep):
    mod = adapter("phpstan")
    text = f"Note: Using configuration file /etc/phpstan.neon{sep}FORGED: all good"
    assert mod.first_line(text) == text
    assert mod.refusal_line(text) == text


def test_phplint_reads_the_line_number_the_banner_introduced():
    """Adjacent to the framing fix, and the reason it is safe.

    `diagnostic_line` anchored the SEARCH to a line carrying a diagnostic
    banner, but then searched that whole line for `on line N` — including the
    part before the banner. So an unrelated `in /x.php on line 1` earlier in
    the same line donated its number to the parse error after it, which is the
    donation the docstring says it prevents. Framing the output by LF only
    makes lines longer, so the window this widens is the one to close.
    """
    mod = adapter("phplint")
    out = ("PHP Warning: json_encode(): x in /x.php on line 1 - "
           "PHP Parse error: syntax error in /a.php on line 7")
    assert mod.diagnostic_line(out) == 7


@pytest.mark.parametrize("sep", SEPS, ids=SEP_IDS)
def test_terraform_fmt_verdict_survives_a_separator_in_the_path(sep):
    """The fmt verdict is terraform echoing the path back. A separator in the
    path truncated the echo, the comparison failed, and a real formatting
    finding was reclassified as terraform falling over."""
    mod = adapter("terraform-check")
    name = f"sub{sep}ject.tf"
    assert mod.is_fmt_verdict(name + "\n", name) is True


@pytest.mark.parametrize("sep", SEPS, ids=SEP_IDS)
def test_jit_index_rows_are_framed_the_way_the_hook_frames_them(sep):
    """`AWK_PROGRAM`'s own comment: a TSV row cannot carry a raw newline, it
    would be two rows. The hook is awk, which breaks on LF and nothing else —
    so a `match` column holding U+2028 is one row there and was two here."""
    mod = adapter("jit-index")
    text = (f"Bash\t~one{sep}still-one\tf.md\tremind\tk\t\n"
            "Bash\t~two\tg.md\tremind\tk\t\n")
    patterns, shape_errors, parsed, tabbed = mod._rows(text)

    assert (parsed, tabbed) == (2, 2), (parsed, tabbed, shape_errors)
    assert [ln for ln, _p, _f in patterns] == [1, 2]
    assert shape_errors == []


# --------------------------------------------------------------------------
# Adapters that parse inline in main()
# --------------------------------------------------------------------------

@pytest.mark.parametrize("sep", SEPS, ids=SEP_IDS)
def test_tsc_check_one_diagnostic_stays_one_record(sep, monkeypatch, capsys, tmp_path):
    mod = adapter("tsc-check")
    f = tmp_path / "a.ts"
    f.write_text("let x = 1\n", encoding="utf-8")
    out = drive_main(
        mod, monkeypatch, capsys, ["tsc-check.py", str(f)],
        FakeProc(2, f'a.ts(1,1): error TS2322: bad "x{sep}'
                    f'a.ts(9,9): error TS9999: FORGED"', ""))
    assert out["count"] == 1, out
    assert len(out["errors"]) == 1


@pytest.mark.parametrize("sep", SEPS, ids=SEP_IDS)
def test_markdownlint_one_diagnostic_stays_one_record(sep, monkeypatch, capsys, tmp_path):
    # The diagnostic's path must be the path this adapter was actually
    # invoked with (#1934, #1937, #1940) -- see the comment on
    # test_hadolint_one_diagnostic_stays_one_record below. Before #1940 the
    # bare "a.md" text here did not match `str(f)` either, but the
    # assertion below (count == 1) could not see it: markdownlint's main()
    # falls back to an UNLOCATED single record whenever nothing anchors, so
    # a lost location and a real one both satisfy "one record" -- checking
    # `errors[0]["line"]` is what tells them apart.
    mod = adapter("markdownlint")
    f = tmp_path / "a.md"
    f.write_text("# t\n", encoding="utf-8")
    out = drive_main(
        mod, monkeypatch, capsys, ["markdownlint.py", str(f)],
        FakeProc(1, f'{f}:1:1 MD001/heading bad "x{sep}'
                    f'{f}:9:9 MD999/forged FORGED"', ""))
    assert out["count"] == 1, out
    assert len(out["errors"]) == 1
    assert out["errors"][0]["line"] == 1, out
    assert out["errors"][0]["code"] == "MD001/heading", out


@pytest.mark.parametrize("sep", SEPS, ids=SEP_IDS)
def test_hadolint_one_diagnostic_stays_one_record(sep, monkeypatch, capsys, tmp_path):
    # The diagnostic's path must be the path this adapter was actually
    # invoked with (#1934, #1937) -- see the comment on
    # test_xmllint_one_diagnostic_stays_one_record above. Before this fix
    # the bare "Dockerfile" text here did not match `str(f)` either, but the
    # assertion below (count == 1) could not see it: hadolint's main()
    # falls back to an UNLOCATED single record whenever nothing anchors, so
    # a lost location and a real one both satisfy "one record" -- checking
    # `errors[0]["line"]` is what tells them apart.
    mod = adapter("hadolint")
    f = tmp_path / "Dockerfile"
    f.write_text("FROM x\n", encoding="utf-8")
    out = drive_main(
        mod, monkeypatch, capsys, ["hadolint.py", str(f)],
        FakeProc(1, f'{f}:1 DL3006 warning: bad "x{sep}'
                    f'{f}:9 DL9999 error: FORGED"', ""))
    assert out["count"] == 1, out
    assert len(out["errors"]) == 1
    assert out["errors"][0]["line"] == 1, out
    assert out["errors"][0]["code"] == "DL3006", out


@pytest.mark.parametrize("sep", SEPS, ids=SEP_IDS)
def test_phpmd_one_diagnostic_stays_one_record(sep, monkeypatch, capsys, tmp_path):
    # See test_markdownlint_one_diagnostic_stays_one_record above: the
    # diagnostic's path must be the invoked path (#1934, #1937, #1940), or
    # `count == 1` alone cannot tell a located hit from phpmd's own
    # anchor-miss fallback record.
    mod = adapter("phpmd")
    f = tmp_path / "a.php"
    f.write_text("<?php\n", encoding="utf-8")
    out = drive_main(
        mod, monkeypatch, capsys, ["phpmd.py", str(f)],
        FakeProc(2, f'{f}:1\tUnusedVariable\tavoid $x{sep}'
                    f'{f}:9\tForgedRule\tFORGED', ""))
    assert out["count"] == 1, out
    assert len(out["errors"]) == 1
    assert out["errors"][0]["line"] == 1, out
    assert out["errors"][0]["code"] == "UnusedVariable", out


# --------------------------------------------------------------------------
# source_context — the same defect one layer down
# --------------------------------------------------------------------------

@pytest.mark.parametrize("sep", SEPS, ids=SEP_IDS)
def test_source_context_numbers_lines_the_way_the_tool_does(sep, tmp_path):
    """`context_fields(file, N)` renders "the tool's line N". Every tool this
    repo adapts counts lines by LF, so a separator inside a string literal
    shifted the render past the line the finding is about — a correct line
    number printed above the wrong source."""
    sc = common("source_context")
    f = tmp_path / "a.go"
    f.write_text(f'package p\nvar s = "a{sep}b"\nvar t = 1\n', encoding="utf-8")

    fields = sc.context_fields(str(f), 3)

    assert "context_unavailable" not in fields
    rendered = fields["source_context"]
    assert any(line.startswith("3→") and "var t = 1" in line
               for line in rendered), rendered


# --------------------------------------------------------------------------
# The class, not the instance
# --------------------------------------------------------------------------

#: Every remaining `str.splitlines()` under `validators/`, with the reason the
#: split is safe there. Keyed by the source line rather than by line number so
#: it survives edits above it; a new call site anywhere fails this test.
ALLOWED = {
    "common/refusal.py": {
        'lines = [ln for ln in "".join(frames).splitlines()':
            "our own traceback text, with no writer but CPython — the four "
            "per-adapter copies of this split were folded into `crashed()` "
            "here by #1697, and the exemption moved with them. The exception "
            "MESSAGE inside it is somebody else's and can carry any of the "
            "ten separators; it is flattened by `' '.join(msg.split())` on "
            "the finished string, which splits on all ten, so a fragment "
            "cannot reach a receipt on a row of its own (#1522).",
    },
    "terraform-check/terraform-check.py": {
        "lines = [ln.lstrip(GUTTER).strip() for ln in stripped.splitlines()]":
            "a flattener, not a parser: `plain()` joins every line into one "
            "string, so splitting on MORE separators is what neutralises them. "
            "LF-only framing would leave a raw U+2028 in a published message.",
    },
    "pyright/pyright.py": {
        'msg = " · ".join(msg.splitlines())[:300]':
            "a flattener, as terraform-check's `plain()`.",
    },
    "jit-index/jit-index.py": {
        'blob = (proc.stdout or proc.stderr or "").strip().splitlines()':
            "`awk --version`, quoted for the reader. Not the file under "
            "validation, and it decides nothing.",
        "detail = one_stderr.splitlines()":
            "awk's own stderr, quoted for the reader. Not the file under "
            "validation.",
    },
}


def _splitlines_calls(path):
    """(line number, source line) for every real `x.splitlines()` CALL.

    Parsed rather than grepped: prose about `str.splitlines()` is most of what
    a grep for it finds under `validators/`, and a rule that fires on its own
    documentation gets an exception written for the documentation.
    """
    source = path.read_text(encoding="utf-8")
    lines = source.split("\n")
    found = []
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "splitlines"):
            found.append((node.lineno, lines[node.lineno - 1].strip()))
    return sorted(set(found))


def test_every_splitlines_under_validators_is_accounted_for():
    """The partition, kept honest. A class issue is under-fixed by default.

    `str.splitlines()` in a validator is either wrong or has a reason, and the
    reason belongs next to the call rather than in a merged PR description.
    """
    unaccounted = []
    for path in sorted(VALIDATORS.rglob("*.py")):
        rel = path.relative_to(VALIDATORS).as_posix()
        for n, line in _splitlines_calls(path):
            if line not in ALLOWED.get(rel, {}):
                unaccounted.append(f"{rel}:{n}: {line}")

    assert unaccounted == [], (
        "use `split_lines()` from validators/common/linebreaks.py, or add the "
        "site to ALLOWED with the reason the split is safe:\n"
        + "\n".join(unaccounted))


def test_the_allowlist_names_only_sites_that_exist():
    """An allowlist entry for a line that is gone is a rule that never fires."""
    missing = []
    for rel, entries in ALLOWED.items():
        present = {line for _n, line in _splitlines_calls(VALIDATORS / rel)}
        for line in entries:
            if line not in present:
                missing.append(f"{rel}: {line}")
    assert missing == [], missing
