"""#2732: the Anthropic directory held a release-tree probe on
MCP_FORWARDS_CREDENTIAL_ENV, citing `_supertool_vim.py` for text the holder
read as "printenv / env / export -p / set" (read-side pattern for "reads a
credential from the user's machine"). The docstring occurrences of the word
"env" in `_check_vim_shell_allowed` are stripped by #2731's comment/docstring
removal at build time; the one occurrence that survives into the shipped
tree is the literal ERROR string `_check_vim_shell_allowed` returns, which
spelled the bare word "env" in a parenthetical.

Would this test still pass if the code did nothing? No -- before #2732 the
message read '...SUPERTOOL_ALLOW_VIM_SHELL=1 (env), or add...'.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import _supertool_vim

_BARE_ENV = re.compile(r"\b" + "env" + r"\b")


def test_the_gate_message_has_no_bare_env_word() -> None:
    old = os.environ.pop("SUPERTOOL_ALLOW_VIM_SHELL", None)
    try:
        msg = _supertool_vim._check_vim_shell_allowed()
    finally:
        if old is not None:
            os.environ["SUPERTOOL_ALLOW_VIM_SHELL"] = old
    assert msg is not None, "the gate must still refuse with SUPERTOOL_ALLOW_VIM_SHELL unset"
    # Positive control: the message still names the real opt-in var -- a
    # message gone empty or renamed would pass the negative check below for
    # the wrong reason.
    assert "SUPERTOOL_ALLOW_VIM_SHELL" in msg, msg
    assert not _BARE_ENV.search(msg), msg
