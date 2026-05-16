"""Integration test: real-world weirdest combo refactor in ONE vi call.

Combines all features (regex, :s, :%s, /PAT, ?^, k, :r FILE, O insert,
multi-line block, escape \\;) on a PHP class file. Locks in the script
that successfully runs end-to-end so we notice if any feature regresses.
"""
from __future__ import annotations

from pathlib import Path

import supertool


DEMO_BEFORE = """<?php

namespace App\\Demo;

class OldName
{
    public const VERSION = "v1";

    public function greet(string $who): string
    {
        echo $who; // debug print
        return "Hello, " . $who;
    }
}
"""

DEMO_AFTER = """<?php

namespace App\\Demo;

/**
 * @see \\Tests\\GreeterTest
 */
final class Greeter
{
    public const VERSION = "v2";

    public function greet(string $who): string
    {
        return "Howdy, " . $who;
    }

    public function shout(string $who): string
    {
        return strtoupper($this->greet($who));
    }
}
"""

METHOD_BODY = """
    public function shout(string $who): string
    {
        return strtoupper($this->greet($who));
    }
"""


NGINX_BEFORE = """# nginx site config

server {
    listen 80;
    server_name old.example.com;

    location /static/ {
        root /home/user/old-app/static;
        expires 1d;
    }

    location /media/ {
        root /home/user/old-app/media;
        expires 7d;
    }

    # deprecated: legacy upload endpoint
    location /upload-old/ {
        proxy_pass http://localhost:9999;
        deny all;
    }
}
"""

NGINX_AFTER = """# generated 2026-05-16 — DO NOT EDIT
# nginx site config

server {
    listen 80;
    server_name new.example.com;

    location /static/ {
        root /srv/new-app/static;
        expires 1d;
    }

    location /media/ {
        root /srv/new-app/media;
        expires 7d;
    }


    location /api/ {
        proxy_pass http://localhost:8080;
        proxy_set_header Host $host;
    }
}
"""

NGINX_EXTRA = """
    location /api/ {
        proxy_pass http://localhost:8080;
        proxy_set_header Host $host;
    }
"""


def test_vimgolf_strip_comments_swap_args(tmp_path: Path, monkeypatch) -> None:
    """VimGolf-style: strip Python inline comments + swap function args."""
    monkeypatch.setenv("SUPERTOOL_VIM_NO_PERSIST", "1")
    f = tmp_path / "x.py"
    f.write_text(
        "def foo(a, b, c):  # main function\n"
        "    x = a + b  # add values\n"
        "    return x\n"
    )
    supertool.op_vim(
        str(f),
        "%s/  +#.*//g␞%s/\\(a, b, c\\)/(c, b, a)/",
    )
    assert f.read_text() == (
        "def foo(c, b, a):\n"
        "    x = a + b\n"
        "    return x\n"
    )


def test_vimgolf_whitespace_to_csv(tmp_path: Path, monkeypatch) -> None:
    """VimGolf-style: collapse runs of spaces into single commas."""
    monkeypatch.setenv("SUPERTOOL_VIM_NO_PERSIST", "1")
    f = tmp_path / "x.csv"
    f.write_text(
        "name    age    city\n"
        "Alice   30     NYC\n"
        "Bob     25     LA\n"
    )
    supertool.op_vim(str(f), "%s/ +/,/g")
    assert f.read_text() == (
        "name,age,city\n"
        "Alice,30,NYC\n"
        "Bob,25,LA\n"
    )


def test_vimgolf_collapse_adjacent_duplicates(tmp_path: Path, monkeypatch) -> None:
    """VimGolf-style: regex backref to collapse adjacent duplicate chars."""
    monkeypatch.setenv("SUPERTOOL_VIM_NO_PERSIST", "1")
    f = tmp_path / "x.txt"
    f.write_text("aabbcc\nddeeff\ngghhii\n")
    supertool.op_vim(str(f), "%s/(.)\\1+/\\1/g")
    assert f.read_text() == "abc\ndef\nghi\n"


def test_strip_blank_lines(tmp_path: Path, monkeypatch) -> None:
    """Remove all blank lines from a file via regex."""
    monkeypatch.setenv("SUPERTOOL_VIM_NO_PERSIST", "1")
    f = tmp_path / "x.txt"
    f.write_text("line1\n\nline2\n\n\nline3\n")
    supertool.op_vim(str(f), "%s/^\\n//g")
    assert f.read_text() == "line1\nline2\nline3\n"


def test_markdown_deepen_headings(tmp_path: Path, monkeypatch) -> None:
    """Demote every `# heading` to `## heading` (add a leading `#`)."""
    monkeypatch.setenv("SUPERTOOL_VIM_NO_PERSIST", "1")
    f = tmp_path / "x.md"
    f.write_text("# Top\n\n## Sub\n\n# Another top\n")
    supertool.op_vim(str(f), "%s/^#/##/g")
    assert f.read_text() == "## Top\n\n### Sub\n\n## Another top\n"


def test_python_decorator_wrap_all_defs(tmp_path: Path, monkeypatch) -> None:
    """Prefix every top-level `def NAME(` with `@profile\\n` decorator line."""
    monkeypatch.setenv("SUPERTOOL_VIM_NO_PERSIST", "1")
    f = tmp_path / "x.py"
    f.write_text("def foo():\n    pass\n\ndef bar(x):\n    return x\n")
    supertool.op_vim(str(f), "%s/^def /@profile\\ndef /g")
    assert f.read_text() == (
        "@profile\ndef foo():\n    pass\n\n"
        "@profile\ndef bar(x):\n    return x\n"
    )


def test_json_config_bump_and_insert(tmp_path: Path, monkeypatch) -> None:
    """Bump version literal + insert a new field before the closing brace."""
    monkeypatch.setenv("SUPERTOOL_VIM_NO_PERSIST", "1")
    f = tmp_path / "config.json"
    f.write_text(
        "{\n"
        '    "version": "1.0.0",\n'
        '    "name": "demo"\n'
        "}\n"
    )
    supertool.op_vim(
        str(f),
        '%s/"1.0.0"/"2.0.0"/␞%s/"name": "demo"$/"name": "demo",/␞G␞?^}␞O    "debug": true',
    )
    assert f.read_text() == (
        "{\n"
        '    "version": "2.0.0",\n'
        '    "name": "demo",\n'
        '    "debug": true\n'
        "}\n"
    )


def test_online_delete_lines_matching_pattern(tmp_path: Path, monkeypatch) -> None:
    """Source: learnvim.irian.to/basics/the_global_command (`:g/console/d`).
    Adapted: remove all lines containing `console` using :%s with whole-line regex."""
    monkeypatch.setenv("SUPERTOOL_VIM_NO_PERSIST", "1")
    f = tmp_path / "x.js"
    f.write_text(
        "const one = 1;\n"
        'console.log("one: ", one);\n'
        "const two = 2;\n"
        'console.log("two: ", two);\n'
        "const three = 3;\n"
        'console.log("three: ", three);\n'
    )
    supertool.op_vim(str(f), "%s/^.*console.*\\n//g")
    assert f.read_text() == (
        "const one = 1;\n"
        "const two = 2;\n"
        "const three = 3;\n"
    )


def test_online_append_semicolon_non_empty_lines(tmp_path: Path, monkeypatch) -> None:
    """Source: learnvim.irian.to (`:g/./normal A;`). Adapted with :%s backref."""
    monkeypatch.setenv("SUPERTOOL_VIM_NO_PERSIST", "1")
    f = tmp_path / "x.js"
    f.write_text("const x = 5\nconst y = 10\nconst z = 15\n")
    supertool.op_vim(str(f), "%s/(.+)$/\\1;/g")
    assert f.read_text() == "const x = 5;\nconst y = 10;\nconst z = 15;\n"


def test_online_reorder_lastname_firstname(tmp_path: Path, monkeypatch) -> None:
    """Source: devhints.io/vim — `LastName, FirstName` → `FirstName LastName`."""
    monkeypatch.setenv("SUPERTOOL_VIM_NO_PERSIST", "1")
    f = tmp_path / "names.txt"
    f.write_text(
        "Smith, John\nJohnson, Sarah\nWilliams, Mike\nBrown, Emma\nDavis, Robert\n"
    )
    supertool.op_vim(str(f), "%s/(.*), (.*)/\\2 \\1/g")
    assert f.read_text() == (
        "John Smith\nSarah Johnson\nMike Williams\nEmma Brown\nRobert Davis\n"
    )


ZEN_BEFORE = """The Zen of Python, by Tim Peters

Beautiful is better than ugly.
Explicit is better than implicit.
Simple is better than complex.
Complex is better than complicated.
Flat is better than nested.
Sparse is better than dense.
Readability counts.
Special cases aren't special enough to break the rules.
Although practicality beats purity.
Errors should never pass silently.
Unless explicitly silenced.
In the face of ambiguity, refuse the temptation to guess.
There should be one-- and preferably only one --obvious way to do it.
Although that way may not be obvious at first unless you're Dutch.
Now is better than never.
Although never is often better than *right* now.
If the implementation is hard to explain, it's a bad idea.
If the implementation is easy to explain, it may be a good idea.
Namespaces are one honking great idea -- let's do more of those!
"""


def test_online_zen_of_python_refactor(tmp_path: Path, monkeypatch) -> None:
    """Source: raw.githubusercontent.com/python/cpython Lib/this.py.
    Real 21-line public text. 6 actions: global word swap, delete line by
    keyword, prepend MD heading, append signature."""
    monkeypatch.setenv("SUPERTOOL_VIM_NO_PERSIST", "1")
    f = tmp_path / "zen.md"
    f.write_text(ZEN_BEFORE)
    supertool.op_vim(
        str(f),
        "%s/better/preferable/g"
        "␞%s/^.*Dutch.*\\n//g"
        "␞gg␞O# Zen of Python\\n"
        "␞G␞oEOF — annotated by Max",
    )
    out = f.read_text()
    assert "# Zen of Python" in out.splitlines()[0]
    assert "Dutch" not in out
    assert out.count("preferable") == 8  # all 8 'better' → 'preferable'
    assert out.rstrip().endswith("EOF — annotated by Max")


LINUX_RST = """.. _codingstyle:

Linux kernel coding style
=========================

This is a short document describing the preferred coding style for the
linux kernel.  Coding style is very personal, and I won't **force** my
views on anybody, but this is what goes for anything that I have to be
able to maintain, and I'd prefer it for most other things too.  Please
at least consider the points made here.

First off, I'd suggest printing out a copy of the GNU coding standards,
and NOT read it.  Burn them, it's a great symbolic gesture.

Anyway, here goes:


1) Indentation
--------------

Tabs are 8 characters, and thus indentations are also 8 characters.
There are heretic movements that try to make indentations 4 (or even 2!)
characters deep, and that is akin to trying to define the value of PI to
be 3.

Rationale: The whole idea behind indentation is to clearly define where
a block of control starts and ends.  Especially when you've been looking
at your screen for 20 straight hours, you'll find it a lot easier to see
how the indentation works if you have large indentations.

The preferred way to ease multiple indentation levels in a switch statement is
to align the ``switch`` and its subordinate ``case`` labels in the same column
instead of ``double-indenting`` the ``case`` labels.  E.g.:
"""


def test_online_linux_kernel_rst_to_md(tmp_path: Path, monkeypatch) -> None:
    """Source: raw.githubusercontent.com/torvalds/linux Documentation/process/
    coding-style.rst — real 33-line public text. 13 actions to convert RST →
    Markdown: drop section underlines, drop directive, RST inline-code →
    MD inline-code, add MD heading markers, delete a paragraph, append footer."""
    monkeypatch.setenv("SUPERTOOL_VIM_NO_PERSIST", "1")
    f = tmp_path / "linux.md"
    f.write_text(LINUX_RST)
    supertool.op_vim(
        str(f),
        "gg␞dd␞dd"                                              # drop `.. _codingstyle:` + blank
        "␞%s/^=+$\\n//g"                                         # drop RST h1 underline
        "␞%s/^-+$\\n//g"                                         # drop RST h2 underline
        "␞%s/``([^`]+)``/`\\1`/g"                                # RST inline code → MD
        "␞%s/^Linux kernel coding style$/# Linux kernel coding style\\n/"  # h1
        "␞%s/^1\\) Indentation$/## 1) Indentation/"             # h2
        "␞gg␞/First off␞2dd"                                    # drop paragraph (First off + Burn them)
        "␞G␞oConverted from RST by vi",
    )
    out = f.read_text()
    assert out.startswith("# Linux kernel coding style")
    assert "## 1) Indentation" in out
    assert "Burn them" not in out
    assert "First off" not in out  # entire paragraph dropped
    assert "``switch``" not in out  # RST inline-code rewritten
    assert "`switch`" in out
    assert out.rstrip().endswith("Converted from RST by vi")


def test_online_prepend_https_to_bare_domains(tmp_path: Path, monkeypatch) -> None:
    """Source: devhints.io/vim — prepend https:// to domain-only lines, skip
    lines that already have http:// or https:// prefix."""
    monkeypatch.setenv("SUPERTOOL_VIM_NO_PERSIST", "1")
    f = tmp_path / "urls.txt"
    f.write_text(
        "example.com\n"
        "https://secure.example.com\n"
        "github.com\n"
        "http://legacy.example.com\n"
        "devhints.io\n"
    )
    supertool.op_vim(str(f), "%s/^([a-z]+\\.[a-z]+)$/https:\\/\\/\\1/g")
    assert f.read_text() == (
        "https://example.com\n"
        "https://secure.example.com\n"
        "https://github.com\n"
        "http://legacy.example.com\n"
        "https://devhints.io\n"
    )


def test_sql_migration_rename_table(tmp_path: Path, monkeypatch) -> None:
    """Rename a table everywhere it appears in a SQL migration script."""
    monkeypatch.setenv("SUPERTOOL_VIM_NO_PERSIST", "1")
    f = tmp_path / "migration.sql"
    f.write_text(
        "ALTER TABLE old_users ADD COLUMN email VARCHAR(255);\n"
        "CREATE INDEX idx_old_users_email ON old_users (email);\n"
        "DROP INDEX idx_old_users_legacy;\n"
    )
    supertool.op_vim(str(f), "%s/old_users/account/g")
    assert f.read_text() == (
        "ALTER TABLE account ADD COLUMN email VARCHAR(255);\n"
        "CREATE INDEX idx_account_email ON account (email);\n"
        "DROP INDEX idx_account_legacy;\n"
    )


def test_nginx_refactor_one_call(tmp_path: Path, monkeypatch) -> None:
    """Real-world nginx config refactor: rename host, migrate path, drop
    deprecated block, inject /api/ location, prepend generated header.
    11 actions, one vi call."""
    monkeypatch.setenv("SUPERTOOL_VIM_NO_PERSIST", "1")
    target = tmp_path / "site.conf"
    target.write_text(NGINX_BEFORE)
    extra = tmp_path / "extra.txt"
    extra.write_text(NGINX_EXTRA)

    script = (
        "%s/old.example.com/new.example.com/g"
        "␞%s/\\/home\\/user\\/old-app\\//\\/srv\\/new-app\\//g"
        "␞gg␞/# deprecated:␞5dd"
        "␞G␞?^}␞k"
        f"␞:r {extra}"
        "␞gg␞O# generated 2026-05-16 — DO NOT EDIT"
    )
    out = supertool.op_vim(str(target), script)
    assert "ERROR" not in out, f"unexpected error:\n{out}"
    assert target.read_text() == NGINX_AFTER, (
        f"refactor diverged\n--- got ---\n{target.read_text()}\n--- want ---\n{NGINX_AFTER}"
    )


def test_weirdest_combo_one_call(tmp_path: Path, monkeypatch) -> None:
    """12 actions in one vi call: rename, kill debug, bump version, add final,
    swap greeting, inject method inside class, prepend class docblock."""
    monkeypatch.setenv("SUPERTOOL_VIM_NO_PERSIST", "1")
    target = tmp_path / "demo.php"
    target.write_text(DEMO_BEFORE)
    body = tmp_path / "method-body.txt"
    body.write_text(METHOD_BODY)

    script = (
        "%s/OldName/Greeter/g"
        "␞%s/^        echo \\$who;.*\\n//"
        "␞%s/\"v1\"/\"v2\"/"
        "␞%s/^class Greeter/final class Greeter/"
        '␞%s/"Hello, "/"Howdy, "/'
        "␞G␞?^}␞k"
        f"␞:r {body}"
        "␞gg␞/class Greeter"
        "␞O/**\\n * @see \\\\Tests\\\\GreeterTest\\n */"
    )
    out = supertool.op_vim(str(target), script)
    assert "ERROR" not in out, f"unexpected error in script:\n{out}"
    assert target.read_text() == DEMO_AFTER, (
        f"refactor diverged from expected output\n"
        f"--- got ---\n{target.read_text()}\n"
        f"--- want ---\n{DEMO_AFTER}"
    )


def test_literal_semicolon_chained_in_real_script(tmp_path: Path, monkeypatch) -> None:
    """End-to-end PR #52 fix: `;` literal inside :s PAT/REPL within a chained
    multi-action script. Strips a PHP debug line whose pattern contains `;`,
    then appends a trailing marker via A — proves the chain still splits at
    the `;` AFTER the trailing `/` while keeping in-pattern `;` literal."""
    monkeypatch.setenv("SUPERTOOL_VIM_NO_PERSIST", "1")
    f = tmp_path / "x.php"
    f.write_text(
        "<?php\n"
        "function go(): void {\n"
        "    echo $debug; // remove me\n"
        "    return;\n"
        "}\n"
    )
    supertool.op_vim(
        str(f),
        ":s/^.*echo \\$debug; \\/\\/ remove me\\n//"
        "␞gg␞A // touched",
    )
    assert f.read_text() == (
        "<?php // touched\n"
        "function go(): void {\n"
        "    return;\n"
        "}\n"
    )
