"""#2121 -- doctor's PATH check must compare *versions*, not just paths.

`_doctor_symlink()` used to flag any PATH entry whose realpath differs from
the running module as a possible stale symlink -- even a launcher script
that resolves the newest install at run time and answers the same version
(#2071's own documented remedy: a symlink pinned into a version directory
is the trap, a launcher that re-resolves is the fix). The predicate compared
*paths*, so the fix for #2071 necessarily fails it while being exactly as
current as the module answering the call.

The fix asks the cheaper question directly: run the PATH entry with
`version` and compare what it answers against `VERSION`. Three states, not
two -- current (agrees, no note), stale (disagrees, note names the version
it answered), unknown (could not run or parse, never folded into either).
"""
from __future__ import annotations

import supertool


def _fake_version_proc(output, returncode=0):
    class _Result:
        pass
    r = _Result()
    r.returncode = returncode
    r.stdout = output
    r.stderr = ""
    return r


def test_launcher_at_current_version_gets_no_stale_note(monkeypatch, tmp_path) -> None:
    """Must-fire: a PATH entry that is not a symlink, resolves to a
    different path than the running module, but answers the same version --
    no NOTE about a possible stale build.
    """
    fake_which = str(tmp_path / "supertool")
    monkeypatch.setattr(supertool.shutil, "which", lambda name: fake_which)
    monkeypatch.setattr(supertool.os.path, "islink", lambda p: False)

    def _fake_run(cmd, **kwargs):
        if cmd[0] == fake_which:
            assert cmd[1] == "version"
            return _fake_version_proc(f"supertool {supertool.VERSION}\n")
        return _fake_version_proc("", returncode=1)
    monkeypatch.setattr(supertool.subprocess, "run", _fake_run)

    sym = supertool._doctor_symlink()
    assert sym["path_resolves_to_running_module"] is False
    assert sym.get("path_version_state") == "current"

    out = supertool.op_doctor("")
    assert "stale build" not in out


def test_genuinely_stale_path_entry_still_warns_with_its_version(monkeypatch, tmp_path) -> None:
    """Control: a PATH entry that really does answer an older version must
    still be flagged, and the note must name the version it answered as --
    a fix that just deletes the branch passes the test above and fails
    this one.
    """
    fake_which = str(tmp_path / "supertool")
    monkeypatch.setattr(supertool.shutil, "which", lambda name: fake_which)
    monkeypatch.setattr(supertool.os.path, "islink", lambda p: False)

    def _fake_run(cmd, **kwargs):
        return _fake_version_proc("supertool 0.1.0\n")
    monkeypatch.setattr(supertool.subprocess, "run", _fake_run)

    sym = supertool._doctor_symlink()
    assert sym["path_resolves_to_running_module"] is False
    assert sym.get("path_version_state") == "stale"
    assert sym.get("path_version") == "0.1.0"

    out = supertool.op_doctor("")
    assert "0.1.0" in out
    assert "NOTE" in out


def test_path_entry_that_cannot_be_run_is_reported_as_unknown(monkeypatch, tmp_path) -> None:
    """A PATH entry that fails to run, or returns output the version line
    cannot be parsed from, must land on `unknown` -- never folded into
    `current` (which would mask a genuinely stale build) or `stale` (which
    would false-alarm a healthy one).
    """
    fake_which = str(tmp_path / "supertool")
    monkeypatch.setattr(supertool.shutil, "which", lambda name: fake_which)
    monkeypatch.setattr(supertool.os.path, "islink", lambda p: False)

    def _boom(cmd, **kwargs):
        raise OSError("no such file")
    monkeypatch.setattr(supertool.subprocess, "run", _boom)

    sym = supertool._doctor_symlink()
    assert sym["path_resolves_to_running_module"] is False
    assert sym.get("path_version_state") == "unknown"

    out = supertool.op_doctor("")
    assert "could not tell" in out.lower() or "unknown" in out.lower()
