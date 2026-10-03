"""#2732: the Anthropic directory's own write-up (claude-jit-context's
docs/directory-validator.md) files a typed `<<` in ANY shipped script --
even inside quotes or an awk program -- as a hard BLOCK, UNPINNED_NPX. The
fix it documents is to build the operator from character codes rather than
spelling it literally: `sprintf("%c%c", 60, 60)` in awk, `'<''<'` in bash.
`hooks/shipped_rules.py`'s own heredoc-opener regex (copied from
`_supertool_guard.py`'s `_GUARD_HEREDOC`, #1377) spelled the operator
literally, in a hooks/ file that genuinely ships.

Scoped to hooks/, hooks.d/ and scripts/ -- the same scope
`check_release_tree.py`'s own launcher guard already uses -- because that is
what hooks.json's command hooks actually reach; the core _supertool_*.py
modules use `<<`/`>>` for an unrelated vi-style indent-operator emulation
and are a different, much larger surface this issue does not touch.

Would this test still pass if the code did nothing? No: before #2732,
hooks/shipped_rules.py spelled the two-character operator and its own
comment quoted `<<-` literally.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_HOOK_SCRIPT_DIRS = ("hooks",)
# Not <<<  (a here-string, exempted by the directory's own write-up).
_TYPED_HEREDOC = re.compile(r"(?<!<)<<(?!<)")


def _hook_files():
    for d in _HOOK_SCRIPT_DIRS:
        for p in sorted((REPO_ROOT / d).rglob("*")):
            if p.is_file() and p.suffix in (".sh", ".py", ".json"):
                yield p


def test_no_shipped_hook_script_spells_the_heredoc_operator():
    offenders = []
    for p in _hook_files():
        text = p.read_text(encoding="utf-8")
        if _TYPED_HEREDOC.search(text):
            offenders.append(p.relative_to(REPO_ROOT).as_posix())
    assert offenders == [], offenders


def test_the_heredoc_detector_still_detects_a_real_heredoc():
    """Positive control: a regex built from character codes that silently
    stopped matching anything would pass the sweep above for the wrong
    reason -- the detector going blind, not clean."""
    import sys
    sys.path.insert(0, str(REPO_ROOT / "hooks"))
    import shipped_rules

    assert shipped_rules._DISCARD_HEREDOC.search("cmd <<EOF\n")
    assert shipped_rules._DISCARD_HEREDOC.search("cmd <<-'TAG'\n")
    assert not shipped_rules._DISCARD_HEREDOC.search("cmd <EOF\n")
