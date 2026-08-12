"""The validator cache is a second door into the #1036 chokepoint (#1044).

#1036 made `_validator_run_one` drop every key the core owns from an adapter's
parsed payload, so an adapter could no longer switch off `rollback_on_fail` by
printing `"timeout": true` beside a real finding. The strip runs on the line
after `json.loads`.

`_validator_run_one` returns a cache hit *before* reaching that line. An entry
written by any pre-`7d12db2` build therefore carries the adapter's `timeout`
verbatim, it is HMAC-valid because the same machine's secret signed it, and the
cache key is `content ‖ name ‖ cmd ‖ fingerprint` with no version component — so
upgrading to the build that fixed #1036 does not invalidate it. Inside the TTL
(default 24h, `0` meaning forever) #1036 is reachable again through the cache.

The fix is the chokepoint's own claim made true: an adapter payload reaches a
decision through exactly two doors, and both strip. What the core stamps on a
result — `elapsed_s` and `resolved_to` — it stamps again on the way out of the
cache, because those are observations of *this* run, not properties of the
cached bytes. The two `test_a_cache_hit_...` cases pin that: a strip that only
removed would blank the time column and lose the resolved target, which is the
quiet regression traded for the loud one.

**The subject file is written as bytes, and the post-condition is bytes.** The
cache key is a hash of the file's raw bytes, so a fixture that seeds an entry
has to put on disk exactly what the op under test will later write there.
`Path.write_text` does not: on Windows it is text mode, so `\\n` reaches disk as
`\\r\\n`, while `_atomic_write` encodes and writes bytes with no translation.
The seeded key was therefore taken over CRLF content and the post-edit lookup
computed over LF content, the entry was never found, the live adapter answered
clean, and every test here that expects a rollback failed on all four Windows
legs while passing everywhere else — the strip was never exercised at all.
Reading back with `read_text` hid the other half of it: universal-newline
decoding turns `\\r\\n` into `\\n`, so a text comparison would have called a
CRLF file equal to an LF one.

**Each end-to-end case asserts the cache was read before it asserts anything
about the rollback**, via the adapter's own spawn count. "The strip let a forged
key through" and "the entry was never found" are the same bytes on disk and
were indistinguishable in the CI log; the count separates them, on every
platform, and names the second so nobody has to re-derive it from a red leg.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import supertool
from _adapter_verdict import run_one_or_skip

CLEAN = json.dumps({"tool": "fake", "ok": True, "count": 0, "errors": [],
                    "duration_ms": 1})
REAL_FINDING = {
    "tool": "fake", "ok": False, "count": 1,
    "errors": [{"line": 1, "col": 1, "severity": "error",
                "code": "E999", "msg": "unterminated object"}],
    "duration_ms": 1,
}

BEFORE = b'{"a": 1}\n'
AFTER = b'{"b": 1}\n'

CORE_ONLY_KEYS = frozenset({"no_verdict", "timeout", "elapsed_s", "resolved_to"})


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("SUPERTOOL_NO_VALIDATOR_CACHE", raising=False)
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("f", ""))


def _adapter(tmp_path: Path) -> "tuple[str, Path]":
    """An adapter that prints a clean verdict on every spawn, and counts them.

    Clean on every spawn is what makes these tests honest: the finding, the
    forged key and the row they produce can only have come out of the cache.
    The count is what makes them diagnosable — see the module docstring.
    `{python}` + `as_posix()` so it spawns under `shell=False` everywhere.
    """
    calls = tmp_path / "_calls.txt"
    script = tmp_path / "_adapter.py"
    script.write_text(
        "import pathlib, sys" + chr(10)
        + f"p = pathlib.Path({str(calls)!r})" + chr(10)
        + "p.write_text(str((int(p.read_text()) if p.exists() else 0) + 1))" + chr(10)
        + f"sys.stdout.write({CLEAN!r})" + chr(10),
        encoding="utf-8")
    return f"{{python}} {script.as_posix()}", calls


def _spawns(calls: Path) -> int:
    return int(calls.read_text(encoding="utf-8")) if calls.exists() else 0


def _configure(cmd: str, **extra: object) -> dict:
    spec = {"cmd": cmd, "match": "*.json", "rollback_on_fail": True,
            "hooks_into": ["edit", "replace", "replace_lines", "paste",
                           "append", "vim"],
            "timeout": 30, **extra}
    supertool._CONFIG = {"validators": {"fake": spec}}
    supertool._CONFIG_CHECKED = True
    return spec


def _seed_cache(spec: dict, f: Path, content: bytes, payload: dict) -> None:
    """Put `payload` verbatim under the key the core uses for `content`.

    This is what a pre-`7d12db2` build left on disk: signed with this machine's
    own secret, so it verifies. The key is taken from a real run rather than
    recomputed here — recomputing it would mean re-deriving the substituted
    `cmd` and the fingerprint, and a test that reimplements the key would pass
    against a key nothing reads. `write_bytes`, for the reason in the module
    docstring: the key is a hash of what is on disk.
    """
    written: list = []
    real_write = supertool._validator_cache_write
    real_read = supertool._validator_cache_read
    original = f.read_bytes()
    f.write_bytes(content)
    try:
        supertool._validator_cache_write = lambda k, d: written.append(k)
        supertool._validator_cache_read = lambda k: None
        run_one_or_skip("fake", spec, str(f))
    finally:
        supertool._validator_cache_write = real_write
        supertool._validator_cache_read = real_read
        f.write_bytes(original)
    assert written, "the priming run never reached the cache write"
    supertool._validator_cache_write(written[-1], payload)


def _edit(f: Path, capsys) -> "tuple[str, bytes]":
    supertool.main([f'edit:::"a":::"b":::{f}'])
    return capsys.readouterr().out, f.read_bytes()


def _assert_cache_was_read(calls: Path, spawned_before: int, out: str) -> None:
    """The precondition, checked before the post-condition it would explain.

    One spawn for the pre-edit baseline; the post-edit pass must find the
    seeded entry and spawn nothing. Two spawns means the lookup missed, the
    live adapter answered clean, and whatever the file's bytes say afterwards
    says nothing about the strip.
    """
    assert _spawns(calls) == spawned_before + 1, (
        f"the post-edit pass spawned the adapter instead of reading the "
        f"seeded cache entry — the key it looked up is not the key the entry "
        f"was written under, so this run exercised no strip at all:"
        f"{chr(10)}{out}")


# ---------------------------------------------------------------------------
# THE bug — the post-condition is the file's bytes
# ---------------------------------------------------------------------------

def test_a_cached_forged_timeout_does_not_disable_the_rollback_guard(
        tmp_path: Path, capsys) -> None:
    """A live adapter that never forges anything, and one stale cache entry."""
    cmd, calls = _adapter(tmp_path)
    spec = _configure(cmd)
    f = tmp_path / "s.json"
    f.write_bytes(BEFORE)
    _seed_cache(spec, f, AFTER, {**REAL_FINDING, "timeout": True})
    spawned = _spawns(calls)

    out, raw = _edit(f, capsys)
    _assert_cache_was_read(calls, spawned, out)
    assert raw == BEFORE, (
        f"a cached result claimed a timeout the core never observed and the "
        f"rollback did not run:{chr(10)}{out}")
    assert "rolled back" in out, out


def test_a_cached_forged_no_verdict_does_not_disable_the_rollback_guard(
        tmp_path: Path, capsys) -> None:
    cmd, calls = _adapter(tmp_path)
    spec = _configure(cmd)
    f = tmp_path / "s.json"
    f.write_bytes(BEFORE)
    _seed_cache(spec, f, AFTER, {**REAL_FINDING, "no_verdict": True})
    spawned = _spawns(calls)

    out, raw = _edit(f, capsys)
    _assert_cache_was_read(calls, spawned, out)
    assert raw == BEFORE, (
        f"a cached forged no_verdict survived into the guard:{chr(10)}{out}")
    assert "rolled back" in out, out


@pytest.mark.parametrize("key", sorted(CORE_ONLY_KEYS))
def test_no_core_only_key_in_a_cache_entry_can_save_a_bad_edit(
        key: str, tmp_path: Path, capsys) -> None:
    """The class, through the cache door, exactly as #1036 pinned the other."""
    cmd, calls = _adapter(tmp_path)
    spec = _configure(cmd)
    f = tmp_path / "s.json"
    f.write_bytes(BEFORE)
    _seed_cache(spec, f, AFTER, {**REAL_FINDING, key: True})
    spawned = _spawns(calls)

    out, raw = _edit(f, capsys)
    _assert_cache_was_read(calls, spawned, out)
    assert raw == BEFORE, (
        f"a cached {key!r} suppressed the rollback:{chr(10)}{out}")


# ---------------------------------------------------------------------------
# The quiet regression the loud fix could buy: stripping and not re-stamping
# ---------------------------------------------------------------------------

def test_a_cache_hit_still_carries_an_elapsed_s(tmp_path: Path) -> None:
    """`elapsed_s` is the time column. A strip that only removes blanks it."""
    cmd, calls = _adapter(tmp_path)
    spec = _configure(cmd)
    f = tmp_path / "s.json"
    f.write_bytes(AFTER)

    first = run_one_or_skip("fake", spec, str(f))
    assert first is not None and first.get("elapsed_s") is not None, first
    second = run_one_or_skip("fake", spec, str(f))
    assert second is not None

    assert _spawns(calls) == 1, (
        "the second run spawned the adapter again — this test never "
        "exercised the cache-read path")
    assert second.get("ok") is True, second
    assert second.get("elapsed_s") is not None, (
        f"a cache hit lost the time column: {second!r}")


def test_a_cache_hit_restamps_resolved_to_from_the_core(tmp_path: Path) -> None:
    """`resolved_to` names the file that was actually judged.

    The core learns it from its own `resolve` run; an adapter's copy is dropped
    on the fresh path and must be dropped here too, and the core's own must
    still be on the result a cache hit returns.
    """
    target = tmp_path / "t.json"
    target.write_bytes(AFTER)
    resolver = tmp_path / "_resolve.py"
    resolver.write_text(
        "import sys" + chr(10)
        + f"sys.stdout.write({target.as_posix()!r})" + chr(10),
        encoding="utf-8")
    cmd, calls = _adapter(tmp_path)
    spec = _configure(cmd, resolve=f"{{python}} {resolver.as_posix()}")
    source = tmp_path / "s.json"
    source.write_bytes(BEFORE)

    fresh = run_one_or_skip("fake", spec, str(source))
    assert fresh is not None
    assert Path(fresh["resolved_to"]) == Path(target)

    _seed_cache(spec, source, BEFORE,
                {**json.loads(CLEAN), "resolved_to": "/forged/elsewhere.json"})
    spawned = _spawns(calls)
    cached = run_one_or_skip("fake", spec, str(source))
    assert cached is not None
    assert _spawns(calls) == spawned, (
        f"the run spawned the adapter instead of reading the seeded entry, so "
        f"nothing here was read off the cache path: {cached!r}")
    assert Path(cached["resolved_to"]) == Path(target), (
        f"a cache hit reported a target the core did not resolve: {cached!r}")
