"""Regression tests for #691 T4: `require_confirm` wired into every publish op.

`_publish_safety.require_confirm` blocks single-shot LLM publishing unless
explicitly bypassed (`|force`, `SUPERTOOL_NO_PUBLISH_CONFIRM=1`, or
`.supertool.json`'s `no_publish_confirm`). `devto_publish` and
`bluesky_publish` called it; six siblings did not --
`devto_comment`, `bluesky_follow`, `bluesky_like`, `hashnode_publish`,
`hashnode_comment` and `hashnode_react` each had their own duplicate-check
`|force` gate but no confirmation gate at all, so a caller with no opt-out
set could still act once with no confirmation required.

Each op module here is loaded with `load_preset_module` and its `main()` is
invoked with every downstream call mocked to raise `AssertionError` --
proving the confirmation gate fires (`SystemExit(2)`, "requires explicit
confirmation") strictly before any network call, not merely somewhere in
the function. `force=True` on the same call must reach past the gate (and
then may fail for an unrelated reason -- these ops need real credentials --
which is why the force-side assertions only check that the confirm-gate
error text is ABSENT, never that the call fully succeeds).
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
    """Force strict confirmation mode, isolated from this checkout's own cwd.

    Mirrors `tests/test_security_publish_149.py::strict_publish`: both env
    and cwd feed `require_confirm` (`.supertool.json` is found by walking up
    from cwd), so both must be isolated or "strict" silently reads whatever
    this checkout's own config says.
    """
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


class TestDevtoComment:
    def test_default_blocks_before_any_network_call(self, strict_publish, monkeypatch, capsys):
        mod = load_preset_module("devto", "comment", "dc_")
        monkeypatch.setattr(mod, "resolve_article_id", _boom)
        monkeypatch.setattr(mod, "get_session_cookie", _boom)
        with pytest.raises(SystemExit) as exc:
            mod.main("123|hello")
        assert exc.value.code == 2
        assert "requires explicit confirmation" in capsys.readouterr().err

    def test_force_reaches_past_the_gate(self, strict_publish, monkeypatch):
        mod = load_preset_module("devto", "comment", "dc_")
        monkeypatch.setattr(mod, "resolve_article_id", lambda raw: (_ for _ in ()).throw(RuntimeError("past gate")))
        with pytest.raises(RuntimeError, match="past gate"):
            mod.main("123|hello||force")


class TestBlueskyFollow:
    def test_default_blocks_before_any_network_call(self, strict_publish, monkeypatch, capsys):
        mod = load_preset_module("bluesky", "follow", "bf_")
        monkeypatch.setattr(mod, "get_handle", _boom)
        monkeypatch.setattr(mod, "get_session", _boom)
        with pytest.raises(SystemExit) as exc:
            mod.main("alice.bsky.social")
        assert exc.value.code == 2
        assert "requires explicit confirmation" in capsys.readouterr().err

    def test_force_reaches_past_the_gate(self, strict_publish, monkeypatch):
        mod = load_preset_module("bluesky", "follow", "bf_")
        monkeypatch.setattr(mod, "get_handle", lambda: (_ for _ in ()).throw(RuntimeError("past gate")))
        with pytest.raises(RuntimeError, match="past gate"):
            mod.main("alice.bsky.social|force")


class TestBlueskyLike:
    def test_default_blocks_before_any_network_call(self, strict_publish, monkeypatch, capsys):
        mod = load_preset_module("bluesky", "like", "bl_")
        monkeypatch.setattr(mod, "get_handle", _boom)
        monkeypatch.setattr(mod, "get_session", _boom)
        with pytest.raises(SystemExit) as exc:
            mod.main("at://did:plc:x/app.bsky.feed.post/y")
        assert exc.value.code == 2
        assert "requires explicit confirmation" in capsys.readouterr().err

    def test_force_reaches_past_the_gate(self, strict_publish, monkeypatch):
        mod = load_preset_module("bluesky", "like", "bl_")
        monkeypatch.setattr(mod, "get_handle", lambda: (_ for _ in ()).throw(RuntimeError("past gate")))
        with pytest.raises(RuntimeError, match="past gate"):
            mod.main("at://did:plc:x/app.bsky.feed.post/y|force")


class TestHashnodePublish:
    def test_default_blocks_before_any_network_call(self, strict_publish, monkeypatch, capsys, tmp_path):
        mod = load_preset_module("hashnode", "publish", "hp_")
        body = tmp_path / "drafts"
        body.mkdir()
        f = body / "post.md"
        f.write_text("hello")
        monkeypatch.setattr(mod, "get_token", _boom)
        with pytest.raises(SystemExit) as exc:
            mod.main(f"T|{f}|https://x.io")
        assert exc.value.code == 2
        assert "requires explicit confirmation" in capsys.readouterr().err

    def test_force_reaches_past_the_gate(self, strict_publish, monkeypatch, tmp_path):
        mod = load_preset_module("hashnode", "publish", "hp_")
        body = tmp_path / "drafts"
        body.mkdir()
        f = body / "post.md"
        f.write_text("hello")
        monkeypatch.setattr(mod, "get_token", lambda: (_ for _ in ()).throw(RuntimeError("past gate")))
        with pytest.raises(RuntimeError, match="past gate"):
            mod.main(f"T|{f}|https://x.io|||force")


class TestHashnodeComment:
    def test_default_blocks_before_any_network_call(self, strict_publish, monkeypatch, capsys):
        mod = load_preset_module("hashnode", "comment", "hc_")
        monkeypatch.setattr(mod, "get_token", _boom)
        with pytest.raises(SystemExit) as exc:
            mod.main("post-id|hello")
        assert exc.value.code == 2
        assert "requires explicit confirmation" in capsys.readouterr().err

    def test_force_reaches_past_the_gate(self, strict_publish, monkeypatch):
        mod = load_preset_module("hashnode", "comment", "hc_")
        monkeypatch.setattr(mod, "get_token", lambda: (_ for _ in ()).throw(RuntimeError("past gate")))
        with pytest.raises(RuntimeError, match="past gate"):
            mod.main("post-id|hello|force")


class TestHashnodeReact:
    def test_default_blocks_before_any_network_call(self, strict_publish, monkeypatch, capsys):
        mod = load_preset_module("hashnode", "react", "hr_")
        monkeypatch.setattr(mod, "get_token", _boom)
        with pytest.raises(SystemExit) as exc:
            mod.main("post-id")
        assert exc.value.code == 2
        assert "requires explicit confirmation" in capsys.readouterr().err

    def test_force_reaches_past_the_gate(self, strict_publish, monkeypatch):
        mod = load_preset_module("hashnode", "react", "hr_")
        monkeypatch.setattr(mod, "get_token", lambda: (_ for _ in ()).throw(RuntimeError("past gate")))
        with pytest.raises(RuntimeError, match="past gate"):
            mod.main("post-id|force")
