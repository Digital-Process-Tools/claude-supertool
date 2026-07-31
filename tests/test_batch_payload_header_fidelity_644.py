"""#644 — a batch sub-op header must never be a runnable string that runs
something other than what ran.

A batch sub-op supplied via `@payload` was echoed back as a flattened
single-colon string:

    --- replace:time: 10:30:time: 11:45:/tmp/h.txt ---

The payload route exists *because* the content contains `:`. Flattening it back
produces a string that does not fail to parse — it parses as a **different op**.
Pasting the header above sends the dispatcher looking for a file named `30`.
On a `replace`/`edit` that is a header inviting the reader to touch a path
nobody intended.

Two rendering paths existed, and they disagreed:

  * `len(arg) <= _HEADER_ARG_MAX` — verbatim flatten (the lie above);
  * `len(arg) >  _HEADER_ARG_MAX` — a quoted, elided summary, and only when the
    op *succeeded*, so a failing long payload op fell back to the lie.

Same family as #621: output that presents itself as a faithful account of an
operation and is not. There the loss was visibility; here it is fidelity.

Alongside it, on the same lines, a fatal one: the batch loop built read-only
sub-op args by joining the payload's fields in **alphabetical key order**
(`sorted(_item)`), not the op's positional order. That is not a header defect —
it is the arg that actually gets dispatched. `between` with `symbol` + `path`
ran as `between:<path>:<symbol>`, searching for the file inside the symbol.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List, Tuple

import pytest

import supertool


def _run_batch(tmp_path: Path, ops_toml: str) -> str:
    spec = tmp_path / "ops.toml"
    spec.write_text(ops_toml, encoding="utf-8")
    return supertool.dispatch(f"batch:@{spec}")


def _sub_headers(out: str) -> List[str]:
    """Every `--- ... ---` header below the outer `batch:@...` one."""
    return [
        line.strip()
        for line in out.splitlines()[1:]
        if line.startswith("--- ") and line.rstrip().endswith(" ---")
    ]


def _rerun(header: str) -> Tuple[str, str]:
    """Feed a header back to the dispatcher. Returns (whole output, body)."""
    inner = header.strip()
    assert inner.startswith("--- ") and inner.endswith(" ---"), inner
    out = supertool.dispatch(inner[4:-4])
    body = out.split("\n", 1)[1] if "\n" in out else ""
    return out, body


_NOT_FOUND = re.compile(r"(?:path|file) not found: (\S+)")


def _paths_the_rerun_went_looking_for(body: str) -> List[str]:
    return _NOT_FOUND.findall(body)


def _assert_round_trip_honest(header: str, real_path: str, real_op: str) -> None:
    """The #644 invariant, as a round trip rather than a string shape.

    A header is honest when re-dispatching it either runs the same op on the
    same target, or is not a runnable op at all. It is dishonest when it parses
    into a *different* op — which is what naming a path the caller never wrote
    proves.
    """
    out, body = _rerun(header)
    for named in _paths_the_rerun_went_looking_for(body):
        # A payload reference that no longer resolves is the header declining,
        # not the header lying — it names the route, not a fabricated target.
        if named.startswith("@"):
            continue
        assert named == real_path, (
            f"header {header!r} re-dispatched into a lookup of {named!r}, "
            f"which is not the op's target {real_path!r}"
        )
    if "ERROR" not in body:
        # It ran. Then it must have run the same op.
        assert out.startswith(f"--- {real_op}"), (
            f"header {header!r} re-ran as something other than {real_op!r}: {out!r}"
        )


class TestPayloadHeaderRoundTrip:
    """The header, fed back to the dispatcher, must not become another op."""

    def test_short_payload_replace_header_does_not_run_a_different_op(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "h.txt"
        target.write_text("time: 10:30\nend\n", encoding="utf-8")
        out = _run_batch(
            tmp_path,
            "[[ops]]\n"
            'op = "replace"\n'
            f'path = "{target}"\n'
            'old = "time: 10:30"\n'
            'new = "time: 11:45"\n',
        )
        assert target.read_text(encoding="utf-8") == "time: 11:45\nend\n", out
        headers = _sub_headers(out)
        assert len(headers) == 1, out
        _assert_round_trip_honest(headers[0], str(target), "replace")

    def test_short_payload_edit_header_does_not_run_a_different_op(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "c.yml"
        target.write_text("url: http://a/b\n", encoding="utf-8")
        out = _run_batch(
            tmp_path,
            "[[ops]]\n"
            'op = "edit"\n'
            f'path = "{target}"\n'
            'old = "url: http://a/b"\n'
            'new = "url: https://a/b"\n',
        )
        headers = _sub_headers(out)
        assert len(headers) == 1, out
        _assert_round_trip_honest(headers[0], str(target), "edit")

    def test_long_payload_op_header_is_also_honest(self, tmp_path: Path) -> None:
        """The other rendering path. Long args took the elided branch — and only
        on success — so this must hold on the same terms."""
        old = "alpha: 1 " + ("filler:token " * 30)
        new = "alpha: 2 " + ("filler:token " * 30)
        target = tmp_path / "long.txt"
        target.write_text(old + "\n", encoding="utf-8")
        out = _run_batch(
            tmp_path,
            "[[ops]]\n"
            'op = "replace"\n'
            f'path = "{target}"\n'
            f'old = "{old}"\n'
            f'new = "{new}"\n',
        )
        headers = _sub_headers(out)
        assert len(headers) == 1, out
        _assert_round_trip_honest(headers[0], str(target), "replace")

    def test_failing_payload_op_header_is_honest_too(self, tmp_path: Path) -> None:
        """The elided header was swapped in only when the op WROTE. A payload op
        that matches nothing therefore fell back to the flattened lie — at the
        exact moment a reader is trying to reconstruct what happened."""
        target = tmp_path / "miss.txt"
        target.write_text("untouched\n", encoding="utf-8")
        out = _run_batch(
            tmp_path,
            "[[ops]]\n"
            'op = "replace"\n'
            f'path = "{target}"\n'
            'old = "time: 10:30"\n'
            'new = "time: 11:45"\n',
        )
        assert target.read_text(encoding="utf-8") == "untouched\n", out
        headers = _sub_headers(out)
        assert len(headers) == 1, out
        _assert_round_trip_honest(headers[0], str(target), "replace")


class TestPayloadHeaderShape:
    """Round-tripping proves the header is not a *different* op. These pin what
    it says instead — a header that declined must say it declined."""

    def test_header_names_the_payload_route(self, tmp_path: Path) -> None:
        target = tmp_path / "h.txt"
        target.write_text("time: 10:30\n", encoding="utf-8")
        out = _run_batch(
            tmp_path,
            "[[ops]]\n"
            'op = "replace"\n'
            f'path = "{target}"\n'
            'old = "time: 10:30"\n'
            'new = "time: 11:45"\n',
        )
        header = _sub_headers(out)[0]
        assert "@payload" in header, header
        assert str(target) in header, header

    def test_rendering_does_not_depend_on_arg_length(self, tmp_path: Path) -> None:
        """Two rendering paths, one of which lied, chosen by `len(arg)`. The
        payload route must render one way regardless."""
        short_t = tmp_path / "s.txt"
        short_t.write_text("a:1\n", encoding="utf-8")
        long_old = "b:1 " + ("pad:x " * 40)
        long_t = tmp_path / "l.txt"
        long_t.write_text(long_old + "\n", encoding="utf-8")
        short_out = _run_batch(
            tmp_path,
            "[[ops]]\nop = \"replace\"\n"
            f'path = "{short_t}"\nold = "a:1"\nnew = "a:2"\n',
        )
        long_out = _run_batch(
            tmp_path,
            "[[ops]]\nop = \"replace\"\n"
            f'path = "{long_t}"\nold = "{long_old}"\nnew = "b:2"\n',
        )
        short_h = _sub_headers(short_out)[0]
        long_h = _sub_headers(long_out)[0]
        assert "@payload" in short_h, short_h
        assert "@payload" in long_h, long_h

    def test_colon_cli_op_keeps_its_verbatim_runnable_header(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard on the fix. A colon-CLI op's header IS re-runnable
        and must stay verbatim — this issue is about the payload route only.

        chdir + a relative path deliberately: the verbatim header survives only
        while the whole arg is under _HEADER_ARG_MAX, and an absolute tmp_path
        under xdist (`popen-gwN/...`) crosses 160 chars on its own and takes the
        elided branch. That is #384 behaving as designed, not this fix — but it
        makes an absolute-path version of this assertion pass or fail on where
        pytest happened to put the file."""
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "plain.txt"
        target.write_text("alpha\n", encoding="utf-8")
        out = supertool.dispatch("replace:alpha:beta:plain.txt")
        assert out.startswith("--- replace:alpha:beta:plain.txt ---"), out
        assert "@payload" not in out
        assert target.read_text(encoding="utf-8") == "beta\n"


class TestBatchReadOpFieldOrder:
    """Found while reading the same lines: batch built read-only sub-op args by
    joining payload fields in ALPHABETICAL key order. That is the dispatched
    arg, not just the header."""

    def test_between_uses_positional_fields_not_sorted_keys(
        self, tmp_path: Path
    ) -> None:
        """Pattern mode rather than symbol mode — symbol mode needs tree-sitter,
        and this is testing field placement, not the symbol backend."""
        src = tmp_path / "m.py"
        src.write_text(
            "head\nSTART: here\n    return 42\nEND: there\ntail\n",
            encoding="utf-8",
        )
        out = _run_batch(
            tmp_path,
            "[[ops]]\n"
            'op = "between"\n'
            'start = "START:"\n'
            'end = "END:"\n'
            f'path = "{src}"\n',
        )
        # sorted({end, path, start}) => end, path, start, i.e.
        # `between:END::<path>:START:` — not any argument order between takes.
        assert "ERROR" not in out, out
        assert "return 42" in out, out
        assert "tail" not in out, out

    def test_read_uses_positional_fields_not_sorted_keys(
        self, tmp_path: Path
    ) -> None:
        src = tmp_path / "lines.txt"
        src.write_text("".join(f"line{i}\n" for i in range(1, 21)), encoding="utf-8")
        out = _run_batch(
            tmp_path,
            "[[ops]]\n" 'op = "read"\n' f'path = "{src}"\n' "offset = 5\n" "limit = 2\n",
        )
        # sorted({limit, offset, path}) => limit, offset, path
        # => `read:2:5:<path>` => "invalid literal for int()".
        assert "ERROR" not in out, out
        assert "line6" in out, out
        assert "line9" not in out, out

    def test_grep_pattern_with_colon_survives_batch(self, tmp_path: Path) -> None:
        """The whole point of the read-op payload route (#625): a pattern that
        contains ':' must not be re-tokenized on its way through batch."""
        src = tmp_path / "code.php"
        src.write_text("$a = Foo::BAR;\n$b = 1;\n", encoding="utf-8")
        out = _run_batch(
            tmp_path,
            "[[ops]]\n"
            'op = "grep"\n'
            'pattern = "Foo::BAR"\n'
            f'path = "{src}"\n'
            "limit = 5\n",
        )
        assert "ERROR" not in out, out
        assert "Foo::BAR" in out, out

    def test_around_pattern_with_colon_survives_batch(self, tmp_path: Path) -> None:
        src = tmp_path / "log.txt"
        src.write_text("noise\nERROR: boom\ntail\n", encoding="utf-8")
        out = _run_batch(
            tmp_path,
            "[[ops]]\n"
            'op = "around"\n'
            'pattern = "ERROR: boom"\n'
            f'path = "{src}"\n'
            "n = 1\n",
        )
        assert "ERROR: file not found" not in out, out
        assert "ERROR: boom" in out, out

class TestDeclineRatherThanGuess:
    """`docs/validators.md` §"Declining instead of guessing" — three states, not
    two. A surface that cannot answer must say so rather than answer wrongly."""

    def test_payload_placeholder_declines_with_a_reason(self) -> None:
        """Re-running the header must not read as a missing file. It is not a
        file — it is a placeholder standing in for fields no colon CLI holds."""
        out = supertool.dispatch("replace:@payload → /tmp/whatever.txt")
        assert "placeholder" in out, out
        assert "#644" in out, out
        assert "not found" not in out, out

    def test_unorderable_batch_fields_decline_instead_of_dispatching(
        self, tmp_path: Path
    ) -> None:
        """`sorted(item)` used to place these alphabetically and dispatch the
        result. An op with no declared field order now refuses."""
        out = _run_batch(
            tmp_path,
            "[[ops]]\n" 'op = "glob"\n' 'pattern = "*.py"\n' 'limit = "3"\n',
        )
        assert "ERROR" in out, out
        assert "declared payload field order" in out, out

    def test_declared_order_is_used_for_a_multi_field_colon_op(
        self, tmp_path: Path
    ) -> None:
        src = tmp_path / "many.txt"
        src.write_text("".join(f"row{i}\n" for i in range(1, 11)), encoding="utf-8")
        out = _run_batch(
            tmp_path,
            "[[ops]]\n" 'op = "head"\n' f'path = "{src}"\n' "n = 2\n",
        )
        # sorted({n, path}) => n, path => `head:2:<path>` — path "2".
        assert "ERROR" not in out, out
        assert "row1" in out, out
        assert "row9" not in out, out

    def test_sparse_positional_fields_decline(self, tmp_path: Path) -> None:
        """`around_line` is path:line:n. A payload giving path and n but not
        line cannot be flattened — the colon form has no way to skip a slot."""
        src = tmp_path / "s.txt"
        src.write_text("a\nb\nc\n", encoding="utf-8")
        out = _run_batch(
            tmp_path,
            "[[ops]]\n" 'op = "around_line"\n' f'path = "{src}"\n' "n = 1\n",
        )
        assert "ERROR" in out, out
        assert "sparse" in out, out
