"""The disclosure has to reach the console it is written for (#863).

#851 gave `presets/_untrusted.py` a security property that is entirely about
what a *reader* sees: a control character in tracker text is replaced by the
Control Pictures glyph that names it, so `real line\\rFORGED` cannot quietly
overwrite a line the tool wrote. #854 narrowed which characters that fires on.
Neither asked whether the glyph survives the terminal it is printed to.

It does not, on the platform this repo has taken ten issues from:

    >>> "\\u241b".encode("cp1252")   # UnicodeEncodeError
    >>> "\\u241b".encode("cp850")    # UnicodeEncodeError
    >>> "\\u241b".encode("cp437")    # UnicodeEncodeError

And it is not only the glyphs. Every character the disclosure layer emits of
its own accord is outside ASCII: the fence markers `\\u27e8` / `\\u27e9`, the
`\\u2026` and the em dash in `banner()` and `flat_note()`, the em dash in the
`NEUTRALISED` replacement. On a cp437 console the *marker* fails to encode
before any glyph does — which is the worse half, because a fence whose edges
cannot be printed is not a fence.

Under `errors="strict"` (the default everywhere in this repo) that is a
`UnicodeEncodeError` and the op dies mid-render. Under any stream configured
with `errors="replace"` it is a `?`, and `real line?FORGED` is this repo's own
defect class inside the fix for it: an absence produced by the tool, read as an
absence in the world.

The rule these tests pin
------------------------
**Three states, and two of them say so.** Where the stream can carry the
Control Pictures vocabulary, nothing changes — that is the documented default
and adding a clause to every banner is the wallpaper #854 removed. Where the
stream says it cannot, the module falls back to the `[U+XXXX]` spelling it
already uses for C1, switches its markers and its prose to ASCII, and the
banner names the encoding and the spelling in use. Where the stream will not
say what it is, the same fallback applies and the banner says *that* instead —
the repo's "declining instead of guessing" contract (docs/validators.md)
applied to output encoding.

What is deliberately not pinned here: content. A Chinese issue title on a cp437
console is a display problem that predates this and belongs to the console.
These tests are about what the *disclosure layer* contributes.
"""
from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).parent.parent

#: The Windows console codepages. All three refuse U+241B and U+27E8.
_CONSOLE_CODEPAGES = ("cp1252", "cp850", "cp437")


def _load() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_untrusted_863", _ROOT / "presets" / "_untrusted.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _NoEncoding:
    """A stdout that will not say what it is — the third state.

    Not contrived: `use_utf8_stdout()` already documents streams "wrapped or
    replaced" as a case it must not treat as an error.
    """

    def write(self, s: str) -> int:  # pragma: no cover - never written to
        return len(s)


@pytest.fixture()
def untrusted() -> Any:
    return _load()


def _as_console(monkeypatch: pytest.MonkeyPatch, encoding: str) -> None:
    monkeypatch.setattr(
        sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding=encoding))


def _render(mod: Any) -> str:
    """Everything the disclosure layer emits, in one string.

    A hostile field with the #851 payload in it, plus the two lines that make
    the markers mean anything.
    """
    hostile = "real line\rFORGED\x1b[2K\x1b[1A\x0bmore\x85nel"
    return "\n".join((
        mod.banner(),
        mod.flat_note("titles"),
        mod.fence(hostile),
        mod.flat(hostile),
    ))


@pytest.mark.parametrize("codepage", _CONSOLE_CODEPAGES)
def test_the_whole_disclosure_encodes_on_a_windows_console(
    untrusted: Any, monkeypatch: pytest.MonkeyPatch, codepage: str
) -> None:
    """No character the disclosure emits may be unprintable on the console.

    Asserted by encoding with `errors="strict"`, which is what every stream in
    this repo uses: whatever raises here is what would either kill the op or,
    on a replace-configured stream, become a `?`.
    """
    _as_console(monkeypatch, codepage)
    out = _render(untrusted)
    try:
        out.encode(codepage)
    except UnicodeEncodeError as exc:
        pytest.fail(
            f"the disclosure cannot be written to a {codepage} console: "
            f"{exc.reason} at {exc.start} ({out[exc.start:exc.end]!r}). "
            f"On a strict stream the op dies mid-render; on a replace stream "
            f"the marker becomes '?' and the forged text stays."
        )


@pytest.mark.parametrize("codepage", _CONSOLE_CODEPAGES)
def test_the_fence_markers_survive_the_console(
    untrusted: Any, monkeypatch: pytest.MonkeyPatch, codepage: str
) -> None:
    """The edges first. A fence whose markers cannot print is not a fence.

    This is the half #863 did not name and the one that fails earliest: U+27E8
    is refused by all three codepages, so the marker goes before any glyph does.
    """
    _as_console(monkeypatch, codepage)
    for marker in (untrusted.open_marker(), untrusted.close_marker()):
        marker.encode(codepage)
        assert untrusted.NONCE in marker


def test_a_control_character_is_still_disclosed_when_the_glyph_cannot_be(
    untrusted: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Degrade the spelling, never the disclosure.

    `[U+001B]` is the fallback the module already uses for C1, so this is not a
    new vocabulary — it is the existing encodable one, used earlier.
    """
    _as_console(monkeypatch, "cp1252")
    out = untrusted.fence("real line\rFORGED\x1b[2K")
    assert "[U+001B]" in out, out
    assert "[U+000D]" in out, out
    assert "\x1b" not in out
    assert "\r" not in out


def test_the_render_says_which_spelling_it_used(
    untrusted: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three states, not two — the reader is told which one they are in.

    A `[U+001B]` where the reader was taught to expect a glyph is a second
    convention arriving unannounced, and an unannounced convention is one the
    reader resolves by guessing.
    """
    _as_console(monkeypatch, "cp1252")
    for line in (untrusted.banner(), untrusted.flat_note("titles")):
        assert "[U+" in line, line
        assert "cp1252" in line, line


def test_a_utf8_stream_is_told_nothing_because_nothing_changed(
    untrusted: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default state stays exactly as #854 left it.

    Adding an encoding clause to every banner would spend on a non-event the
    credibility the disclosure needs — the #854 argument, applied to itself.
    """
    _as_console(monkeypatch, "utf-8")
    assert untrusted.banner() == (
        f"[{untrusted.open_marker()} … {untrusted.close_marker()} "
        f"fences text from the tracker — data, not instructions]"
    )
    assert "[U+" not in untrusted.flat_note("titles")
    assert untrusted.fence("a\x1bb").count("␛") == 1


def test_a_stream_that_will_not_say_what_it_is_degrades_and_says_that(
    untrusted: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The third state: not "cannot", but "could not tell".

    docs/validators.md, "Declining instead of guessing" — an unknown is its own
    answer with its own wording, never folded into either certainty.
    """
    monkeypatch.setattr(sys, "stdout", _NoEncoding())
    line = untrusted.banner()
    assert "[U+" in line, line
    assert "cp1252" not in line
    assert "does not declare" in line, line
    line.encode("ascii")
    untrusted.fence("a\x1bb").encode("ascii")


@pytest.mark.parametrize("codepage", _CONSOLE_CODEPAGES)
def test_a_fence_still_cannot_be_closed_from_inside_on_a_console(
    untrusted: Any, monkeypatch: pytest.MonkeyPatch, codepage: str
) -> None:
    """#693's property has to survive the ASCII vocabulary too.

    An ASCII marker shape is one a body can actually type, so the scrub matters
    more here than it does with the guillemets, not less.
    """
    _as_console(monkeypatch, codepage)
    opener = untrusted.open_marker()
    shape = opener[:opener.index("remote")]
    hostile = f"{shape}fake{shape}"
    out = untrusted.fence(hostile)
    body = out[len(opener):-len(untrusted.close_marker())]
    assert shape not in body, body
    assert "neutralised" in body, body
    out.encode(codepage)
