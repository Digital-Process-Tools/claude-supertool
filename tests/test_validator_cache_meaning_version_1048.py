"""A cache entry written under one MEANING must not be read under another (#1048).

The validator cache key is `content ‖ name ‖ cmd ‖ tool-fingerprint`. Every one
of those describes *what was analysed and by which build of the analyser*. None
of them describes **what the stored fields mean to the core reading them back**.

So when the core changes its own interpretation of a field it already owns — a
count that starts excluding a category, an `ok` that starts implying something
narrower, a key that becomes core-only — entries written under the old meaning
verify (this machine's secret signed them), are inside the TTL, and are read
under the new rules. Nothing is forged. The bytes were correct when written.

The version component is derived, not declared. Two sources, both mechanical:

* `validators/SCHEMA.md`, which is this repo's canonical statement of what each
  field means. A meaning change that does not touch it is already a contract
  violation with its own tests.
* the sorted `_VALIDATOR_CORE_ONLY_KEYS` set, the one meaning-bearing contract
  that lives in code rather than in the doc.

Content-hashed rather than stat-ed on purpose: a reinstall or a fresh clone
rewrites identical bytes at a new mtime, and keying on that would cold-invalidate
every validator cache on every checkout — the cost that got the blunt fix
rejected in #1044.
"""
from __future__ import annotations

from pathlib import Path

import supertool

SCHEMA_A = "| `count` | int | number of findings |\n"
SCHEMA_B = "| `count` | int | number of findings, excluding notices |\n"


def _install(tmp_path: Path, schema: str) -> Path:
    root = tmp_path / "install"
    (root / "validators").mkdir(parents=True, exist_ok=True)
    (root / "validators" / "SCHEMA.md").write_text(schema, encoding="utf-8")
    return root


def _version(monkeypatch, root: Path) -> str:
    monkeypatch.setattr(supertool, "_INSTALL_DIR", str(root))
    monkeypatch.setattr(supertool, "_VALIDATOR_MEANING_VERSION", None)
    return supertool._validator_meaning_version()


def _key(monkeypatch, root: Path, target: Path, cmd: str) -> str:
    monkeypatch.setattr(supertool, "_INSTALL_DIR", str(root))
    monkeypatch.setattr(supertool, "_VALIDATOR_MEANING_VERSION", None)
    supertool._VALIDATOR_FINGERPRINT_CACHE.clear()
    key = supertool._validator_cache_key(str(target), "phpstan-mcp", cmd, {})
    assert key is not None
    return key


def test_a_changed_field_meaning_changes_the_cache_key(tmp_path, monkeypatch) -> None:
    """The property #1048 asks for: same file, same tools, different meaning."""
    monkeypatch.setattr(supertool, "_load_config", lambda: {})
    target = tmp_path / "Subject.php"
    target.write_text("<?php\n", encoding="utf-8")
    cmd = "python3 /nonexistent/adapter.py"

    before = _key(monkeypatch, _install(tmp_path, SCHEMA_A), target, cmd)
    after = _key(monkeypatch, _install(tmp_path, SCHEMA_B), target, cmd)

    assert before != after, (
        "an entry written before the field's meaning changed is still served "
        "after it — the bytes were correct when written and are wrong when read"
    )


def test_the_core_only_key_set_is_part_of_the_meaning(tmp_path, monkeypatch) -> None:
    """The half of the contract that lives in code, not in SCHEMA.md.

    Adding a key to `_VALIDATOR_CORE_ONLY_KEYS` changes what a cached payload
    means: entries written earlier carry that key and it is now the core's word.
    """
    root = _install(tmp_path, SCHEMA_A)
    before = _version(monkeypatch, root)
    monkeypatch.setattr(supertool, "_VALIDATOR_CORE_ONLY_KEYS",
                        frozenset(supertool._VALIDATOR_CORE_ONLY_KEYS | {"verdict_of"}))
    after = _version(monkeypatch, root)

    assert before != after, (
        "a key that became core-only does not retire the entries that carry it"
    )


def test_a_reinstall_of_the_same_bytes_does_not_invalidate(tmp_path, monkeypatch) -> None:
    """Content, not `stat` — otherwise every clone pays a full cold cache.

    This is the cost that made the blunt "put the release version in the
    fingerprint" fix the wrong trade in #1044, and it is the reason this
    component is derived from bytes rather than from mtime or from a release
    number that moves whether or not any meaning did.
    """
    first = _install(tmp_path / "a", SCHEMA_A)
    second = _install(tmp_path / "b", SCHEMA_A)

    assert _version(monkeypatch, first) == _version(monkeypatch, second)


def test_an_unreadable_schema_is_keyed_apart(tmp_path, monkeypatch) -> None:
    """Three states: a meaning we know, another meaning we know, and no answer.

    An install without `validators/SCHEMA.md` cannot say which meaning its
    entries were written under. Folding that into whatever the readable case
    hashes to would let entries cross the boundary in exactly the direction this
    issue is about, so the unknown case is its own key space.
    """
    readable = _install(tmp_path, SCHEMA_A)
    missing = tmp_path / "no-install"
    missing.mkdir()

    assert _version(monkeypatch, readable) != _version(monkeypatch, missing)


def test_an_entry_written_under_one_meaning_is_a_miss_under_another(
        tmp_path, monkeypatch) -> None:
    """End to end through the real write/read pair, not just the key function."""
    monkeypatch.setattr(supertool, "_load_config", lambda: {})
    monkeypatch.setattr(supertool, "_cache_root", lambda: tmp_path / "cache")
    target = tmp_path / "Subject.php"
    target.write_text("<?php\n", encoding="utf-8")
    cmd = "python3 /nonexistent/adapter.py"

    old_key = _key(monkeypatch, _install(tmp_path, SCHEMA_A), target, cmd)
    supertool._validator_cache_write(old_key, {"tool": "phpstan-mcp", "ok": True,
                                               "count": 0, "errors": []})
    assert supertool._validator_cache_read(old_key) is not None

    new_key = _key(monkeypatch, _install(tmp_path, SCHEMA_B), target, cmd)
    assert supertool._validator_cache_read(new_key) is None, (
        "the entry written under the old meaning was served under the new one"
    )
