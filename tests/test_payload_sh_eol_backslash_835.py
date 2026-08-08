r"""#835 — the trailing `\\` guard on a shell payload blocks instead of informing.

`_sh_backslash_warning` (#380) reads the bytes about to land on a `.sh` file and
says so when a line ends with an *even* run of backslashes: bash consumes them
pairwise, so the line genuinely ends there and what looked like a continuation
is an escaped backslash followed by a new command. It parses, `bash -n` agrees,
`bash-check` agrees, and the program does something else. The warning printed —
and the write went through, which is what this issue is about.

The severity is decided by the rule #834 established in `docs/validators.md`,
"Declining instead of guessing": **refuse when every intent behind the pattern
has another spelling; warn when refusing would strand one.** Applied here it
splits the guard in two rather than promoting it wholesale, and the split is
what these tests pin:

* Content arriving from a `'''literal'''` payload block is **refused**. A
  literal block preserves backslashes verbatim, so `\\` there is ambiguous
  between "I want one, and TOML would have eaten it" (wrong — it would not) and
  "I want two". Both intents have another spelling: write one `\`, or move to a
  basic (triple-double-quote) block, where a wanted pair is spelled with four.
  Nothing becomes unwritable, so refusing costs nothing.

* Everything else stays a **warning**. At the write chokepoint there is no
  second spelling at all — a block there would make the byte pattern unwritable
  by any op, including an edit to an unrelated line of a file that already
  contains it. The reporter's proposed `allow_literal_backslash = true` is what
  that shape needs, and it is a new public field on the payload format bought to
  re-enable something the basic-block spelling already expresses.

* The backslash-then-whitespace half of the same guard stays a warning even from
  a literal block, because the reading "I meant an escaped space" has no other
  spelling — a basic block writes `\ ` as `\ ` too. Same guard, same payload
  route, different severity, decided only by whether an alternative exists.
"""
from pathlib import Path

import pytest

import supertool

BS = chr(92)
NL = chr(10)


# #1087 refuses a doubled backslash in `new` regardless of the target language,
# so the tests below that assert #835's SHELL scope have to say the pair is
# deliberate or they would be measuring the wrong guard. That is what the flag
# is for, and it is also the honest statement of the trade: a LaTeX line break
# and a Markdown hard break are correct content that now costs one key.
OPT_IN = "literal_backslashes = true" + NL


def _payload(tmp_path: Path, target: Path, new_field: str,
             opt_in: bool = False) -> str:
    body = (
        (OPT_IN if opt_in else "")
        + 'path = "' + str(target).replace(BS, BS * 2) + '"' + NL
        + 'old = "OLD"' + NL
        + new_field + NL
    )
    p = tmp_path / "p.toml"
    p.write_text(body, encoding="utf-8")
    return "@" + str(p)


# The reported shape: a literal block whose line ends with two backslashes.
LITERAL_DOUBLE = "new = '''echo one " + BS * 2 + NL + "echo two'''"
# The same two backslashes, said deliberately. A basic block escapes, so a
# wanted pair is spelled with four — the route the refusal has to name.
BASIC_DOUBLE = 'new = """echo one ' + BS * 4 + NL + 'echo two"""'
# One backslash: a real bash line continuation, and a literal block does not
# eat it. Must keep writing.
LITERAL_SINGLE = "new = '''echo one " + BS + NL + "echo two'''"
# Backslash then a space. Also broken bash, also caught by #380 — and it stays
# a warning, because "I meant an escaped space" has no second spelling.
LITERAL_SPACE = "new = '''echo one " + BS + " " + NL + "echo two'''"


def _target(tmp_path: Path, name: str = "s.sh") -> Path:
    t = tmp_path / name
    t.write_text("OLD" + NL, encoding="utf-8")
    return t


def test_the_literal_block_double_backslash_is_refused_and_nothing_is_written(
    tmp_path: Path,
) -> None:
    """The whole issue in one assertion: the file must be untouched.

    Asserting only that a message was printed is the proxy that lets this bug
    through — today's guard prints *and* writes. `bash-check` has no objection
    to the content (that is #380's premise), so nothing but this guard can stop
    the write.
    """
    target = _target(tmp_path)
    out = supertool.dispatch("edit:" + _payload(tmp_path, target, LITERAL_DOUBLE))
    assert target.read_text(encoding="utf-8") == "OLD" + NL, "the file was written"
    assert "ERROR" in out, out
    assert "backslash" in out.lower()


def test_the_refusal_names_both_spellings(tmp_path: Path) -> None:
    """A refusal is only legitimate because both intents have another spelling.
    If the message does not carry them, the caller is holding a rejection and no
    way forward — and the honest severity would have been a warning."""
    target = _target(tmp_path)
    out = supertool.dispatch("edit:" + _payload(tmp_path, target, LITERAL_DOUBLE))
    assert '"""' in out, "the basic-block route"
    assert BS * 4 in out, "spelled with the doubling that route implies"
    assert "echo one " + BS + NL in out, "the caller's own line, continued"


def test_the_basic_block_spelling_writes_the_pair(tmp_path: Path) -> None:
    """The escape hatch the refusal names has to work, or the advice is a wall.

    This is also why no `allow_literal_backslash` field is needed: the opt-out
    already exists in the payload format.
    """
    target = _target(tmp_path)
    out = supertool.dispatch("edit:" + _payload(tmp_path, target, BASIC_DOUBLE))
    assert "ERROR" not in out, out
    assert target.read_text(encoding="utf-8") == (
        "echo one " + BS * 2 + NL + "echo two" + NL
    )


def test_a_single_backslash_continuation_still_writes(tmp_path: Path) -> None:
    """The other half of the trade — the guard must not cost the legal payload."""
    target = _target(tmp_path)
    out = supertool.dispatch("edit:" + _payload(tmp_path, target, LITERAL_SINGLE))
    assert "ERROR" not in out, out
    assert target.read_text(encoding="utf-8") == (
        "echo one " + BS + NL + "echo two" + NL
    )


def test_the_trailing_whitespace_half_stays_a_warning(tmp_path: Path) -> None:
    """Same guard, same literal block, and it still writes.

    Not an oversight: a basic block spells `\\ ` as `\\ ` too, so there is no
    second spelling to send anyone to, and refusing would strand the intent.
    """
    target = _target(tmp_path)
    out = supertool.dispatch("edit:" + _payload(tmp_path, target, LITERAL_SPACE))
    assert "ERROR" not in out, out
    assert target.read_text(encoding="utf-8") == (
        "echo one " + BS + " " + NL + "echo two" + NL
    )
    assert "whitespace" in out.lower(), "the #380 warning still has to be said"


def test_a_non_shell_target_is_not_refused(tmp_path: Path) -> None:
    """`\\\\` at end of line is a line break in LaTeX and content in Markdown.

    The guard is scoped to the files whose language makes the pattern a bug.

    The payload declares the pair deliberate because #1087's refusal is NOT
    language-scoped -- it reads the payload's escape reflex, not the target's
    grammar. Without the flag this would be measuring that guard instead of
    this one.
    """
    target = _target(tmp_path, "s.txt")
    out = supertool.dispatch(
        "edit:" + _payload(tmp_path, target, LITERAL_DOUBLE, opt_in=True))
    assert "ERROR" not in out, out
    assert target.read_text(encoding="utf-8") == (
        "echo one " + BS * 2 + NL + "echo two" + NL
    )


def test_a_preexisting_line_does_not_block_an_unrelated_edit(tmp_path: Path) -> None:
    """Why the refusal reads the payload and not the resulting file.

    A block at the write chokepoint sees whole-file content, so one legitimate
    `echo \\\\` on line 400 would make every later edit to that script
    impossible — with no spelling that gets round it. That is the intent-
    stranding the rule forbids, and it is the reason the severity split falls
    where it does.
    """
    target = tmp_path / "s.sh"
    target.write_text("echo one " + BS * 2 + NL + "OLD" + NL, encoding="utf-8")
    out = supertool.dispatch(
        "edit:" + _payload(tmp_path, target, "new = '''echo two'''")
    )
    assert "ERROR" not in out, out
    assert target.read_text(encoding="utf-8") == (
        "echo one " + BS * 2 + NL + "echo two" + NL
    )


def test_a_batch_refuses_only_for_the_shell_op(tmp_path: Path) -> None:
    """The pattern belongs to whichever op carries it, not to the payload."""
    sh = _target(tmp_path, "a.sh")
    txt = _target(tmp_path, "b.txt")
    body = (
        OPT_IN
        + "[[ops]]" + NL
        + 'op = "edit"' + NL
        + 'path = "' + str(sh).replace(BS, BS * 2) + '"' + NL
        + 'old = "OLD"' + NL
        + "new = '''echo clean'''" + NL
        + "[[ops]]" + NL
        + 'op = "edit"' + NL
        + 'path = "' + str(txt).replace(BS, BS * 2) + '"' + NL
        + 'old = "OLD"' + NL
        + LITERAL_DOUBLE + NL
    )
    p = tmp_path / "b.toml"
    p.write_text(body, encoding="utf-8")
    out = supertool.dispatch("batch:@" + str(p))
    assert "ERROR" not in out, out
    assert sh.read_text(encoding="utf-8") == "echo clean" + NL
    assert txt.read_text(encoding="utf-8") == (
        "echo one " + BS * 2 + NL + "echo two" + NL
    )


def test_a_batch_refuses_before_any_op_runs(tmp_path: Path) -> None:
    """A payload-level refusal is not a per-op failure — it happens at parse.

    `batch:` applies partially by design, so a guard that fired mid-run would
    leave the earlier ops on disk. This one has to leave the whole batch alone.
    """
    first = _target(tmp_path, "a.sh")
    second = _target(tmp_path, "b.sh")
    body = (
        "[[ops]]" + NL
        + 'op = "edit"' + NL
        + 'path = "' + str(first).replace(BS, BS * 2) + '"' + NL
        + 'old = "OLD"' + NL
        + "new = '''echo clean'''" + NL
        + "[[ops]]" + NL
        + 'op = "edit"' + NL
        + 'path = "' + str(second).replace(BS, BS * 2) + '"' + NL
        + 'old = "OLD"' + NL
        + LITERAL_DOUBLE + NL
    )
    p = tmp_path / "b.toml"
    p.write_text(body, encoding="utf-8")
    out = supertool.dispatch("batch:@" + str(p))
    assert "ERROR" in out, out
    assert first.read_text(encoding="utf-8") == "OLD" + NL, "an earlier op ran"
    assert second.read_text(encoding="utf-8") == "OLD" + NL


def test_the_refusal_is_raised_by_the_loader(tmp_path: Path) -> None:
    """Unit-level: every route into a payload goes through `_load_at_file`."""
    target = _target(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        supertool._load_at_file(_payload(tmp_path, target, LITERAL_DOUBLE))
    assert "backslash" in str(excinfo.value).lower()


def test_the_loader_leaves_a_json_payload_alone(tmp_path: Path) -> None:
    """JSON has no literal block, so the ambiguity this refuses does not exist
    there: `"\\\\\\\\"` is already the explicit spelling of a pair. The #380
    warning still covers it at the chokepoint."""
    target = _target(tmp_path)
    p = tmp_path / "p.json"
    p.write_text(
        '{"path": ' + repr(str(target)).replace("'", '"').replace(BS, BS * 2)
        + ', "old": "OLD", "new": "echo one ' + BS * 4 + BS + 'necho two"}',
        encoding="utf-8",
    )
    out = supertool.dispatch("edit:@" + str(p))
    assert "ERROR" not in out, out
    assert target.read_text(encoding="utf-8") == (
        "echo one " + BS * 2 + NL + "echo two" + NL
    )
