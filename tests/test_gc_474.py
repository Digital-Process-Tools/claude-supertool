"""#474 — supertool never GCs its own caches.

Measured on a daily-driver machine: ~/.cache/supertool reached 1.0 GB /
242k files in two weeks with no reaper anywhere in the tree. `vim-cursor`
and `vim-undo` were 99% older than 7 days; `validators` was entirely hot,
so a default policy must not touch it.

The load-bearing tests here are the retention boundary (off-by-one on a
retention window is how a hot cache gets wiped), the dry-run default
(deleting 244k files is not reversible), and the rule that an entry whose
age cannot be determined is never removed.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

import supertool

DAY = 86400.0


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the whole cache tree at tmp_path and enable auto-GC."""
    root = tmp_path / "xdg" / "supertool"
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("SUPERTOOL_GC_DISABLE", raising=False)
    root.mkdir(parents=True)
    return root


def _entry(path: Path, content: str = "x", age: float = 0.0,
           now: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    t = (time.time() if now is None else now) - age
    os.utime(path, (t, t))
    return path


# ---------------------------------------------------------------------------
# The retention boundary
# ---------------------------------------------------------------------------

def test_retention_boundary_is_exact(cache: Path) -> None:
    """now - retention - 1s goes; now - retention + 1s stays."""
    now = time.time()
    retention = 7 * DAY
    stale = _entry(cache / "vim-undo" / "stale", age=retention + 1, now=now)
    fresh = _entry(cache / "vim-undo" / "fresh", age=retention - 1, now=now)

    res = supertool._gc_sweep_kind("vim-undo", retention, dry=False, now=now)

    assert not stale.exists()
    assert fresh.exists()
    assert res["removed"] == 1
    assert res["kept"] == 1


def test_entry_exactly_at_the_retention_edge_is_kept(cache: Path) -> None:
    now = time.time()
    retention = 7 * DAY
    edge = _entry(cache / "vim-undo" / "edge", age=retention, now=now)

    res = supertool._gc_sweep_kind("vim-undo", retention, dry=False, now=now)

    assert edge.exists()
    assert res["removed"] == 0


# ---------------------------------------------------------------------------
# Dry run is the default surface
# ---------------------------------------------------------------------------

def test_bare_gc_op_deletes_nothing(cache: Path) -> None:
    now = time.time()
    stale = _entry(cache / "vim-cursor" / "a", "12345", age=30 * DAY, now=now)

    out = supertool.dispatch("gc")

    assert stale.exists(), "bare `gc` must be a preview, not a deletion"
    assert "dry run" in out.lower()
    assert "vim-cursor" in out
    assert "5 B" in out, "preview must report bytes, not just counts"
    assert "1 stale" in out


def test_gc_dry_reports_counts_and_bytes_per_kind(cache: Path) -> None:
    now = time.time()
    _entry(cache / "vim-cursor" / "a", "aaa", age=30 * DAY, now=now)
    _entry(cache / "vim-undo" / "b", "bbbbb", age=30 * DAY, now=now)
    _entry(cache / "vim-undo" / "c", "c", age=1 * DAY, now=now)

    out = supertool.dispatch("gc:dry")

    assert "vim-cursor" in out and "vim-undo" in out
    assert "3 B" in out and "5 B" in out
    assert "8 B" in out, "a total across kinds must be reported"
    assert "gc:run" in out, "the preview must say how to actually delete"


def test_gc_run_deletes(cache: Path) -> None:
    now = time.time()
    stale = _entry(cache / "vim-cursor" / "a", age=30 * DAY, now=now)
    fresh = _entry(cache / "vim-cursor" / "b", age=1 * DAY, now=now)

    out = supertool.dispatch("gc:run")

    assert not stale.exists()
    assert fresh.exists()
    assert "1 removed" in out
    assert "dry run" not in out.lower()


def test_gc_run_can_be_scoped_to_one_kind(cache: Path) -> None:
    now = time.time()
    cursor = _entry(cache / "vim-cursor" / "a", age=30 * DAY, now=now)
    undo = _entry(cache / "vim-undo" / "b", age=30 * DAY, now=now)

    out = supertool.dispatch("gc:run:vim-undo")

    assert cursor.exists(), "an explicit kind must not sweep the others"
    assert not undo.exists()
    assert "vim-cursor" not in out


def test_gc_rejects_an_unknown_kind(cache: Path) -> None:
    out = supertool.dispatch("gc:run:nope")
    assert "ERROR" in out
    assert "vim-undo" in out, "the error must list the kinds that do exist"


# ---------------------------------------------------------------------------
# An age it cannot determine is not evidence of staleness
# ---------------------------------------------------------------------------

def test_entry_with_a_future_mtime_is_kept_and_reported(cache: Path) -> None:
    now = time.time()
    skewed = _entry(cache / "vim-undo" / "skewed", age=-3600, now=now)
    stale = _entry(cache / "vim-undo" / "stale", age=30 * DAY, now=now)

    res = supertool._gc_sweep_kind("vim-undo", 7 * DAY, dry=False, now=now)

    assert skewed.exists(), "a clock-skewed entry must never be silently removed"
    assert not stale.exists()
    assert res["skipped"] == 1
    assert res["removed"] == 1


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_entry_whose_stat_fails_is_kept_and_reported(cache: Path) -> None:
    now = time.time()
    (cache / "vim-undo").mkdir(parents=True, exist_ok=True)
    dangling = cache / "vim-undo" / "dangling"
    dangling.symlink_to(cache / "vim-undo" / "does-not-exist")

    res = supertool._gc_sweep_kind("vim-undo", 7 * DAY, dry=False, now=now)

    assert dangling.is_symlink(), "unstattable entry must be left alone"
    assert res["skipped"] == 1
    assert res["removed"] == 0


def test_skipped_entries_are_surfaced_in_the_receipt(cache: Path) -> None:
    now = time.time()
    _entry(cache / "vim-undo" / "skewed", age=-3600, now=now)

    out = supertool.dispatch("gc:dry")

    assert "skipped 1" in out


# ---------------------------------------------------------------------------
# Per-kind defaults — validators was measured entirely hot
# ---------------------------------------------------------------------------

def test_validators_default_window_is_wider_than_vim_undo() -> None:
    d = supertool._GC_DEFAULT_RETENTION_DAYS
    assert d["vim-undo"] == 7
    assert d["vim-cursor"] == 7
    assert d["validators"] == 30
    assert d["validators"] > d["vim-undo"], (
        "validators was measured with zero entries older than 7 days — one "
        "number for all four kinds would put a hot cache inside the window"
    )


def test_default_sweep_spares_a_ten_day_old_validator_entry(cache: Path) -> None:
    now = time.time()
    validator = _entry(cache / "validators" / "k.json", age=10 * DAY, now=now)
    undo = _entry(cache / "vim-undo" / "u", age=10 * DAY, now=now)

    supertool.dispatch("gc:run")

    assert validator.exists()
    assert not undo.exists()


def test_retention_is_configurable_per_kind(cache: Path) -> None:
    supertool._CONFIG = {"gc": {"retention_days": {"vim-cursor": 1}}}
    now = time.time()
    f = _entry(cache / "vim-cursor" / "a", age=2 * DAY, now=now)

    supertool.dispatch("gc:run")

    assert not f.exists(), "config must override the 7-day default"


def test_receipt_names_the_retention_window_it_used(cache: Path) -> None:
    _entry(cache / "vim-undo" / "a", age=1 * DAY)
    out = supertool.dispatch("gc:dry")
    assert "7d" in out, "a user must be able to see why nothing was removed"


# ---------------------------------------------------------------------------
# Blast radius
# ---------------------------------------------------------------------------

def test_gc_never_touches_the_hmac_secret_or_unknown_directories(cache: Path) -> None:
    secret = _entry(cache / ".cache_key", "s" * 32, age=365 * DAY)
    stranger = _entry(cache / "something-else" / "f", age=365 * DAY)
    loose = _entry(cache / "loose-file", age=365 * DAY)
    canary = _entry(cache / "vim-undo" / "a", age=365 * DAY)

    supertool.dispatch("gc:run")

    assert not canary.exists(), "the sweep must actually have run"
    assert secret.exists(), "deleting the HMAC secret would invalidate every cache entry"
    assert stranger.exists()
    assert loose.exists()


def test_gc_does_not_descend_into_or_delete_subdirectories(cache: Path) -> None:
    now = time.time()
    nested = _entry(cache / "vim-undo" / "sub" / "deep", age=365 * DAY, now=now)

    res = supertool._gc_sweep_kind("vim-undo", 7 * DAY, dry=False, now=now)

    assert nested.exists()
    assert (cache / "vim-undo" / "sub").is_dir()
    assert res["removed"] == 0


def test_missing_kind_directory_is_not_an_error(cache: Path) -> None:
    res = supertool._gc_sweep_kind("vim-undo", 7 * DAY, dry=False)
    assert res["removed"] == 0
    assert res["missing"] is True


def test_gc_does_not_shell_out(cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """BSD `find -delete` silently no-opped on macOS — 269 matched files left
    untouched, zero exit. The unlink happens in Python or not at all."""
    def boom(*a: object, **k: object) -> None:
        raise AssertionError("gc must not spawn a subprocess")

    monkeypatch.setattr(supertool.subprocess, "run", boom)
    monkeypatch.setattr(supertool.subprocess, "Popen", boom)
    now = time.time()
    stale = _entry(cache / "vim-undo" / "a", age=30 * DAY, now=now)

    supertool.dispatch("gc:run")

    assert not stale.exists()


# ---------------------------------------------------------------------------
# The automatic trigger — deterministic, at most once per interval
# ---------------------------------------------------------------------------

def test_auto_gc_runs_at_most_once_per_interval(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(supertool, "_gc_sweep_all",
                        lambda *a, **k: calls.append(1) or [])

    supertool._maybe_auto_gc()
    supertool._maybe_auto_gc()
    supertool._maybe_auto_gc()

    assert len(calls) == 1
    assert (cache / ".gc-stamp").exists()


def test_auto_gc_runs_again_once_the_interval_has_elapsed(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(supertool, "_gc_sweep_all",
                        lambda *a, **k: calls.append(1) or [])

    supertool._maybe_auto_gc()
    stamp = cache / ".gc-stamp"
    old = time.time() - 7200
    os.utime(stamp, (old, old))
    supertool._maybe_auto_gc()

    assert len(calls) == 2


def test_auto_gc_deletes_for_real(cache: Path) -> None:
    now = time.time()
    stale = _entry(cache / "vim-undo" / "a", age=30 * DAY, now=now)
    supertool._maybe_auto_gc()
    assert not stale.exists()


def test_auto_gc_never_raises_and_still_stamps(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cache prune that raises during someone's edit is a worse bug than
    the disk usage — and it must not retry on every subsequent call."""
    def explode(*a: object, **k: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(supertool, "_gc_sweep_all", explode)

    supertool._maybe_auto_gc()

    assert (cache / ".gc-stamp").exists()


def test_auto_gc_is_disabled_by_env(cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPERTOOL_GC_DISABLE", "1")
    calls: list[int] = []
    monkeypatch.setattr(supertool, "_gc_sweep_all",
                        lambda *a, **k: calls.append(1) or [])

    supertool._maybe_auto_gc()

    assert calls == []
    assert not (cache / ".gc-stamp").exists()


def test_auto_gc_is_disabled_by_config(cache: Path) -> None:
    supertool._CONFIG = {"gc": {"enabled": False}}
    now = time.time()
    stale = _entry(cache / "vim-undo" / "a", age=30 * DAY, now=now)

    supertool._maybe_auto_gc()

    assert stale.exists()


def test_auto_gc_interval_is_configurable(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supertool._CONFIG = {"gc": {"interval_seconds": 1}}
    calls: list[int] = []
    monkeypatch.setattr(supertool, "_gc_sweep_all",
                        lambda *a, **k: calls.append(1) or [])

    supertool._maybe_auto_gc()
    stamp = cache / ".gc-stamp"
    old = time.time() - 5
    os.utime(stamp, (old, old))
    supertool._maybe_auto_gc()

    assert len(calls) == 2


# ---------------------------------------------------------------------------
# The op is discoverable
# ---------------------------------------------------------------------------

def test_gc_is_listed_in_the_ops_reference() -> None:
    import json
    with open(Path(__file__).parent.parent / ".supertool.json", encoding="utf-8") as fh:
        cfg = json.load(fh)
    assert "gc" in cfg["builtin-ops"]
