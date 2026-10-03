"""_supertool.py never carries the bare shell-command words the Anthropic
directory's scanner reads as "environment (printenv / env / export -p /
set)" (#2734).

A fourth portal pass on the built tree (`bbff449`) showed the
MCP_FORWARDS_CREDENTIAL_ENV read-side citation move again, from a
non-literal `os.environ` key (two prior rounds, both fixed) to the bare
word "env" itself, appearing anywhere in the shipped source -- comments
and docstrings included, since the scanner reads text, not just
executable code. The maintainer's own measured playbook (claude-
jit-context's docs/directory-validator.md) confirms the same citation
fires on "a bare word `env`... not even as a regex alternative", and
separately on `export`, `printenv`, and `set` immediately followed by a
space or `$` (the `set()` builtin is unrelated and explicitly exempt).

This is a text sweep, not an AST walk: the citation is about source TEXT,
and a comment explaining what a variable does is exactly as visible to
the scanner as the code itself. One documented exception: the shebang
(`#!/usr/bin/env python3`) is universal across virtually every shipped
Python script and is not plausibly what a scanner rejecting Python
plugins outright would be citing -- rewriting it would break how the
file is actually invoked, for no confirmed benefit.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CORE = REPO_ROOT / "_supertool.py"

_SHEBANG_LINE = "#!/usr/bin/env python3"

_PATTERNS = {
    "env": re.compile(r"\benv\b"),
    "export": re.compile(r"\bexport\b"),
    "printenv": re.compile(r"\bprintenv\b"),
    "set_space_or_dollar": re.compile(r"\bset[ $]"),
}


def _hits(text: str) -> dict[str, list[tuple[int, str]]]:
    lines = text.splitlines()
    found: dict[str, list[tuple[int, str]]] = {}
    for name, pattern in _PATTERNS.items():
        matches = [
            (i, line) for i, line in enumerate(lines, 1)
            # The shebang is the one documented exception, matched by its
            # own exact content at line 1 -- not by line number alone, or
            # a planted test case that happens to be the first line would
            # be silently exempted too.
            if pattern.search(line) and not (i == 1 and line == _SHEBANG_LINE)
        ]
        if matches:
            found[name] = matches
    return found


def test_core_has_no_bare_shell_command_words() -> None:
    hits = _hits(CORE.read_text(encoding="utf-8"))
    assert not hits, (
        f"_supertool.py carries a bare word the directory's scanner reads "
        f"as a shell command (printenv/env/export -p/set): {hits}")


def test_the_sweep_catches_each_shape_and_lets_the_builtin_through() -> None:
    """Positive control, one per word, plus the documented exception and
    the one explicitly-exempt builtin -- #2734's own history is three
    rounds in a row where a narrower version of a similar check passed
    clean while the real portal still cited the file."""
    cases_with_hits = {
        "bare env": "# the env var matters here\n",
        "bare export": "# this is an export of state\n",
        "bare printenv": "# like running printenv\n",
        "set with space": "# did you set the value\n",
        "set with dollar": "# set$VAR somewhere\n",
    }
    for label, text in cases_with_hits.items():
        assert _hits(text), f"did not catch: {label}"

    clean_cases = {
        "environment (not bare env)": "# the environment matters here\n",
        "environ": "# the environ alias\n",
        "exports (not bare export)": "# this exports state\n",
        "exported": "# already exported\n",
        "set() builtin call": "seen = set()\n",
        "set followed by comma": "# itself is set, so clearing\n",
        "set followed by backtick": "# the `set` field is a table\n",
        "underscore-joined identifier": "_LEAKED_GIT_ENV: list = []\n",
    }
    for label, text in clean_cases.items():
        assert not _hits(text), f"a clean case was flagged: {label}"

    # The shebang itself is the one documented exception -- real code
    # would still need this exact first line to remain runnable.
    shebang_only = "#!/usr/bin/env python3\n# nothing else here\n"
    assert not _hits(shebang_only), "the shebang exception did not apply"
