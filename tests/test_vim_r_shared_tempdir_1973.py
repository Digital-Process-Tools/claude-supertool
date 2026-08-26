"""#1973 -- the vim `:r` fixtures that raced on the shared platform temp
root must build their source file under pytest's own `tmp_path`, not under
`tempfile.mkstemp()`.

Two tests -- one in `test_vim_r_read_diagnostic_1763.py`, one in this
repo's morning-mined `test_kevin_2026_05_17.py` -- built the file `:r`
reads via a bare `tempfile.mkstemp()`. That lands directly in the platform
temp root (`/tmp`, `/var/folders/.../T`), written and reaped by every
xdist worker at once, rather than under pytest's own per-test `tmp_path`.

This is a mitigation, not a diagnosis: it removes the tests' own exposure
to that shared root. It does NOT establish that the shared root was the
mechanism -- see the issue for what remains open. `mkstemp()` (unlike
`NamedTemporaryFile(delete=True)`) does not delete the file on close, so
the "closes and therefore deletes" hypothesis in the issue's own text does
not apply to either of these two tests; both explicitly `os.close()` then
leave the file in place for `op_vim` to read afterward.

Positive control included so this guard cannot pass vacuously by never
flagging anything.
"""
import re
from pathlib import Path

_ROOT = Path(__file__).parent.parent

_TARGETS = {
    "tests/test_vim_r_read_diagnostic_1763.py": "test_r_success_receipt_carries_no_diagnostic_noise",
    "tests/test_kevin_2026_05_17.py": "test_log_pattern_Nr_read_file_after_line",
}


def _function_body(source: str, name: str) -> str:
    lines = source.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"def {name}("))
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("def ") or lines[i].startswith("class "):
            end = i
            break
    return "\n".join(lines[start:end])


def _src_path_built_on_shared_tempdir(body: str) -> bool:
    """True if `src_path` -- the file this function's `:r` action reads --
    is created via a bare `tempfile.mkstemp()` rather than under `tmp_path`."""
    for line in body.splitlines():
        if re.match(r"\s*src_(?:fd,\s*)?src_path\s*=\s*tempfile\.mkstemp", line):
            return True
    return False


def test_detector_flags_the_old_shared_tempdir_shape():
    """Positive control: the shape #1973 actually hit must trip the
    detector, so it cannot pass this suite by never firing on anything."""
    old_shape = "\n".join([
        "def test_old_shape():",
        "    src_fd, src_path = tempfile.mkstemp(suffix='.txt', text=True)",
        "    os.close(src_fd)",
        "    r = st.op_vim(p, f':1r {src_path}')",
    ])
    assert _src_path_built_on_shared_tempdir(old_shape) is True


def test_r_fixtures_no_longer_use_the_shared_tempdir_for_their_source_file():
    for rel_path, func_name in _TARGETS.items():
        source = (_ROOT / rel_path).read_text(encoding="utf-8")
        body = _function_body(source, func_name)
        assert "tmp_path" in body, (
            f"{rel_path}::{func_name} must accept pytest's tmp_path fixture"
        )
        assert not _src_path_built_on_shared_tempdir(body), (
            f"{rel_path}::{func_name} still builds its :r source file via "
            "tempfile.mkstemp() on the shared platform temp root (#1973)"
        )
