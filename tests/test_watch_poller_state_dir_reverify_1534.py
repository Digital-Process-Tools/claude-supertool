"""A re-exec'd poller re-verifies the state directory it was handed (#1534).

`transport.poller_env` pins both derived paths into the child's environment
(#1477), so the child's `naming.resolve` saw an **explicit**
`SUPERTOOL_WATCH_STATE_DIR` and set `state_dir_is_derived` to `False`. Every
guard downstream of that flag — `claim_pidfile` and, since #1540, every
`write_state` — then returned without asking anything about the directory. The
parent verified once before the spawn and nothing re-established it after.

The flag conflated two facts. *The operator supplied this path* is theirs, and
manufacturing it would trade a loud refusal for a poller spawned into a
directory nobody asked for (#693). *This is the path the name derives* is ours
— the derivation is `state_dir_for(name)`, public and reproducible — and
whoever holds it should be checked, however the value arrived.

So the flag is now a question about the **value**, not about which variable
delivered it: the state directory equals what `SUPERTOOL_WATCH_NAME` derives.
That needs no marker to survive the exec and nothing new to forge — an
environment that sets the pair to disagreeing values is exactly the
operator-supplied case it was before. `state_dir_env_set` is the separate fact,
carried so `state_dir_provenance` can stop claiming a variable is unset in a
child where it demonstrably is.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for _dir in (str(REPO / "presets" / "watch"), str(REPO / "presets"), str(REPO / "tests")):
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import naming  # noqa: E402
from _changelog_findable import assert_change_is_findable  # noqa: E402

NAME = "oss1534"


def _poller_env_in_subprocess(env_extra: dict[str, str]) -> dict[str, str]:
    """What `transport.poller_env()` actually exports under an environment.

    Module constants are read at import, so the environment has to be in place
    before `transport` is imported — and the point of this file is the exec
    boundary, so the parent's export is asked for rather than reconstructed.
    """
    code = (
        "import sys, json;"
        f"sys.path[:0] = [{str(REPO / 'presets' / 'watch')!r}, {str(REPO / 'presets')!r}];"
        "import transport;"
        "print(json.dumps(transport.poller_env()))"
    )
    out = subprocess.run([sys.executable, "-c", code],
                         capture_output=True, text=True, check=True,
                         encoding="utf-8", errors="replace",
                         env={**os.environ, **env_extra}, cwd=str(REPO))
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_a_poller_exec_d_under_a_name_still_knows_the_directory_is_ours():
    """The whole issue, end to end through the real `poller_env`."""
    child_env = _poller_env_in_subprocess({
        naming.NAME_ENV: NAME,
        naming.SOCK_ENV: "",
        naming.STATE_DIR_ENV: "",
    })
    assert child_env[naming.STATE_DIR_ENV] == naming.state_dir_for(NAME), (
        "the fixture stopped exercising the derived path")
    child = naming.resolve(child_env)
    assert child.state_dir_is_derived, (
        "the child treats the path its parent derived as operator-supplied, so "
        "ensure_state_dir returns without checking anything")


def test_the_child_actually_re_establishes_the_directory(tmp_path):
    """`state_dir_is_derived` is only worth anything through this call."""
    target = tmp_path / "derived"
    child = naming.resolve({naming.NAME_ENV: NAME,
                            naming.STATE_DIR_ENV: naming.state_dir_for(NAME)})
    assert naming.ensure_state_dir(child, str(target)) == ""
    assert target.is_dir(), "nothing established the directory in the child"


def test_a_path_that_disagrees_with_the_name_is_still_the_operators(tmp_path):
    """#693's contract, unchanged: a supplied directory is never manufactured."""
    target = tmp_path / "supplied"
    r = naming.resolve({naming.NAME_ENV: NAME,
                        naming.STATE_DIR_ENV: str(target)})
    assert not r.state_dir_is_derived
    assert naming.ensure_state_dir(r, str(target)) == ""
    assert not target.exists()


def test_the_default_and_the_unnamed_channel_are_still_not_derived():
    assert not naming.resolve({}).state_dir_is_derived
    assert not naming.resolve(
        {naming.STATE_DIR_ENV: naming.DEFAULT_STATE_DIR}).state_dir_is_derived


def test_a_path_equal_to_the_derivation_is_not_reported_as_overriding_it():
    """The note read "poller slots are in X, not X" in every re-exec'd poller.

    A precedence note that fires when nothing was overridden is noise on the
    one surface this module exists to keep trustworthy.
    """
    r = naming.resolve({naming.NAME_ENV: NAME,
                        naming.STATE_DIR_ENV: naming.state_dir_for(NAME)})
    assert not [n for n in r.notes if naming.STATE_DIR_ENV in n], r.notes


def test_the_socket_note_has_the_same_shape_and_the_same_bug():
    """`poller_env` pins **both** halves, so the socket note read "the socket is
    X, not X" in the same processes. Reviewer-raised on the #1534 diff: the
    equality was applied to one of two identical branches."""
    r = naming.resolve({naming.NAME_ENV: NAME,
                        naming.SOCK_ENV: naming.sock_for(NAME)})
    assert not [n for n in r.notes if naming.SOCK_ENV in n], r.notes

    moved = naming.resolve({naming.NAME_ENV: NAME,
                            naming.SOCK_ENV: "/tmp/elsewhere.sock"})
    assert [n for n in moved.notes if naming.SOCK_ENV in n], (
        "a socket that really was moved must still say so")


def test_provenance_does_not_claim_an_unset_variable_in_a_child_that_set_it():
    """`state_dir_provenance` fed the operator-facing refusal in
    `dispatcher.py`. Under the old flag a re-exec'd poller sent them to
    `SUPERTOOL_WATCH_STATE_DIR`; under a naive fix it would tell them the
    variable is not set while its own environment sets it."""
    child = naming.resolve({naming.NAME_ENV: NAME,
                            naming.STATE_DIR_ENV: naming.state_dir_for(NAME)})
    why = naming.state_dir_provenance(child)
    assert naming.NAME_ENV in why, why
    assert "is not set" not in why, why

    parent = naming.resolve({naming.NAME_ENV: NAME})
    assert naming.state_dir_provenance(parent) == (
        f"{naming.STATE_DIR_ENV} is not set — this directory was derived from "
        f"{naming.NAME_ENV}={NAME}")


def test_state_dir_env_set_is_the_fact_the_flag_used_to_carry():
    assert not naming.resolve({naming.NAME_ENV: NAME}).state_dir_env_set
    assert naming.resolve(
        {naming.NAME_ENV: NAME,
         naming.STATE_DIR_ENV: naming.state_dir_for(NAME)}).state_dir_env_set
    assert not naming.resolve({naming.STATE_DIR_ENV: ""}).state_dir_env_set


def test_the_change_is_documented():
    assert_change_is_findable(1534)
