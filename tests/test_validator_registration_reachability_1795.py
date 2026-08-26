"""Registration and reachability guards for `validators/` adapters (#1795).

Two failures, same shape, and neither is a bug in the adapter:

1. **Registered nowhere.** `yaml-check` shipped with an adapter, tests and an
   `absent`-tool third state (#1202) and was never wired into any
   `.supertool.json` -- not this repo's, not the example template users copy.
2. **Registered in the example, dead in this repo.** The example template is
   not what runs here. A validator can be present, correct, tested, listed in
   the example config users copy from -- and still never fire on a single
   edit in *this* checkout, which is exactly the gap #1795's comment
   describes: "A validator can be present, correct, tested, and dead."
   `actionlint` (#1798 / PR #1904) shipped this same way: adapter shipped,
   registered in `.supertool.example.json`, never in this repo's own config.

`test_every_adapter_is_registered_in_at_least_one_config` catches the first
shape for every adapter under `validators/`, forever, without needing this
file edited when a new one ships. It does not catch the second shape on its
own -- a directory-vs-example diff is green the whole time yaml-check and
actionlint are unreachable in this repo, because the example already lists
both (this is the trap: "a test that accepts the example config alone would
have passed while both defects above were live").

The second shape needs a config that knows which files this repo actually
edits, which a directory listing cannot derive by itself: half of
`validators/` (phplint, phpstan, gofmt-check, cargo-check, ruby-check,
terraform-check, ...) targets a language this repo has no real source in --
demanding local registration for those would be wrong, and probed against
this repo's own `git ls-files` output at review time, several of the
generic-looking ones turn out to be repo-specific choices rather than gaps:
`gitleaks` ('*'), `pyright`/`py-compile` (redundant with `ruff`, which is
registered) and `phplint`/`psr`/`phpstan` (matching only a test fixture
`.class.php`) are all deliberately not wired locally, and a blanket
"file exists -> must be registered" rule flagged all of them plus
`markdownlint` (82 real `.md` files, still unregistered) as false positives
when this was checked by hand. That markdownlint gap is real and is #1795's
own class again, but it is a third instance the issue text does not ask this
lane to close -- see the pull request body.

So the second shape is pinned by name, against this repo's real
`.supertool.json` via the `shipped_config` fixture and the same
`_applicable_validators` the core itself calls before every mutating op --
not a hand-rolled re-implementation of match-glob logic that could drift from
the thing it is meant to guard.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import supertool

REPO = Path(__file__).resolve().parent.parent
VALIDATORS_DIR = REPO / "validators"

#: Directories under validators/ that are not adapters and never will be.
_NOT_ADAPTERS = {"common"}


def _adapter_dirs() -> "list[str] | None":
    """Every adapter directory name, or None if the listing itself failed.

    Three states for the caller: a populated list is ok, an empty list from
    a real directory is a finding (nothing shipped, which would be strange
    but is not "could not check"), and None means the directory could not be
    enumerated at all -- a state the caller must refuse to read as clean.
    """
    if not VALIDATORS_DIR.is_dir():
        return None
    try:
        return sorted(
            p.name for p in VALIDATORS_DIR.iterdir()
            if p.is_dir() and p.name not in _NOT_ADAPTERS
            and not p.name.startswith("_")
        )
    except OSError:
        return None


def _registered_adapter_names(config_path: Path) -> "set[str] | None":
    """Adapter directory names referenced by a `cmd` in *config_path*.

    None (not an empty set) when the config could not be read or parsed --
    the three-state rule this repo's own CLAUDE.md asks every checker to
    honour: a config that could not be read must never render as a config
    with zero validators.
    """
    if not config_path.is_file():
        return None
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    names: "set[str]" = set()
    for spec in (cfg.get("validators") or {}).values():
        if not isinstance(spec, dict):
            continue
        m = re.search(r"validators/([^/{}]+)/", spec.get("cmd", ""))
        if m:
            names.add(m.group(1))
    return names


# ---------------------------------------------------------------------------
# Shape 1: dead adapter -- reachable from *no* config at all
# ---------------------------------------------------------------------------

def test_every_adapter_is_registered_in_at_least_one_config() -> None:
    """An adapter under `validators/` must appear in `.supertool.json` or
    `.supertool.example.json` -- reachable from at least one config, the bar
    #1795's comment names. Neither present means the exact failure mode this
    issue describes: shipped, tested, never wired anywhere.
    """
    adapters = _adapter_dirs()
    assert adapters is not None, "could-not-check: validators/ is not a readable directory"
    assert adapters, "could-not-check: validators/ contains no adapter directories"

    local = _registered_adapter_names(REPO / ".supertool.json")
    example = _registered_adapter_names(REPO / ".supertool.example.json")
    assert local is not None, "could-not-check: .supertool.json could not be read/parsed"
    assert example is not None, "could-not-check: .supertool.example.json could not be read/parsed"

    reachable = local | example
    unreachable = sorted(set(adapters) - reachable)
    assert not unreachable, (
        "adapters shipped under validators/ but registered in neither "
        f".supertool.json nor .supertool.example.json: {unreachable}"
    )


def test_a_genuinely_unregistered_adapter_is_caught(tmp_path: Path, monkeypatch) -> None:
    """Positive control for the assertion above (CLAUDE.md: pair every "must
    not fire" with a "must fire" in the same fixture). Without this, a
    harness that silently found nothing (an empty adapters list, a config
    that failed to parse and read as "no names") would pass the guard above
    for the wrong reason.
    """
    fake_validators = tmp_path / "validators"
    (fake_validators / "ghost-lint").mkdir(parents=True)
    (fake_validators / "ghost-lint" / "ghost-lint.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(__import__("sys").modules[__name__], "VALIDATORS_DIR", fake_validators)
    adapters = _adapter_dirs()
    assert adapters == ["ghost-lint"]

    empty_cfg = tmp_path / "empty.json"
    empty_cfg.write_text(json.dumps({"validators": {}}), encoding="utf-8")
    registered = _registered_adapter_names(empty_cfg)
    assert registered == set()
    assert set(adapters) - registered == {"ghost-lint"}


def test_an_unreadable_config_is_could_not_check_not_a_pass(tmp_path: Path) -> None:
    """The three-state rule, pinned: garbage JSON must not read as zero
    validators, which would let a corrupt config sail through the guard
    above as if every adapter were reachable-from-the-other-file.
    """
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert _registered_adapter_names(broken) is None


def test_a_missing_config_is_could_not_check_not_a_pass(tmp_path: Path) -> None:
    assert _registered_adapter_names(tmp_path / "does-not-exist.json") is None


# ---------------------------------------------------------------------------
# Shape 2: registered in the example, dead in *this* repo -- pinned by name
# ---------------------------------------------------------------------------

def test_yaml_check_fires_on_a_yml_edit_in_this_repo(shipped_config) -> None:
    applicable = supertool._applicable_validators("edit", ".github/workflows/tests.yml")
    assert "yaml-check" in applicable, (
        "yaml-check ships, is tested, and must fire on a .yml edit in this "
        f"repo's own config -- got {sorted(applicable)}"
    )


def test_yaml_check_yaml_fires_on_a_dot_yaml_edit_in_this_repo(shipped_config) -> None:
    applicable = supertool._applicable_validators("edit", "some/thing.yaml")
    assert any(n.startswith("yaml-check") for n in applicable), (
        f"no yaml-check-family validator fired on a .yaml edit -- got {sorted(applicable)}"
    )


def test_actionlint_fires_on_a_workflow_edit_in_this_repo(shipped_config) -> None:
    applicable = supertool._applicable_validators("edit", ".github/workflows/tests.yml")
    assert "actionlint" in applicable, (
        f"actionlint must fire on a .github/workflows/*.yml edit -- got {sorted(applicable)}"
    )


def test_actionlint_does_not_fire_on_a_non_workflow_yml(shipped_config) -> None:
    """Positive control for the actionlint match scope: a plain repo-root
    .yml file is not a GitHub Actions workflow, and actionlint's own match
    (`.github/workflows/*.{yml,yaml}`) says so -- this must stay true, or the
    "must fire" assertion above is meaningless (it could be firing on
    everything).
    """
    applicable = supertool._applicable_validators("edit", ".gitlab-ci.yml")
    assert "actionlint" not in applicable, (
        f"actionlint fired outside .github/workflows/ -- got {sorted(applicable)}"
    )
    assert "yaml-check" in applicable, (
        "yaml-check should still fire on this .yml file even though "
        f"actionlint should not -- got {sorted(applicable)}"
    )
