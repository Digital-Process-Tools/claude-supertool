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
