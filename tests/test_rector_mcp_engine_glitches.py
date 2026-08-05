"""rector-mcp adapter: config-driven engine-glitch suppression.

Non-deterministic warm-daemon glitches (PHP fatals / engine-state corruption
inside rector itself) are NOT findings about the edited file. The adapter drops
them at the source so they never surface as a red or reach the validator cache.
Signatures are a config prop in .supertool.json (validators.rector.engine_glitches)
that the adapter reads directly — no env, the generic core stays oblivious.
Built-in defaults are the safety net when the prop is absent.
"""
import importlib.util
import json
from pathlib import Path

ADAPTER = Path(__file__).resolve().parent.parent / "validators" / "rector-mcp" / "rector-mcp.py"
DEFAULTS = ["System error:", "toMutatingScope() on null"]


def _load(working_dir):
    spec = importlib.util.spec_from_file_location("rector_mcp_adapter", ADAPTER)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.WORKING_DIR = str(working_dir)
    return m


def _write_config(tmp_path, rector_entry):
    cfg = {"validators": {"rector": rector_entry}}
    (tmp_path / ".supertool.json").write_text(json.dumps(cfg))


class TestEngineGlitchSignatures:
    def test_default_when_prop_absent(self, tmp_path):
        _write_config(tmp_path, {})
        m = _load(tmp_path)
        assert m.engine_glitch_signatures() == DEFAULTS

    def test_default_when_no_config_file(self, tmp_path):
        m = _load(tmp_path)  # no .supertool.json written
        assert m.engine_glitch_signatures() == DEFAULTS

    def test_prop_overrides_and_strips(self, tmp_path):
        _write_config(tmp_path, {"engine_glitches": ["foo", "  bar baz  ", ""]})
        m = _load(tmp_path)
        assert m.engine_glitch_signatures() == ["foo", "bar baz"]

    def test_empty_list_disables_suppression(self, tmp_path):
        _write_config(tmp_path, {"engine_glitches": []})
        m = _load(tmp_path)
        assert m.engine_glitch_signatures() == []
        assert not m.is_engine_glitch("System error: anything")

    def test_is_engine_glitch_substring_match(self, tmp_path):
        _write_config(tmp_path, {})
        m = _load(tmp_path)
        assert m.is_engine_glitch('Call to a member function toMutatingScope() on null')
        assert m.is_engine_glitch('System error: "ClassReflection must be resolved for class XTest"')
        assert not m.is_engine_glitch('Unused parameter $x')
        assert not m.is_engine_glitch('')

    def test_malformed_json_falls_back_to_defaults(self, tmp_path):
        (tmp_path / ".supertool.json").write_text("{ this is not valid json ")
        m = _load(tmp_path)
        assert m.engine_glitch_signatures() == DEFAULTS

    def test_non_list_prop_falls_back_to_defaults(self, tmp_path):
        _write_config(tmp_path, {"engine_glitches": "System error:"})
        m = _load(tmp_path)
        assert m.engine_glitch_signatures() == DEFAULTS


class TestFormatResponseDropsGlitch:
    def _resp(self, message):
        rector_json = {"file_diffs": [], "errors": [{"message": message}]}
        return {"result": {"structuredContent": {"exit_code": 0, "output": json.dumps(rector_json)}}}

    def test_glitch_error_dropped(self, tmp_path):
        m = _load(tmp_path)
        out = m.format_response("Foo.php", self._resp("Call to a member function toMutatingScope() on null"), 5)
        assert out["ok"] is True
        assert out["count"] == 0
        assert out["errors"] == []

    def test_system_error_glitch_dropped(self, tmp_path):
        m = _load(tmp_path)
        out = m.format_response("Foo.php", self._resp('System error: "ClassReflection must be resolved"'), 5)
        assert out["ok"] is True
        assert out["count"] == 0

    def test_real_error_surfaces(self, tmp_path):
        m = _load(tmp_path)
        out = m.format_response("Foo.php", self._resp("Some genuine rector parse error"), 5)
        assert out["ok"] is False
        assert out["count"] == 1
        assert "genuine rector" in out["errors"][0]["msg"]
