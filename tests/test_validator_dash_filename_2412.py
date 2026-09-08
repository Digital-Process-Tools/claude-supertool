"""17 validator adapters pass a flag-shaped filename to subprocess with no
separator between the tool's own flags and the target (#2412, #2418, #2438).

A contributor can add a file named `-o=payload` or `--eval=x` to a pull
request; this repo's own post-write validators then run on exactly that
diff. If the underlying tool's argv parser reads a leading `-`/`--` as an
option rather than a bare positional, a crafted filename changes what the
validator does rather than what file it lints — the same class #1040
already tracks for `repo:` accepting a leading `-` into `gh repo view`.

Each of the 15 adapters below was checked against its own CLI behaviour
(`--help`, or a local `--` smoke test against the real installed binary
where available) rather than assumed to support `--` universally:

  observed (`--` honoured, real binary): bash, gofmt, php, php phpstan
    (via symfony/console), node --check, prettier, shellcheck, ruby,
    markdownlint, glab (ci-lint) — each was run twice against a filename
    starting with `-`, once with `--` and once without, and the
    without-`--` run always misparsed the filename as an option (bash:
    "invalid option"; gofmt: "flag provided but not defined"; php: dumped
    `phpinfo()` -- the flag-combination `-w -e -i -r -d` triggered `-i`;
    ruby: silently changed behaviour; node: "bad option"; shellcheck:
    "unrecognized option"; markdownlint/glab: "unknown option"/"unknown
    shorthand flag") — WITH `--` every one of those same runs treated the
    filename as an ordinary positional path.

  reasoned (docs, no local binary to test against): none in the `--`
    group below — see `contained` group.

  observed NOT to honour `--` (real binary): xmllint — `xmllint --noout
    --nonet --noent -- -weird.xml` itself errors `Unknown option --`; the
    separator is read as a bare option rather than a terminator.

  reasoned NOT confirmed to honour `--` (no local binary; routed through
    containment rather than assumed): eslint, hadolint, stylelint,
    terraform-check, phpmd, psr. Each is routed through the same
    `contained_target()` shape `validators/pyright/pyright.py` (#2379) and
    `validators/tsc-check/tsc-check.py` (#1519) already use for pyright/tsc
    — containment does not depend on the tool understanding `--` at all,
    so it is the safe default for a tool this pass could not verify.
    phpmd and psr (#2412's own sweep missed both, filed as #2438) were
    both unmeasured against a real binary in this pass too: phpmd's own
    argv shape is positional (`<file> <format> <ruleset>`, file first,
    not last) rather than GNU-getopt-style, so a `--` separator is not
    known to be understood the way it is for the tools in the observed
    group above; psr (phpcs) was likewise never measured. Containment
    avoids depending on either answer.

This file is the shared pattern #2412 asks for: table-driven, so a 20th
adapter reusing the same shape is one row, not a new test file. It never
runs the real external tool — `subprocess.run` is monkeypatched to capture
argv and return a benign, unparsed result, and `shutil.which` is
monkeypatched to report every tool as present — so it runs on every CI leg
regardless of which linters happen to be installed there, the same
constraint the pytest matrix (`.github/workflows/tests.yml`) already
imposes: only `markdownlint-cli`, `pyright` and `mypy` are ever installed
on any leg (the `coverage_gate` job), and eslint/hadolint/stylelint
/terraform/glab are never installed anywhere in this repo's CI. A test
gated on the real binary would silently skip on every leg for those five
and prove nothing; this harness proves the adapter's own argv construction
instead, which is stable regardless of whether the real tool is present.

Each case pairs a "must not fire" assertion (the crafted name is not sent
as a bare option-shaped argument) with a "must fire" one (an ordinary
filename still reaches the tool unmolested) in the same fixture, so a
harness that saw nothing at all cannot pass either half by accident.
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
VALIDATORS = REPO / "validators"

#: (slug, relative adapter path, route). route is "separator" when the
#: adapter now inserts a bare `--` immediately before the target, or
#: "contained" when it instead spells the target so it cannot start with
#: `-` (`contained_target()`/`_contained_target()`).
CASES = [
    ("bash-check", "bash-check/bash-check.py", "separator"),
    ("ci-lint", "ci-lint/ci-lint.py", "separator"),
    ("gofmt-check", "gofmt-check/gofmt-check.py", "separator"),
    ("markdownlint", "markdownlint/markdownlint.py", "separator"),
    # NOT "separator": `php -l --` was measured, after this PR's CI first
    # failed, to silently discard `--` and fall back to reading php's OWN
    # stdin instead of the file -- see contained_target()'s docstring in
    # phplint.py for the reproduction (a closed local stdin made the
    # original `--` fix look green by returning instantly on empty
    # stdin; CI's own open stdin hung for the full 30s timeout instead).
    ("phplint", "phplint/phplint.py", "contained"),
    ("phpstan", "phpstan/phpstan.py", "separator"),
    ("node-check", "node-check/node-check.py", "separator"),
    # Two subprocess.run calls with two different fixes: `--check` gets a
    # `--` (a boolean flag, no value of its own to lose); `--file-info`
    # gets containment instead, because it consumes its target as ITS OWN
    # option value rather than a plain positional -- `contained_target()`'s
    # own docstring in `prettier-check.py` has the measured reasoning.
    # `--check` runs first in `main()`, `--file-info` second (only once
    # `--check` exits 0), so the routes below are in that call order.
    ("prettier-check", "prettier-check/prettier-check.py",
     ("separator", "contained")),
    ("shellcheck", "shellcheck/shellcheck.py", "separator"),
    ("ruby-check", "ruby-check/ruby-check.py", "separator"),
    ("eslint", "eslint/eslint.py", "contained"),
    ("hadolint", "hadolint/hadolint.py", "contained"),
    ("stylelint", "stylelint/stylelint.py", "contained"),
    ("terraform-check", "terraform-check/terraform-check.py", "contained"),
    ("xmllint", "xmllint/xmllint.py", "contained"),
    ("phpmd", "phpmd/phpmd.py", "contained"),
    ("psr", "psr/psr.py", "contained"),
]

#: Crafted names a real CLI's own flag parser reads as an option rather
#: than a path -- the "must not fire" half of every case below.
DASH_NAMES = ["-o=payload", "--eval=x"]

#: An ordinary filename -- the "must fire" positive control paired with
#: every dash-named case, so a harness that captured nothing (a broken
#: fixture, a mocked call nothing ever reached) cannot pass by silence.
PLAIN_NAME = "ordinary.txt"


def _load(adapter: Path, unique: str):
    spec = importlib.util.spec_from_file_location(unique, adapter)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _spawn_calls(monkeypatch, tmp_path: Path, adapter: Path, unique: str,
                  name: str) -> list:
    """Every argv the adapter handed `subprocess.run`, for a target `name`.

    `shutil.which` is patched to report every tool present -- the adapter's
    OWN existence gate is not what this test is about -- and
    `subprocess.run` is captured rather than let through, so this never
    depends on any of the 15 real tools being installed. Whatever `main()`
    does with the canned reply afterwards (JSON it cannot parse, a shape it
    does not expect) is swallowed: the argv this test inspects was already
    built and sent before that.
    """
    mod = _load(adapter, unique)
    seen: list = []

    def fake_run(argv, **kwargs):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    if hasattr(mod, "shutil"):
        monkeypatch.setattr(mod.shutil, "which",
                             lambda cmd=None, *a, **k: f"/usr/bin/{cmd or 'tool'}")
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.setattr(mod.sys, "argv", [str(adapter), name])
    monkeypatch.chdir(tmp_path)
    (tmp_path / name).write_text("stub content\n", encoding="utf-8")
    try:
        mod.main()
    except Exception:
        pass  # only the argv sent to subprocess.run is under test
    return seen


@pytest.mark.parametrize("slug,relpath,route", CASES, ids=[c[0] for c in CASES])
@pytest.mark.parametrize("name", DASH_NAMES)
def test_a_flag_shaped_filename_is_not_read_as_an_option(
        monkeypatch, tmp_path, slug, relpath, route, name) -> None:
    """The "must not fire" half: the crafted name never reaches the tool
    as a bare argument the tool's own parser could read as an option.
    """
    adapter = VALIDATORS / relpath
    calls = _spawn_calls(monkeypatch, tmp_path, adapter,
                          f"dash_{slug.replace('-', '_')}_{name!r}", name)
    assert calls, f"{slug}: the adapter never spawned its tool"
    # A single string broadcasts to every captured call; a tuple/list pairs
    # one route per call, in call order (prettier-check's two calls).
    routes = [route] * len(calls) if isinstance(route, str) else list(route)
    assert len(routes) == len(calls), (
        f"{slug}: {len(calls)} calls captured but {len(routes)} routes "
        f"declared: {calls}")
    for argv, this_route in zip(calls, routes):
        if this_route == "separator":
            assert name in argv, (slug, argv)
            i = argv.index(name)
            assert i > 0 and argv[i - 1] == "--", (
                f"{slug}: {name!r} reached the tool with no `--` "
                f"separator ahead of it: {argv}")
        else:
            assert this_route == "contained"
            assert name not in argv, (
                f"{slug}: {name!r} reached the tool completely "
                f"unmodified, so a leading `-`/`--` is still live: {argv}")
            matches = [a for a in argv if a.endswith(name)]
            assert matches, (slug, argv)
            for a in matches:
                assert not a.startswith("-"), (
                    f"{slug}: containment left an argv entry still "
                    f"starting with '-': {a!r} in {argv}")


@pytest.mark.parametrize("slug,relpath,route", CASES, ids=[c[0] for c in CASES])
def test_an_ordinary_filename_still_reaches_the_tool(
        monkeypatch, tmp_path, slug, relpath, route) -> None:
    """The "must fire" positive control: an unremarkable name is not
    mangled by the same fix that contains a dash-shaped one.
    """
    adapter = VALIDATORS / relpath
    calls = _spawn_calls(monkeypatch, tmp_path, adapter,
                          f"plain_{slug.replace('-', '_')}", PLAIN_NAME)
    assert calls, f"{slug}: the adapter never spawned its tool"
    for argv in calls:
        matches = [a for a in argv if a.endswith(PLAIN_NAME)]
        assert matches, (slug, argv)
