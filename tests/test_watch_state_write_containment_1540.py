"""Every writer of a state file must prove the name it opens (#1540).

#1518 established the *derived* state directory through `_image_root.ensure`,
and wired it into `claim_pidfile` -- the one path that spawns a poller. It is
also the one path that was never the problem. `transport.write_state` opened
`f"{state_path}.tmp"` with a plain `open(..., "w")`, and it is reached from
**reader** processes that never claim a slot:

* `record_death` from `dispatcher.cmd_status`, from `watcher_pids` and from
  `reap_dead_pidfile` -- i.e. from `watches`, from `unwatch` and from the
  `radar` heal;
* `clear_deaths` from `cmd_watch` and `cmd_unwatch`.

A co-tenant creates `/tmp/supertool-watch-<name>/` first (the channel name is
public: `.supertool.json` commits `"watch_name": "oss-supertool"`), plants a
regular pid file naming a dead PID, and a **symlink** at
`supertool-watch-<src>__<id>.state.json.tmp`. The next `watches` reaps the
stale pidfile, `record_death` writes, and `open(tmp, "w")` follows the link and
truncates the target -- filling it with JSON whose `last_event.payload` is
remote text. Reproduced in a scratch `BASE_DIR` before the fix; never in the
real `/tmp`, where a live watch fleet runs.

Two halves, and the split matters because they cover different populations:

* `O_NOFOLLOW` on the `.tmp` open covers **every** channel, including the
  unnamed default where the state files sit loose in world-writable `/tmp` and
  there is no derived directory to establish at all. This is the reachable step.
* establishing the derived directory on every write covers the **named**
  channel's remaining shape: a squatter who owns the directory rather than
  planting a link inside it. That is the residual `naming.ensure_state_dir`
  already claimed was closed, and for readers it was not.

**Windows.** `O_NOFOLLOW` does not exist there and `_NOFOLLOW` is already `0`,
so the two symlink arms carry `require_symlink()` rather than a `skipif` that
would report a coverage this suite does not have. The ownership arm moves *our*
uid (`_image_root._euid`) exactly as `test_watch_state_dir_containment_1518.py`
does, so it runs everywhere; `_image_root.refusal` declines that arm on a
platform where `st_uid` is a constant, which is the honest grade and not a pass.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from _changelog_findable import assert_change_is_findable
from _symlink import require_symlink

REPO = Path(__file__).resolve().parents[1]
for _dir in (str(REPO / "presets" / "watch"), str(REPO / "presets"), str(REPO / "tests")):
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import naming  # noqa: E402
import transport  # noqa: E402

#: What the victim file holds before anything hostile is attempted, and what it
#: must still hold afterwards.
INTACT = "original content"


def _channel(monkeypatch, tmp_path, make: bool = True):
    """Point `transport` at a scratch derived state directory. Returns its path.

    `RESOLVED` as well as `STATE_DIR`: `ensure_state_dir` refuses to create
    anything the operator supplied (#693), so a fixture that patched only the
    path would exercise the arm that returns "" without looking.
    """
    resolved = naming.resolve({naming.NAME_ENV: "oss"})
    assert resolved.state_dir_is_derived, "the fixture stopped exercising the derived path"
    root = tmp_path / "supertool-watch-oss"
    if make:
        root.mkdir(mode=0o700)
    monkeypatch.setattr(transport, "RESOLVED", resolved)
    monkeypatch.setattr(transport, "STATE_DIR", str(root))
    return root


def _victim(tmp_path) -> Path:
    v = tmp_path / "VICTIM.txt"
    v.write_text(INTACT, encoding="utf-8")
    return v


# ---------------------------------------------------------------------------
# 1. The reachable step: a symlink at the .tmp name
# ---------------------------------------------------------------------------

def test_a_symlink_on_the_state_tmp_is_not_written_through(tmp_path, monkeypatch):
    require_symlink()
    _channel(monkeypatch, tmp_path)
    victim = _victim(tmp_path)
    os.symlink(victim, transport.state_path("gh-prs", "x") + ".tmp")

    transport.write_state("gh-prs", "x", {"last_event": {"payload": "REMOTE TEXT"}})

    assert victim.read_text(encoding="utf-8") == INTACT, (
        "the write followed a planted symlink and overwrote {0}".format(victim))


def test_the_reader_path_writes_nothing_through_a_planted_symlink(tmp_path, monkeypatch):
    """The chain from the issue, entered where a reader enters it.

    `record_death` is called by `watches`, by `unwatch` and by the `radar`
    heal, in processes that never called `claim_pidfile` -- so nothing on this
    path had ever asked a question about the directory or about the name.
    """
    require_symlink()
    _channel(monkeypatch, tmp_path)
    victim = _victim(tmp_path)
    os.symlink(victim, transport.state_path("gl-mrs", "21803") + ".tmp")

    transport.record_death("gl-mrs", "21803", 4242)

    assert victim.read_text(encoding="utf-8") == INTACT, (
        "record_death wrote through the link into {0}".format(victim))


# ---------------------------------------------------------------------------
# 2. The other half: a directory somebody else owns
# ---------------------------------------------------------------------------

def test_a_reader_writes_nothing_into_a_state_directory_owned_by_another_uid(
        tmp_path, monkeypatch):
    """Driven by moving *our* uid rather than the directory's, for the reason
    `test_watch_state_dir_containment_1518.py` gives: a test cannot chown to a
    uid it does not have, and this keeps the arm reachable from the caller on
    every platform."""
    root = _channel(monkeypatch, tmp_path)
    monkeypatch.setattr(naming._image_root, "_euid", lambda: 4242)

    transport.write_state("gh-prs", "x", {"last_event": {"payload": "REMOTE TEXT"}})

    landed = sorted(p.name for p in root.iterdir())
    assert landed == [], "a reader wrote into the squatted directory: {0}".format(landed)


def test_a_refused_write_is_not_reported_as_a_recorded_death(tmp_path, monkeypatch):
    """The absence-read-as-presence half. `record_death` returned True -- "newly
    recorded" -- while `write_state` swallowed the `OSError` and recorded
    nothing, so the respawn cap counted a death the ledger does not hold."""
    _channel(monkeypatch, tmp_path)
    monkeypatch.setattr(naming._image_root, "_euid", lambda: 4242)

    assert transport.record_death("gl-mrs", "21803", 4242) is False, (
        "a death that was never written to disk was reported as recorded")


def test_a_refused_clear_is_not_reported_as_acknowledged(tmp_path, monkeypatch):
    """`dispatcher` prints an acknowledgement off this bool, so a swallowed
    write made `unwatch` claim it had cleared a ledger that is still on disk."""
    root = _channel(monkeypatch, tmp_path)
    (root / "supertool-watch-gl-mrs__21803.state.json").write_text(
        json.dumps({"deaths": [{"pid": 7, "ts": "2026-01-01T00:00:00Z"}]}),
        encoding="utf-8")
    monkeypatch.setattr(naming._image_root, "_euid", lambda: 4242)

    assert transport.clear_deaths("gl-mrs", "21803") is False, (
        "a ledger still on disk was reported as cleared")


# ---------------------------------------------------------------------------
# 3. The contract the guard must not have broken
# ---------------------------------------------------------------------------

def test_an_ordinary_write_still_publishes_and_reads_back(tmp_path, monkeypatch):
    """**This one passes with the production change reverted**, and is kept
    anyway: the guard replaces the open, and an open that refused everything
    would satisfy every assertion above. Named here rather than left for a
    reader to work out, because a test that would pass if the code did nothing
    is not evidence and must not be counted as any."""
    root = _channel(monkeypatch, tmp_path)

    transport.write_state("gh-prs", "x", {"cursor": 12})

    assert transport.read_state("gh-prs", "x") == {"cursor": 12}
    assert (root / "supertool-watch-gh-prs__x.state.json.tmp").exists() is False, (
        "the temporary file survived the replace")


def test_a_missing_derived_directory_is_created_by_the_first_write(
        tmp_path, monkeypatch):
    """A reader may be the first process to touch a named channel -- `watches`
    on a board with no poller yet. Establishing must create, not only verify,
    or the guard turns a working write into a silent no-op."""
    root = _channel(monkeypatch, tmp_path, make=False)
    assert not root.exists()

    transport.write_state("gh-prs", "x", {"cursor": 1})

    assert transport.read_state("gh-prs", "x") == {"cursor": 1}


def test_a_supplied_state_directory_is_still_never_created(tmp_path, monkeypatch):
    """#693 survives: only a derived directory is established, so an operator's
    own path stays their business and the write simply fails. The `O_NOFOLLOW`
    half still covers that population -- it is the only half the unnamed `/tmp`
    default ever gets.

    The `not target.exists()` half of this **would pass with the production
    change reverted**, because the old `open(tmp, "w")` also raised
    `FileNotFoundError` and swallowed it. What makes it evidence is the returned
    refusal: the old spelling returned `None` from every arm, so a caller could
    not tell this from a write that landed."""
    target = tmp_path / "supplied"
    resolved = naming.resolve({naming.NAME_ENV: "oss",
                               naming.STATE_DIR_ENV: str(target)})
    assert not resolved.state_dir_is_derived
    monkeypatch.setattr(transport, "RESOLVED", resolved)
    monkeypatch.setattr(transport, "STATE_DIR", str(target))

    why = transport.write_state("gh-prs", "x", {"cursor": 1})

    assert not target.exists()
    assert why, "a write that reached no disk at all was reported as having landed"
    assert str(target) in why, why


def test_the_change_is_documented():
    assert_change_is_findable(1540)
