"""#691 T4 (adjacent finding): `bluesky_repost` had no confirmation gate at
all -- not even the `|force` bypass its own siblings `bluesky_follow` and
`bluesky_like` carry. Same missing-guard shape as the six instances fixed
directly under #691 T4, one subsystem over: `presets/bluesky/`, and the
mechanical fix is identical (parse a `|force` suffix, call `require_confirm`
before the first network call).

Same harness pattern as `tests/test_publish_confirm_gate_691.py`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "presets"))
import _publish_safety  # noqa: E402
from _preset_loader import load_preset_module  # noqa: E402


@pytest.fixture
def strict_publish(monkeypatch, tmp_path):
    monkeypatch.delenv("SUPERTOOL_NO_PUBLISH_CONFIRM", raising=False)
    monkeypatch.chdir(tmp_path)
    if hasattr(_publish_safety, "_CACHED_CONFIG"):
        delattr(_publish_safety, "_CACHED_CONFIG")
    yield
    if hasattr(_publish_safety, "_CACHED_CONFIG"):
        delattr(_publish_safety, "_CACHED_CONFIG")


def _boom(*a, **k):
    raise AssertionError("reached past the confirmation gate: a network/auth "
                          "call ran before require_confirm fired")


class TestBlueskyRepost:
    def test_default_blocks_before_any_network_call(self, strict_publish, monkeypatch, capsys):
        mod = load_preset_module("bluesky", "repost", "br_")
        monkeypatch.setattr(mod, "get_handle", _boom)
        monkeypatch.setattr(mod, "get_session", _boom)
        with pytest.raises(SystemExit) as exc:
            mod.main("at://did:plc:x/app.bsky.feed.post/y")
        assert exc.value.code == 2
        assert "requires explicit confirmation" in capsys.readouterr().err

    def test_force_reaches_past_the_gate(self, strict_publish, monkeypatch):
        mod = load_preset_module("bluesky", "repost", "br_")
        monkeypatch.setattr(mod, "get_handle", lambda: (_ for _ in ()).throw(RuntimeError("past gate")))
        with pytest.raises(RuntimeError, match="past gate"):
            mod.main("at://did:plc:x/app.bsky.feed.post/y|force")
