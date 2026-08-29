"""Verdict cache for classify's model stage (#2054).

`classify` has no memory: `check.py` is a fresh subprocess per call, and an
in-process `lru_cache` on `model.classify` would not survive that -- it dies
with the process that would have populated it. Everything here exercises
`presets/classify/cache.py`'s file-backed store directly, with a `directory`
and `now` passed explicitly so nothing touches the real `/tmp` or the real
clock.

Three constraints straight out of the issue, and each gets a must-fire /
must-not-fire pair in the same fixture rather than one half tested alone:

1. keyed on text, never on the prompt (`_untrusted.fence` draws a fresh nonce
   per process, so a key on the prompt would never hit twice for identical
   text);
2. `could-not-classify` is never written, ever -- the transient bucket must
   not calcify into a permanent verdict;
3. the key carries a version derived from `model.AXES` and
   `model._SYSTEM_PROMPT`, so a change to either invalidates every entry
   without a hand-bumped counter.
"""
from __future__ import annotations

from _preset_loader import load_preset_module
from _symlink import require_symlink

cache = load_preset_module("classify", "cache", prefix="cls_cache_")
# `cache.py` does its own bare `import model` internally (a sibling module,
# same convention `check.py` and `scanner.py` already use) -- reusing that
# reference here, rather than a second `load_preset_module("classify",
# "model", ...)` call, is not a style choice: a second load creates a
# SECOND, unrelated `Verdict` class object, and `Verdict.__eq__` checks
# `isinstance(other, Verdict)` against whichever class built it. Two
# separately-loaded "model" modules produce two classes that never compare
# equal to each other no matter how identical their fields are.
model = cache.model


def _cache(tmp_path, safe_ttl=cache.SAFE_TTL_SECONDS):
    return cache.Cache(directory=str(tmp_path / "cache"), safe_ttl=safe_ttl)


# --- key: text, never the prompt -------------------------------------------

def test_the_key_depends_on_the_text_not_on_a_prompt_wrapper() -> None:
    """A prompt is `_untrusted.fence(text)`, fresh-nonced per process --
    keying on it would never hit twice for the same text. The key must be a
    function of the bare text (plus the version), not of anything fence()
    would produce."""
    k1 = cache.key("hello")
    k2 = cache.key("hello")
    assert k1 == k2, "the same text must produce the same key every time"


def test_different_text_gets_a_different_key() -> None:
    assert cache.key("hello") != cache.key("goodbye")


# --- version: derived from live constants, not hand-bumped -----------------

def test_the_version_changes_when_axes_change(monkeypatch) -> None:
    before = cache.version()
    monkeypatch.setattr(model, "AXES", model.AXES + ("a-new-axis",))
    after = cache.version()
    assert before != after, (
        "AXES defines what a verdict means -- a cached verdict under the "
        "old axis list must key differently under a wider one")


def test_the_version_changes_when_the_system_prompt_changes(monkeypatch) -> None:
    before = cache.version()
    monkeypatch.setattr(model, "_SYSTEM_PROMPT", model._SYSTEM_PROMPT + " extra")
    after = cache.version()
    assert before != after


def test_the_cache_key_changes_when_the_version_changes() -> None:
    v1 = "version-one"
    v2 = "version-two"
    assert cache.key("hello", v1) != cache.key("hello", v2)


# --- get/put round trip, and the must-not-fire half: could-not-classify ----

def test_a_safe_verdict_round_trips(tmp_path) -> None:
    c = _cache(tmp_path)
    v = model.Verdict("safe", [], "")
    assert c.put("hello", v) == ""
    got, status = c.get("hello")
    assert status == "hit"
    assert got == v


def test_a_suspect_verdict_round_trips(tmp_path) -> None:
    c = _cache(tmp_path)
    v = model.Verdict("suspect", ["role-persona"], "model flagged: role-persona")
    assert c.put("x", v) == ""
    got, status = c.get("x")
    assert status == "hit"
    assert got == v


def test_could_not_classify_is_never_written() -> None:
    """The must-not-fire half of constraint 2. Paired with the two round-trip
    tests above, which are the must-fire half in the same module: a real
    verdict is cached, the transient one never is."""
    def _put_then_get(tmp_path):
        c = _cache(tmp_path)
        v = model.Verdict("could-not-classify", [], "spawn timed out after 45s")
        reason = c.put("flaky text", v)
        got, status = c.get("flaky text")
        return reason, got, status
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        reason, got, status = _put_then_get(Path(d))
    assert reason != "", "put() must refuse a could-not-classify verdict"
    assert got is None
    assert status == "miss", (
        "a refused write must leave a plain miss behind, not a fake hit and "
        "not something that reads as could-not-classify on its own")


# --- miss: no entry was ever written ---------------------------------------

def test_an_absent_entry_is_a_miss_not_an_error(tmp_path) -> None:
    c = _cache(tmp_path)
    got, status = c.get("never seen before")
    assert got is None
    assert status == "miss"


# --- TTL: safe expires, suspect does not (#2054 leaves this to the author) --

def test_a_safe_entry_expires_after_its_ttl(tmp_path) -> None:
    c = _cache(tmp_path, safe_ttl=100)
    v = model.Verdict("safe", [], "")
    c.put("hello", v, now=1000.0)
    got, status = c.get("hello", now=1000.0 + 100.0 + 1.0)
    assert got is None
    assert status == "expired"


def test_a_safe_entry_within_its_ttl_still_hits(tmp_path) -> None:
    """Must-fire half of the TTL pair directly above: a safe entry read
    before its TTL elapses is still a hit, not degraded into a miss along
    the way to proving expiry works."""
    c = _cache(tmp_path, safe_ttl=100)
    v = model.Verdict("safe", [], "")
    c.put("hello", v, now=1000.0)
    got, status = c.get("hello", now=1000.0 + 50.0)
    assert status == "hit"
    assert got == v


def test_a_suspect_entry_never_expires(tmp_path) -> None:
    c = _cache(tmp_path, safe_ttl=100)
    v = model.Verdict("suspect", ["credential-shape"], "model flagged: credential-shape")
    c.put("x", v, now=1000.0)
    ten_years = 10 * 365 * 24 * 3600
    got, status = c.get("x", now=1000.0 + ten_years)
    assert status == "hit", "suspect must be cached indefinitely, not given a TTL"
    assert got == v


# --- unreadable: corrupt cache must never render as a verdict, and must not
# collapse into a plain miss either, because the two can call for different
# caller behaviour -----------------------------------------------------------

def test_a_corrupt_cache_file_is_unreadable_not_a_miss_and_not_a_verdict(tmp_path) -> None:
    c = _cache(tmp_path)
    v = model.Verdict("safe", [], "")
    assert c.put("hello", v) == ""
    path = c._path(cache.key("hello"))
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not json")
    got, status = c.get("hello")
    assert got is None, "a corrupt entry must never be handed back as a verdict"
    assert status != "miss", (
        "a file that exists but cannot be read is a different fact than no "
        "file at all -- collapsing them can change a caller's behaviour")
    assert "unreadable" in status


def test_an_entry_with_an_unexpected_shape_is_unreadable(tmp_path) -> None:
    path_dir = tmp_path / "cache"
    path_dir.mkdir(parents=True)
    c2 = cache.Cache(directory=str(path_dir))
    p = c2._path(cache.key("hello"))
    import json
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"state": "could-not-classify", "axes": [], "reason": "",
                   "written": 0}, f)
    got, status = c2.get("hello")
    assert got is None
    assert "unreadable" in status, (
        "an entry naming could-not-classify should never have been written "
        "in the first place -- reading one back must refuse it rather than "
        "hand back a verdict this cache is not supposed to be able to hold")


# --- a symlink planted at the entry's own name is refused, not followed ----
# `get()`'s `os.open(path, os.O_RDONLY | nofollow)` is the guard; nothing in
# this module exercised it before this test, which is exactly the gap #2054's
# own auditor round flagged: an edit that silently dropped the O_NOFOLLOW bit
# (a "simplification" to a plain `open()`) would not have reddened anywhere.
# `require_symlink()` -- not a `skipif(os.name == "nt")` -- so this runs
# wherever the privilege is actually present, Windows included, rather than
# hardcoding "Windows cannot" the way #1143's own module docstring warns
# against.

def test_a_symlink_planted_at_the_entry_name_is_refused_not_followed(
        tmp_path) -> None:
    require_symlink()
    c = _cache(tmp_path)
    victim_dir = tmp_path / "victim"
    victim_dir.mkdir()
    victim = victim_dir / "secret.json"
    victim.write_text(
        '{"state": "safe", "axes": [], "reason": "", "written": 0}',
        encoding="utf-8")
    root, why = c._root()
    assert root is not None, "the cache root itself must establish cleanly: {0}".format(why)
    entry_path = c._path(cache.key("hello"))
    import os as _os
    _os.symlink(str(victim), entry_path)

    got, status = c.get("hello")

    assert got is None, (
        "a symlinked entry must never be followed into a verdict, even one "
        "that happens to parse as a well-formed cache entry")
    assert status != "miss", (
        "a symlink at the name is a different fact than no entry at all")
    assert "unreadable" in status
    # The positive control this test's own docstring promises: an ordinary,
    # non-symlinked entry at the same path still round-trips as a hit --
    # proving the refusal above is actually about the symlink and not some
    # unrelated brokenness in this fixture's setup.
    _os.unlink(entry_path)
    v = model.Verdict("safe", [], "")
    assert c.put("hello", v) == ""
    got2, status2 = c.get("hello")
    assert status2 == "hit"
    assert got2 == v
