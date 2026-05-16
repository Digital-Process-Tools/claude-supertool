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
    monkeypatch.setenv("SUPERTOOL_VI_NO_PERSIST", "1")
    f = tmp_path / "x.py"
    f.write_text(
        "def foo(a, b, c):  # main function\n"
        "    x = a + b  # add values\n"
        "    return x\n"
    )
    supertool.op_vi(
        str(f),
        "%s/  +#.*//g;%s/\\(a, b, c\\)/(c, b, a)/",
    )
    assert f.read_text() == (
        "def foo(c, b, a):\n"
        "    x = a + b\n"
        "    return x\n"
    )


def test_vimgolf_whitespace_to_csv(tmp_path: Path, monkeypatch) -> None:
    """VimGolf-style: collapse runs of spaces into single commas."""
    monkeypatch.setenv("SUPERTOOL_VI_NO_PERSIST", "1")
    f = tmp_path / "x.csv"
    f.write_text(
        "name    age    city\n"
        "Alice   30     NYC\n"
        "Bob     25     LA\n"
    )
    supertool.op_vi(str(f), "%s/ +/,/g")
    assert f.read_text() == (
        "name,age,city\n"
        "Alice,30,NYC\n"
        "Bob,25,LA\n"
    )


def test_vimgolf_collapse_adjacent_duplicates(tmp_path: Path, monkeypatch) -> None:
    """VimGolf-style: regex backref to collapse adjacent duplicate chars."""
    monkeypatch.setenv("SUPERTOOL_VI_NO_PERSIST", "1")
    f = tmp_path / "x.txt"
    f.write_text("aabbcc\nddeeff\ngghhii\n")
    supertool.op_vi(str(f), "%s/(.)\\1+/\\1/g")
    assert f.read_text() == "abc\ndef\nghi\n"


def test_nginx_refactor_one_call(tmp_path: Path, monkeypatch) -> None:
    """Real-world nginx config refactor: rename host, migrate path, drop
    deprecated block, inject /api/ location, prepend generated header.
    11 actions, one vi call."""
    monkeypatch.setenv("SUPERTOOL_VI_NO_PERSIST", "1")
    target = tmp_path / "site.conf"
    target.write_text(NGINX_BEFORE)
    extra = tmp_path / "extra.txt"
    extra.write_text(NGINX_EXTRA)

    script = (
        "%s/old.example.com/new.example.com/g"
        ";%s/\\/home\\/user\\/old-app\\//\\/srv\\/new-app\\//g"
        ";gg;/# deprecated:;5dd"
        ";G;?^};k"
        f";:r {extra}"
        ";gg;O# generated 2026-05-16 — DO NOT EDIT"
    )
    out = supertool.op_vi(str(target), script)
    assert "ERROR" not in out, f"unexpected error:\n{out}"
    assert target.read_text() == NGINX_AFTER, (
        f"refactor diverged\n--- got ---\n{target.read_text()}\n--- want ---\n{NGINX_AFTER}"
    )


def test_weirdest_combo_one_call(tmp_path: Path, monkeypatch) -> None:
    """12 actions in one vi call: rename, kill debug, bump version, add final,
    swap greeting, inject method inside class, prepend class docblock."""
    monkeypatch.setenv("SUPERTOOL_VI_NO_PERSIST", "1")
    target = tmp_path / "demo.php"
    target.write_text(DEMO_BEFORE)
    body = tmp_path / "method-body.txt"
    body.write_text(METHOD_BODY)

    script = (
        "%s/OldName/Greeter/g"
        ";%s/^        echo \\$who;.*\\n//"
        ";%s/\"v1\"/\"v2\"/"
        ";%s/^class Greeter/final class Greeter/"
        ';%s/"Hello, "/"Howdy, "/'
        ";G;?^};k"
        f";:r {body}"
        ";gg;/class Greeter"
        ";O/**\\n * @see \\\\Tests\\\\GreeterTest\\n */"
    )
    out = supertool.op_vi(str(target), script)
    assert "ERROR" not in out, f"unexpected error in script:\n{out}"
    assert target.read_text() == DEMO_AFTER, (
        f"refactor diverged from expected output\n"
        f"--- got ---\n{target.read_text()}\n"
        f"--- want ---\n{DEMO_AFTER}"
    )
