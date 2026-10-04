"""#2705 (ported from claude-remember#851) -- the build script that turns a git ref
into the slim `release` tree.

The Anthropic directory holds any plugin folder with a non-image file of 256 KiB or
more, more than 512 files, or a `.gitattributes` carrying export-ignore/export-subst/
filter. `master` keeps everything; `.github/scripts/build_release_tree.py` extracts a
ref through `git ls-tree` + `git cat-file` (never `git archive`, which would need
export-ignore), drops a deny-list, cuts CHANGELOG.md to the latest released section,
and rewrites links that would otherwise point at removed paths.

Written before the script existed. Every "must be removed" assertion is paired with
a "must be kept" one: a build that produced an empty tree would pass every removal
check, and a build that copied everything would pass every keep check.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / ".github" / "scripts" / "build_release_tree.py"
CONFIG = REPO_ROOT / ".github" / "release-branch.json"

_BS = chr(92)
_BASH_PROBE = "supertool-bash-ok"


def _bash_candidates():
    """Where a bash that actually runs scripts might be, most likely first
    (same list tests/test_guard_interpreter_ladder_1390.py resolves
    through, #1399/#1390): PATH first, then the two Git-for-Windows
    locations, then the POSIX ones."""
    git_bin = "C:" + _BS + "Program Files" + _BS + "Git" + _BS
    return [shutil.which("bash"),
            git_bin + "bin" + _BS + "bash.exe",
            git_bin + "usr" + _BS + "bin" + _BS + "bash.exe",
            "/bin/bash", "/usr/bin/bash", "/usr/local/bin/bash"]


def _first_bash_that_runs_a_script():
    """A bash chosen by what it does, not by what it is called -- #2732's
    own new tests hit the same shape #1390 already fixed on windows-latest.
    shutil.which("bash") answers "a file
    named bash is on PATH", which on windows-latest is satisfied by the
    System32 WSL launcher stub -- a program that is not a shell, cannot
    open a script, and writes a UTF-16LE refusal instead. Each candidate is
    asked to print a known string; the one that actually does is returned,
    and that exact path is what gets spawned later -- subprocess.run with a
    bare "bash" on Windows re-searches PATH through CreateProcess, which
    need not agree with shutil.which, so probing one executable and
    spawning the bare name proves nothing about the one that runs.

    Returns None, never a platform name, when nothing on this host
    qualifies: tests/test_symlink_capability_1143.py is the precedent for
    why a skip must be gated on a capability probe, not on os.name."""
    for candidate in _bash_candidates():
        if not candidate:
            continue
        try:
            proc = subprocess.run(
                [candidate, "-c", "printf %s " + _BASH_PROBE],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=60)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0 and proc.stdout.strip() == _BASH_PROBE:
            return candidate
    return None


#: Resolved once at collection time rather than per-test: three tests below
#: each need it, and re-probing three times would triple the spawn cost for
#: the same answer.
BASH = _first_bash_that_runs_a_script()

SLUG = "Example-Org/example-plugin"
RAW = "https://raw.githubusercontent.com/Example-Org/example-plugin/main/"
BLOB = "https://github.com/Example-Org/example-plugin/blob/main/"


def _load():
    assert SCRIPT.exists(), f"{SCRIPT} does not exist (#2705)"
    spec = importlib.util.spec_from_file_location("build_release_tree", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["build_release_tree"] = mod
    spec.loader.exec_module(mod)
    return mod


def _git_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_NOSYSTEM": "1",
    })
    return env


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True,
        text=True, encoding="utf-8", errors="replace", env=_git_env(),
    ).stdout


CHANGELOG = """# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added

- something not released yet (must not ship)

## [0.2.0] - 2026-01-02 -- second

### Fixed

- the latest released fix

## [0.1.0] - 2026-01-01 -- first

- the oldest entry (must not ship)

[Unreleased]: https://github.com/Example-Org/example-plugin/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Example-Org/example-plugin/releases/tag/v0.2.0
[0.1.0]: https://github.com/Example-Org/example-plugin/releases/tag/v0.1.0
"""

README = """# Example

![logo](docs/logo.png)

<p align="center"><img src="docs/logo.png" width="200"></p>

[![License](https://img.shields.io/badge/x-y-z)](LICENSE)

Read [the guide](docs/guide.md#install) or [the changelog](CHANGELOG.md).

Absolute stays: [site](https://example.com/docs/guide.md).

Anchor stays: [below](#example).

```
![in a fence](docs/logo.png)
```

Inline code stays: `[x](docs/guide.md)`.

[ref-style]: docs/guide.md
"""

SKILL = """---
name: s
description: d
---

See [the guide](../../docs/guide.md) and [hooks](../../hooks/hooks.json).
"""


def _make_repo(tmp_path: Path, *, changelog: str = CHANGELOG,
               extra: dict | None = None) -> Path:
    repo = tmp_path / "src"
    files = {
        ".claude-plugin/plugin.json": json.dumps({"name": "example", "version": "0.2.0"}),
        "hooks/hooks.json": json.dumps({"hooks": {}}),
        "scripts/run.sh": "#!/bin/sh\necho hi\n",
        "scripts/run-tests.sh": "#!/bin/sh\npytest\n",
        "pipeline/__init__.py": "",
        "skills/s/SKILL.md": SKILL,
        "tests/test_x.py": "def test_x():\n    pass\n",
        "docs/guide.md": "# Guide\n",
        "docs/logo.png": "\x89PNG fake\n",
        ".github/workflows/x.yml": "name: x\n",
        ".claude/jit-context/rule.md": "rule\n",
        "CLAUDE.md": "# dev notes\n",
        "LICENSE": "license\n",
        "README.md": README,
        "CHANGELOG.md": changelog,
    }
    files.update(extra or {})
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=_git_env())
    _git(repo, "add", "-A")
    _git(repo, "update-index", "--chmod=+x", "scripts/run.sh")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "tag", "v0.2.0")
    return repo


def _config(**over) -> dict:
    cfg = {
        "repo": SLUG,
        "default_branch": "main",
        "deny": ["tests/", "docs/", ".github/", ".claude/", "CLAUDE.md",
                 "scripts/run-tests.sh"],
        "changelog": "CHANGELOG.md",
        "rewrite_links": True,
    }
    cfg.update(over)
    return cfg


def _build(tmp_path: Path, repo: Path, cfg: dict | None = None, ref: str = "v0.2.0") -> Path:
    mod = _load()
    out = tmp_path / "out"
    mod.build(repo, ref, out, cfg or _config())
    return out


def _files(root: Path) -> set:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


# -- deny-list ------------------------------------------------------------------

def test_denied_paths_are_removed_and_everything_else_is_kept(tmp_path):
    out = _build(tmp_path, _make_repo(tmp_path))
    files = _files(out)
    for gone in ("tests/test_x.py", "docs/guide.md", "docs/logo.png",
                 ".github/workflows/x.yml", ".claude/jit-context/rule.md",
                 "CLAUDE.md", "scripts/run-tests.sh"):
        assert gone not in files, f"{gone} is on the deny-list but shipped"
    # Positive control: the build did not just produce an empty tree.
    for kept in (".claude-plugin/plugin.json", "hooks/hooks.json", "scripts/run.sh",
                 "pipeline/__init__.py", "skills/s/SKILL.md", "LICENSE",
                 "README.md", "CHANGELOG.md"):
        assert kept in files, f"{kept} is not on the deny-list but was dropped"


def test_a_file_entry_is_exact_and_a_directory_entry_needs_its_slash(tmp_path):
    """`CLAUDE.md` must not take `docs2/CLAUDE.md` or `CLAUDE.md.bak` with it, and
    `tests/` must not take `tests-helper.sh`."""
    repo = _make_repo(tmp_path, extra={
        "sub/CLAUDE.md": "kept\n", "CLAUDE.md.bak": "kept\n", "tests-helper.sh": "kept\n",
    })
    files = _files(_build(tmp_path, repo))
    assert "CLAUDE.md" not in files
    assert {"sub/CLAUDE.md", "CLAUDE.md.bak", "tests-helper.sh"} <= files


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_the_executable_bit_survives_extraction(tmp_path):
    out = _build(tmp_path, _make_repo(tmp_path))
    assert os.stat(out / "scripts/run.sh").st_mode & stat.S_IXUSR
    # Positive control: a 100644 blob is not made executable wholesale.
    assert not os.stat(out / "LICENSE").st_mode & stat.S_IXUSR


def test_the_ref_is_built_not_the_working_tree(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "LICENSE").write_text("uncommitted edit\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("x\n", encoding="utf-8")
    out = _build(tmp_path, repo)
    assert (out / "LICENSE").read_text(encoding="utf-8") == "license\n"
    assert not (out / "untracked.txt").exists()


def test_a_non_empty_output_directory_is_refused(tmp_path):
    mod = _load()
    repo = _make_repo(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "precious.txt").write_text("x", encoding="utf-8")
    with pytest.raises(mod.BuildError, match="not empty"):
        mod.build(repo, "v0.2.0", out, _config())
    assert (out / "precious.txt").exists()
    # Positive control: an existing EMPTY directory is accepted.
    out2 = tmp_path / "out2"
    out2.mkdir()
    mod.build(repo, "v0.2.0", out2, _config())
    assert (out2 / "LICENSE").exists()


def test_a_forbidden_gitattributes_stops_the_build(tmp_path):
    mod = _load()
    repo = _make_repo(tmp_path, extra={".gitattributes": "tests/ export-ignore\n"})
    with pytest.raises(mod.BuildError, match="export-ignore"):
        mod.build(repo, "v0.2.0", tmp_path / "out", _config())


def test_a_benign_gitattributes_ships(tmp_path):
    repo = _make_repo(tmp_path, extra={".gitattributes": "*.sh text eol=lf\n"})
    out = _build(tmp_path, repo)
    assert (out / ".gitattributes").read_text(encoding="utf-8") == "*.sh text eol=lf\n"


# -- CHANGELOG -------------------------------------------------------------------

def test_changelog_keeps_only_the_latest_released_section(tmp_path):
    text = (_build(tmp_path, _make_repo(tmp_path)) / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [0.2.0] - 2026-01-02" in text
    assert "the latest released fix" in text
    assert "[0.2.0]: https://github.com/Example-Org/example-plugin/releases/tag/v0.2.0" in text
    assert "## [Unreleased]" not in text
    assert "not released yet" not in text
    assert "[Unreleased]:" not in text
    assert "## [0.1.0]" not in text
    assert "the oldest entry" not in text
    assert "[0.1.0]:" not in text
    assert BLOB + "CHANGELOG.md" in text


def test_changelog_skips_an_empty_unreleased_section_too(tmp_path):
    """The real CHANGELOG's first `## [` is an EMPTY [Unreleased]; "keep the first
    section" would then ship nothing."""
    empty = CHANGELOG.replace(
        "### Added\n\n- something not released yet (must not ship)\n\n", "")
    mod = _load()
    out = mod.cut_changelog(empty, BLOB + "CHANGELOG.md")
    assert "the latest released fix" in out
    assert "## [Unreleased]" not in out


def test_changelog_with_no_released_section_is_an_error_not_an_empty_file():
    mod = _load()
    with pytest.raises(mod.BuildError, match="released"):
        mod.cut_changelog("# Changelog\n\n## [Unreleased]\n\n- x\n", BLOB + "CHANGELOG.md")


def test_changelog_preamble_is_kept():
    mod = _load()
    out = mod.cut_changelog(CHANGELOG, BLOB + "CHANGELOG.md")
    assert out.startswith("# Changelog\n")


# -- link rewriting --------------------------------------------------------------

def test_readme_links_into_removed_paths_become_absolute(tmp_path):
    text = (_build(tmp_path, _make_repo(tmp_path)) / "README.md").read_text(encoding="utf-8")
    assert f"![logo]({RAW}docs/logo.png)" in text
    assert f'<img src="{RAW}docs/logo.png" width="200">' in text
    assert f"[the guide]({BLOB}docs/guide.md#install)" in text
    assert f"[ref-style]: {BLOB}docs/guide.md" in text
    # Only the two inside code (the fence and the inline span) may remain relative.
    assert text.count("](docs/") == 2, text


def test_readme_links_that_still_resolve_are_left_alone(tmp_path):
    text = (_build(tmp_path, _make_repo(tmp_path)) / "README.md").read_text(encoding="utf-8")
    assert "](LICENSE)" in text
    assert "[the changelog](CHANGELOG.md)" in text
    assert "[site](https://example.com/docs/guide.md)" in text
    assert "[below](#example)" in text
    assert "![in a fence](docs/logo.png)" in text, "fenced code must not be rewritten"
    assert "`[x](docs/guide.md)`" in text, "inline code must not be rewritten"


def test_nested_markdown_links_resolve_relative_to_their_own_file(tmp_path):
    text = (_build(tmp_path, _make_repo(tmp_path)) / "skills/s/SKILL.md").read_text(encoding="utf-8")
    assert f"[the guide]({BLOB}docs/guide.md)" in text
    # Positive control: a relative link to a path that ships is untouched.
    assert "[hooks](../../hooks/hooks.json)" in text


def test_the_link_base_comes_from_the_config(tmp_path):
    cfg = _config(repo="Other/thing", default_branch="trunk")
    text = (_build(tmp_path, _make_repo(tmp_path), cfg) / "README.md").read_text(encoding="utf-8")
    assert "https://raw.githubusercontent.com/Other/thing/trunk/docs/logo.png" in text
    assert "https://github.com/Other/thing/blob/trunk/docs/guide.md#install" in text


# -- the real repository ---------------------------------------------------------

def test_the_committed_config_parses_and_names_the_brief_deny_list():
    mod = _load()
    cfg = mod.load_config(CONFIG)
    for entry in ("tests/", "docs/", ".oss/", ".github/", ".githooks/", ".claude/",
                  "outbound/", "trap.d/", "changelog.d/", "CLAUDE.md",
                  "CONTRIBUTING.md", "SECURITY.md", "CODE_OF_CONDUCT.md",
                  "pyproject.toml", ".oss.json", ".supertool.json",
                  ".supertool.example.json", "supertool-banner.webp",
                  "notifiers/claude-channel/README.md",
                  "notifiers/claude-channel/install.sh",
                  "notifiers/cursor-witness/"):
        assert entry in cfg["deny"], entry
    # Positive control: the MCP server's own command script, the thing
    # .mcp.json actually runs, must not be denied by the directory sweep
    # above -- only the human-facing setup docs beside it are.
    assert "notifiers/claude-channel/" not in cfg["deny"]
    # Positive control: nothing the plugin runs is denied.
    for runtime in ("hooks/", "presets/", "validators/", "formatters/", "notifiers/",
                    ".claude-plugin/"):
        assert runtime not in cfg["deny"], runtime
    # _shipped_reference.py and .mcp.json are deliberately NOT denied (#2705): the
    # first is the fallback _shipped_config() reads once .supertool.json is denied,
    # the second registers the claude-channel notifier the manifest declares.
    assert "_shipped_reference.py" not in cfg["deny"]
    assert ".mcp.json" not in cfg["deny"]
    assert cfg["budget"]["max_files"] == 512
    assert cfg["budget"]["max_file_bytes"] == 262144
    # #2706 used to hold _supertool.py's size exception here while its split
    # was in progress; all ten part lanes have since landed and _supertool.py
    # itself is well under max_file_bytes, so the exception list is empty
    # again -- not restored, and not replaced with a different one.
    assert cfg.get("exceptions", []) == [], cfg.get("exceptions")
    supertool_bytes = (REPO_ROOT / "_supertool.py").stat().st_size
    assert supertool_bytes < cfg["budget"]["max_file_bytes"], (
        f"_supertool.py is {supertool_bytes} bytes, over the "
        f"{cfg['budget']['max_file_bytes']}-byte budget the removed "
        "exception used to cover -- #2706's split regressed")


def test_building_this_repository_head_ships_every_hook_script(tmp_path):
    """Integration: the real deny-list against the real tree. Every script
    hooks/hooks.json names must survive the build."""
    mod = _load()
    out = tmp_path / "out"
    mod.build(REPO_ROOT, "HEAD", out, mod.load_config(CONFIG))
    hooks = json.loads((out / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    commands = [h["command"] for groups in hooks["hooks"].values()
                for g in groups for h in g["hooks"]]
    assert commands
    for cmd in commands:
        rel = cmd.split("${CLAUDE_PLUGIN_ROOT}/", 1)[1].split('"', 1)[0]
        assert (out / rel).is_file(), f"{rel} (named by hooks.json) did not ship"
    assert not (out / "tests").exists()
    assert not (out / "docs").exists()
    assert (out / "CHANGELOG.md").stat().st_size < 262144


def test_credential_forwarding_presets_do_not_ship(tmp_path):
    """#2734: the directory build must not carry a preset that reads a
    credential and sends it to its own vendor's API -- the shape the
    Anthropic directory's MCP_FORWARDS_CREDENTIAL_ENV hold fires on.

    Checks both that the config's deny list agrees with
    `_supertool_config._DIRECTORY_BUILD_EXCLUDED_PRESETS` (so the clear
    "not in this build" message and the actual build can never drift apart)
    and that a real build of this repository's own HEAD leaves every denied
    file and directory out, while a sibling preset's own files still ship --
    a build that dropped presets/ entirely would pass the first half of this
    test and fail only the second."""
    sys.path.insert(0, str(REPO_ROOT))
    import supertool  # noqa: E402  (module-identity-swapped to _supertool, #931)
    mod = _load()
    cfg = mod.load_config(CONFIG)
    excluded = sorted(supertool._DIRECTORY_BUILD_EXCLUDED_PRESETS)
    assert excluded == ["bluesky", "devto", "hashnode", "slack", "youtube"], excluded
    for name in excluded:
        assert f"presets/{name}.json" in cfg["deny"], name
        assert f"presets/{name}/" in cfg["deny"], name
    # The slack watch source dynamically loads presets/slack/_auth.py and
    # presets/slack/_api.py by file path at import time -- denying
    # presets/slack/ without this would ship a source whose import crashes.
    assert "presets/watch/sources/slack/" in cfg["deny"]
    # bluesky-engagement and devto-engagement read their own env vars
    # directly and do not import from presets/bluesky or presets/devto, but
    # #2734 excludes them too (named explicitly in the issue).
    assert "presets/watch/sources/bluesky-engagement/" in cfg["deny"]
    assert "presets/watch/sources/devto-engagement/" in cfg["deny"]

    out = tmp_path / "out"
    mod.build(REPO_ROOT, "HEAD", out, cfg)
    for name in excluded:
        assert not (out / "presets" / f"{name}.json").exists(), name
        assert not (out / "presets" / name).exists(), name
    for source in ("slack", "bluesky-engagement", "devto-engagement"):
        assert not (out / "presets" / "watch" / "sources" / source).exists(), source
    # Positive control: a sibling preset this issue does not touch still
    # ships, same shape as the "nothing is present" trap the module docstring
    # warns an empty-tree build would otherwise pass unnoticed.
    assert (out / "presets" / "github.json").is_file()
    assert (out / "presets" / "github").is_dir()
    assert (out / "presets" / "watch" / "sources" / "gh-run").is_dir()


def test_cli_builds_and_reports(tmp_path):
    repo = _make_repo(tmp_path)
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps(_config()), encoding="utf-8")
    out = tmp_path / "out"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo), "--ref", "v0.2.0",
         "--out", str(out), "--config", str(cfg)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=_git_env(), check=False,
    )
    assert r.returncode == 0, r.stderr
    assert (out / "LICENSE").exists()
    assert "removed" in r.stdout


def test_cli_fails_loudly_on_an_unknown_ref(tmp_path):
    repo = _make_repo(tmp_path)
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps(_config()), encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo), "--ref", "v9.9.9",
         "--out", str(tmp_path / "out"), "--config", str(cfg)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=_git_env(), check=False,
    )
    assert r.returncode != 0
    assert "v9.9.9" in r.stderr


def test_the_banner_is_denied_and_the_readme_reference_rewritten(tmp_path):
    """#2732: the Anthropic directory held a release-tree probe on
    UNREAD_ASSET_REFERENCED, citing supertool-banner.webp. The fix is to not
    ship it at all -- denying it lets the existing link-rewriting machinery
    turn README.md's `<img src="supertool-banner.webp">` into an absolute
    raw.githubusercontent.com URL on the default branch, the same mechanism
    already proven generically above for docs/logo.png."""
    mod = _load()
    cfg = mod.load_config(CONFIG)
    assert "supertool-banner.webp" in cfg["deny"], cfg["deny"]
    out = tmp_path / "out"
    report = mod.build(REPO_ROOT, "HEAD", out, cfg)
    assert "supertool-banner.webp" in report["removed"], report["removed"]
    assert not (out / "supertool-banner.webp").exists()
    # Not asserted here: the rewritten-URL mechanism itself. This build also
    # swaps README.release.md in for README.md (#2732's other step), so the
    # shipped README no longer carries the original <img src> to rewrite at
    # all -- see test_a_denied_image_referenced_by_readme_is_rewritten below
    # for that mechanism in isolation, decoupled from the readme swap.


def test_a_denied_image_referenced_by_readme_is_rewritten(tmp_path):
    """The banner-denial mechanism in isolation: a synthetic repo whose
    README references a denied image, with no readme swap configured --
    the same generic path the real repo's docs/logo.png case already
    proves, applied to a root-level webp via an <img> tag."""
    repo = _make_repo(tmp_path, extra={
        "banner.webp": "fake webp\n",
        "README.md": '<img src="banner.webp" width="900">\n\n' + README,
    })
    cfg = _config(deny=_config()["deny"] + ["banner.webp"])
    out = _build(tmp_path, repo, cfg)
    assert not (out / "banner.webp").exists()
    readme = (out / "README.md").read_text(encoding="utf-8")
    assert 'src="banner.webp"' not in readme, readme
    assert ('src="https://raw.githubusercontent.com/Example-Org/'
            'example-plugin/main/banner.webp"') in readme, readme


# -- #2732: inline hooks/python-ladder.sh rather than source it -----------------

_LADDER = (
    'SUPERTOOL_LADDER_PROBE=1\n'
    'supertool_python_identifies() { return 1; }\n'
    'supertool_python_each() { "$1" fake; }\n'
)

_GUARDED_CONSUMER = (
    '#!/bin/bash\n'
    'echo before\n'
    'LADDER="$(cd "$(dirname "$0")" && pwd)/python-ladder.sh"\n'
    '# shellcheck source=hooks/python-ladder.sh\n'
    '. "$LADDER" 2>/dev/null || decline "the shared interpreter ladder could not be sourced"\n'
    'echo after\n'
)

_IF_CONSUMER = (
    '#!/bin/bash\n'
    'LADDER="$(cd "$(dirname "$0")" && pwd)/python-ladder.sh"\n'
    'echo setup\n'
    '# shellcheck source=hooks/python-ladder.sh\n'
    'if . "$LADDER" 2>/dev/null; then\n'
    '    supertool_python_each onboard\n'
    '    echo ran\n'
    'else\n'
    '    echo "> could not source hooks/python-ladder.sh"\n'
    'fi\n'
    'exit 0\n'
)


def _ladder_config() -> dict:
    return {"ladder_inline": {
        "ladder": "hooks/python-ladder.sh",
        "consumers": ["hooks/pre-bash-guard.sh", "hooks/session-start.sh"],
    }}


def test_the_ladder_is_inlined_into_the_guarded_consumer():
    mod = _load()
    contents = {
        "hooks/python-ladder.sh": _LADDER.encode(),
        "hooks/pre-bash-guard.sh": _GUARDED_CONSUMER.encode(),
    }
    inlined = mod.inline_python_ladder(
        contents, {"ladder_inline": {"ladder": "hooks/python-ladder.sh",
                                      "consumers": ["hooks/pre-bash-guard.sh"]}})
    assert inlined == ["hooks/pre-bash-guard.sh"]
    assert "hooks/python-ladder.sh" not in contents
    text = contents["hooks/pre-bash-guard.sh"].decode()
    assert "LADDER=" not in text
    assert "python-ladder.sh" not in text
    assert "supertool_python_identifies" in text
    assert "echo before" in text and "echo after" in text
    if BASH is None:
        pytest.skip("no bash that actually runs a script was found on this host")
    # Behaviour-preserving: a bash parse of the inlined script still
    # succeeds and defines the ladder's own functions.
    r = subprocess.run([BASH, "-n", "-c", text], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr


def test_the_ladder_is_inlined_into_the_if_then_else_consumer():
    mod = _load()
    contents = {
        "hooks/python-ladder.sh": _LADDER.encode(),
        "hooks/session-start.sh": _IF_CONSUMER.encode(),
    }
    inlined = mod.inline_python_ladder(
        contents, {"ladder_inline": {"ladder": "hooks/python-ladder.sh",
                                      "consumers": ["hooks/session-start.sh"]}})
    assert inlined == ["hooks/session-start.sh"]
    text = contents["hooks/session-start.sh"].decode()
    assert "python-ladder.sh" not in text
    assert "could not source" not in text
    assert "echo setup" in text and "supertool_python_each onboard" in text
    assert "echo ran" in text
    if BASH is None:
        pytest.skip("no bash that actually runs a script was found on this host")
    r = subprocess.run([BASH, "-n", "-c", text], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr


def test_no_ladder_inline_key_is_a_noop():
    mod = _load()
    contents = {"hooks/pre-bash-guard.sh": _GUARDED_CONSUMER.encode()}
    assert mod.inline_python_ladder(contents, {}) == []
    assert contents["hooks/pre-bash-guard.sh"].decode() == _GUARDED_CONSUMER


def test_a_configured_ladder_missing_from_the_tree_refuses(tmp_path):
    """#2732 self-review: a config naming a ladder not in this tree must
    not render the same as 'nothing configured' -- a consumer could still
    carry the raw source/. line with nothing left to inline it."""
    mod = _load()
    contents = {"hooks/pre-bash-guard.sh": _GUARDED_CONSUMER.encode()}
    with pytest.raises(mod.BuildError):
        mod.inline_python_ladder(contents, _ladder_config())


def test_an_unrecognised_consumer_refuses_rather_than_ships_broken():
    """Positive control for the error path: a consumer whose source call
    site this step cannot find must stop the build, not silently ship a
    hook with the ladder's functions undefined."""
    mod = _load()
    contents = {
        "hooks/python-ladder.sh": _LADDER.encode(),
        "hooks/pre-bash-guard.sh": b"#!/bin/bash\necho no ladder reference here\n",
    }
    with pytest.raises(mod.BuildError):
        mod.inline_python_ladder(contents, _ladder_config())


def test_building_this_repository_head_ships_no_shell_hook(tmp_path):
    """Integration: the real deny-list against the real tree. #2734 replaced
    #2732's ladder inlining: the release hooks.json runs Python directly
    (release_hooks), so the two .sh hooks and the ladder they sourced do not
    ship at all, and no shipped hook command names a .sh file."""
    mod = _load()
    cfg = mod.load_config(CONFIG)
    out = tmp_path / "out"
    report = mod.build(REPO_ROOT, "HEAD", out, cfg)
    assert report["ladder_inlined"] == []
    for rel in ("hooks/python-ladder.sh", "hooks/pre-bash-guard.sh",
                "hooks/session-start.sh"):
        assert not (out / rel).exists(), rel
        assert rel in report["removed"], rel
    hooks = json.loads((out / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    commands = [h["command"] for groups in hooks["hooks"].values()
                for group in groups for h in group["hooks"]]
    assert commands, "the built hooks.json registers no hook at all"
    assert not any(".sh" in c for c in commands), commands


# -- #2732: README.release.md swapped in for README.md at build time -----------

def test_the_release_readme_is_swapped_in():
    mod = _load()
    contents = {"README.md": b"full readme, long and detailed",
                "README.release.md": b"short release readme"}
    swapped = mod.swap_release_readme(contents, {"release_readme": "README.release.md"})
    assert swapped is True
    assert contents == {"README.md": b"short release readme"}


def test_no_release_readme_key_is_a_noop():
    mod = _load()
    contents = {"README.md": b"full readme"}
    assert mod.swap_release_readme(contents, {}) is False
    assert contents == {"README.md": b"full readme"}


def test_a_configured_release_readme_missing_from_the_tree_refuses():
    """#2732 self-review: a config naming a release_readme not in this tree
    must not render the same as 'nothing configured' -- README.md would
    ship unreplaced, full security example and all, with nothing saying
    so."""
    mod = _load()
    contents = {"README.md": b"full readme"}
    with pytest.raises(mod.BuildError):
        mod.swap_release_readme(contents, {"release_readme": "README.release.md"})


def test_building_this_repository_head_ships_the_release_readme(tmp_path):
    """Integration: the real README.release.md against the real tree."""
    mod = _load()
    cfg = mod.load_config(CONFIG)
    out = tmp_path / "out"
    report = mod.build(REPO_ROOT, "HEAD", out, cfg)
    assert report["readme_swapped"] is True
    assert not (out / "README.release.md").exists()
    shipped = (out / "README.md").read_text(encoding="utf-8")
    full = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert shipped != full
    assert "supertool-banner.webp" not in shipped
    assert "authorized_keys" not in shipped
    for word in ("curl", "wget", "printenv"):
        assert word not in shipped.lower(), (word, shipped)
