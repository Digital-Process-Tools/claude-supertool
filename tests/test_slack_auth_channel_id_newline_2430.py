"""slack_authorization -- a config channel_id must not forge an extra row (#2430).

`presets/slack/auth.py::_list_all` prints `f"{channel_id}: ..."` at column 0
with no flattening. `channel_id` is a key read straight out of
`~/.config/supertool/slack_authorization.json` -- an out-of-repo config file,
but the repo already ruled on this exact rendering shape elsewhere in-tree
(`_supertool.py:3363` flattens an `"ops"`/`"aliases"` config key through
`_flat_field(..., disclose_newline=True)` for the identical reason). A key
carrying an embedded newline lets one JSON entry render as two physical
lines, the second of which is indistinguishable from a genuine channel row.

Positive control: `test_two_plain_channels_still_render_as_two_rows` pins
that a fix here must not collapse two *real* channels onto one line -- only
an embedded newline *inside* a single key should be flattened.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


auth_op = _load("presets/slack/auth.py", "slack_auth_op_2430")


def _write_config(tmp_path: Path, data: dict) -> Path:
    import json
    home = tmp_path / "home"
    (home / ".config" / "supertool").mkdir(parents=True, exist_ok=True)
    (home / ".config" / "supertool" / "slack_authorization.json").write_text(
        json.dumps(data), encoding="utf-8")
    return home


def _set_home(monkeypatch, path) -> None:
    """Point `~` at `path` on every platform `expanduser` supports.

    Same helper as `tests/test_slack_authorization_op_argv_2035.py::_set_home`
    -- POSIX reads HOME; Windows prefers USERPROFILE, checked BEFORE HOME.
    """
    import os as _os
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("USERPROFILE", str(path))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)
    assert Path(_os.path.expanduser("~")) == Path(str(path)), (
        "expanduser('~') did not resolve to the fake home on this "
        "platform -- the whole point of this helper"
    )


def test_newline_in_channel_id_cannot_forge_an_extra_row(
    monkeypatch, tmp_path, capsys,
) -> None:
    """A channel_id key with an embedded newline must render as ONE row, not
    two physical lines -- the second of which would be an unflagged forged
    row at column 0, indistinguishable from a real channel."""
    home = _write_config(tmp_path, {"channels": {
        "C_REAL\nC_FORGED": {"level": "context"},
    }})
    _set_home(monkeypatch, str(home))
    monkeypatch.chdir(tmp_path)

    rc = auth_op.main([])
    out = capsys.readouterr().out

    assert rc == 0
    lines = out.splitlines()
    row_lines = [ln for ln in lines if "heard=" in ln]
    assert len(row_lines) == 1, (
        f"expected exactly one rendered channel row, got {row_lines!r} "
        f"in full output {out!r}"
    )
    # the header line plus exactly one row -- a raw newline in the key would
    # add a THIRD physical line ("C_FORGED: context (heard=...) ...") that
    # looks like its own complete, legitimate row.
    assert len(lines) == 2, f"expected 2 physical lines, got {lines!r}"
    assert "C_REAL" in row_lines[0] and "C_FORGED" in row_lines[0]


def test_two_plain_channels_still_render_as_two_rows(
    monkeypatch, tmp_path, capsys,
) -> None:
    """Positive control: two genuinely distinct channels must still render
    as two separate rows -- the fix must not over-collapse."""
    home = _write_config(tmp_path, {"channels": {
        "C_A": {"level": "context"}, "C_B": {"level": "off"},
    }})
    _set_home(monkeypatch, str(home))
    monkeypatch.chdir(tmp_path)

    rc = auth_op.main([])
    out = capsys.readouterr().out

    assert rc == 0
    row_lines = [ln for ln in out.splitlines() if "heard=" in ln]
    assert len(row_lines) == 2, f"expected 2 rows, got {row_lines!r}"
