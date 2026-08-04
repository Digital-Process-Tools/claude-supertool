"""desktop_notify must hand its text to osascript as arguments, not as script text.

Notification titles and bodies come from remote repos — MR/PR titles, comment
bodies, author names — so they routinely contain quotes, backslashes and other
characters that are syntax to AppleScript. Interpolating them into the script
makes the notification's behaviour depend on the content of somebody else's
branch name, which is neither correct nor predictable.

`osascript` reads positional arguments after `--` into `argv`, where they are
values rather than source. These tests assert that property directly: the text
appears only as a trailing argument and never inside an `-e` script. Asserting
"it did not raise" would not distinguish the two shapes, since the interpolating
version does not raise either.
"""

import subprocess

import presets.watch.transport as transport

_AWKWARD = 'release "v2" & friends'
_TRAILING_BACKSLASH = 'branch name ending in a slash\\'


def _run_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(transport.sys, "platform", "darwin")
    monkeypatch.setattr(transport.shutil, "which", lambda _n: "/usr/bin/osascript")

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(transport.subprocess, "run", fake_run)
    return calls


def _e_scripts(argv):
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-e" and i + 1 < len(argv)]


def test_message_text_is_an_argument_not_script_text(monkeypatch):
    calls = _run_calls(monkeypatch)
    transport.desktop_notify(_AWKWARD, _AWKWARD)

    assert len(calls) == 1, "expected exactly one osascript invocation"
    argv = calls[0]

    scripts = _e_scripts(argv)
    assert scripts, "osascript must be driven by -e scripts"
    for script in scripts:
        assert _AWKWARD not in script, "remote text must not be compiled as AppleScript"

    assert "--" in argv, "values must be passed positionally after a -- separator"
    tail = argv[argv.index("--") + 1:]
    assert _AWKWARD in tail, "the text must survive intact as a positional argument"


def test_trailing_backslash_stays_a_value(monkeypatch):
    calls = _run_calls(monkeypatch)
    transport.desktop_notify(_TRAILING_BACKSLASH, "body")
    argv = calls[0]
    for script in _e_scripts(argv):
        assert _TRAILING_BACKSLASH not in script
    assert _TRAILING_BACKSLASH in argv[argv.index("--") + 1:]


def test_still_noop_off_macos(monkeypatch):
    monkeypatch.setattr(transport.sys, "platform", "linux")
    ran = []
    monkeypatch.setattr(transport.subprocess, "run", lambda *a, **k: ran.append(a))
    transport.desktop_notify("t", "m")
    assert ran == [], "must not shell out on non-macOS"
