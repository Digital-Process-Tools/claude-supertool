"""Credential-shaped files must not reach the agent's context (#691, theme T5).

`_DEFAULT_EXCLUDE_PATHS` has carried `.env/`, `.env.local/` and friends since
#146, with a comment promising the trailing slash catches a *file* of that name
as well as a directory. `_is_excluded` does honour that. Nothing ever called it
on a file:

- `_grep_candidates` applied it to `dirs[:]` only; the `for name in files` loop
  filtered on extension and nothing else.
- the rtk-delegated path emitted `grep --exclude-dir=NAME`, which by definition
  cannot skip a file.
- `_glob_files`' walk branch (recursive `**` patterns) filtered dirs only, while
  its glob.glob branch post-filtered files — so the two halves of one op
  disagreed with each other.
- `_collect_files` (map) and `op_tree` had the same dirs-only shape.

Every test here uses obviously fake values. Nothing in this file is a real
credential, and no test prints one on failure.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import supertool


# ---------------------------------------------------------------------------
# Fixture tree — one file per credential shape, all values FAKE_*
# ---------------------------------------------------------------------------

# basename -> content. The needle is always FAKE_<something>.
SECRET_FILES = {
    ".env": "TOK=FAKE_root_env_value\n",
    ".env.production": "K=FAKE_env_production_value\n",
    ".env.local": "K=FAKE_env_local_value\n",
    ".netrc": "machine example.com password FAKE_netrc_value\n",
    ".npmrc": "//registry.npmjs.org/:_authToken=FAKE_npmrc_value\n",
    ".git-credentials": "https://u:FAKE_gitcred_value@github.com\n",
    ".pgpass": "localhost:5432:db:user:FAKE_pgpass_value\n",
    ".pypirc": "password = FAKE_pypirc_value\n",
    ".htpasswd": "user:FAKE_htpasswd_value\n",
    "id_rsa": "-----BEGIN OPENSSH PRIVATE KEY-----\nFAKE_id_rsa_value\n",
    "id_ed25519": "FAKE_id_ed25519_value\n",
    "server.pem": "-----BEGIN PRIVATE KEY-----\nFAKE_pem_value\n",
    "tls.key": "FAKE_tls_key_value\n",
    "bundle.p12": "FAKE_p12_value\n",
    # supertool's own documented cwd token files (presets/*/_auth.py)
    ".hashnode-token": "FAKE_hashnode_value\n",
    ".devto-token": "FAKE_devto_value\n",
    ".bluesky-app-password": "FAKE_bluesky_value\n",
}

# Files that must stay visible. `.env.example` and friends are committed
# placeholders — hiding them is the over-broad failure this list must not have.
VISIBLE_FILES = {
    "app.py": 'TOKEN_NAME = "FAKE_source_value"\n',
    ".env.example": "K=FAKE_example_placeholder\n",
    ".env.sample": "K=FAKE_sample_placeholder\n",
    "notes.md": "see FAKE_notes_value\n",
}

SECRET_NEEDLES = tuple(
    re.search(r"FAKE_\w+", body).group(0)
    for body in SECRET_FILES.values()
)


def _make_secret_tree(root: Path) -> None:
    """Write the fixture tree, with one nested copy to pin the nested case."""
    for name, body in SECRET_FILES.items():
        (root / name).write_text(body, encoding="utf-8")
    for name, body in VISIBLE_FILES.items():
        (root / name).write_text(body, encoding="utf-8")
    sub = root / "sub"
    sub.mkdir()
    (sub / ".env").write_text("TOK=FAKE_nested_env_value\n", encoding="utf-8")
    (sub / "lib.py").write_text(
        'X = "FAKE_nested_source_value"\n', encoding="utf-8")


def _assert_no_secret(out: str) -> None:
    leaked = [n for n in SECRET_NEEDLES + ("FAKE_nested_env_value",) if n in out]
    # Report the count and the *filenames*, never the values themselves.
    assert not leaked, f"{len(leaked)} secret value(s) reached the output"


# ---------------------------------------------------------------------------
# rtk fixtures — the engine my repro went through, and the one most likely
# to be missed by a fix that lands on the native walker only.
# ---------------------------------------------------------------------------


class _RtkCalls:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []


@pytest.fixture
def rtk_real_grep(monkeypatch: pytest.MonkeyPatch) -> _RtkCalls:
    """Delegate to the *system* grep, exactly as rtk itself does.

    Stubbing `_rtk_run` with canned output would let a broken
    `--exclude`/`--exclude-dir` argv pass unnoticed. This stub runs the real
    grep with the argv supertool built, so the flags are genuinely exercised —
    and it stubs one named function rather than doubling `subprocess.run`
    wholesale (#731).
    """
    if not shutil.which("grep"):
        pytest.skip("system grep unavailable")
    seen = _RtkCalls()

    def _fake_rtk_run(args, timeout: int = 30) -> str | None:
        seen.calls.append(list(args))
        assert args[0] == "grep"
        proc = subprocess.run(
            ["grep"] + list(args[1:]),
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        return proc.stdout if proc.returncode == 0 else None

    monkeypatch.setattr(supertool, "_CONFIG", {"rtk": True})
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    monkeypatch.setattr(supertool, "_RTK_CHECKED", True)
    monkeypatch.setattr(supertool, "_RTK_PATH", "/fake/bin/rtk")
    monkeypatch.setattr(supertool, "_rtk_run", _fake_rtk_run)
    return seen


@pytest.fixture
def rtk_leaky(monkeypatch: pytest.MonkeyPatch) -> _RtkCalls:
    """An rtk that ignores every exclude flag and returns the secrets anyway.

    Pins the guarantee that does not depend on the argv: whatever the delegated
    engine hands back is filtered through the same `_is_excluded` the native
    walker uses, so a grep that does not honour `--exclude` (or an rtk release
    that rewrites the argv) still cannot leak.
    """
    seen = _RtkCalls()

    def _fake_rtk_run(args, timeout: int = 30) -> str | None:
        seen.calls.append(list(args))
        path = args[-1]
        lines = []
        for root, _dirs, files in os.walk(path):
            for name in sorted(files):
                full = os.path.join(root, name)
                for i, line in enumerate(
                    Path(full).read_text(encoding="utf-8").splitlines(), start=1
                ):
                    if "FAKE" in line:
                        lines.append(f"{full}:{i}:{line}")
        return "\n".join(lines) + "\n"

    monkeypatch.setattr(supertool, "_CONFIG", {"rtk": True})
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    monkeypatch.setattr(supertool, "_RTK_CHECKED", True)
    monkeypatch.setattr(supertool, "_RTK_PATH", "/fake/bin/rtk")
    monkeypatch.setattr(supertool, "_rtk_run", _fake_rtk_run)
    return seen


# ---------------------------------------------------------------------------
# The repro: grep, both engines
# ---------------------------------------------------------------------------


def test_native_grep_does_not_surface_secret_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_secret_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    out = supertool.op_grep("FAKE", ".", limit=100)
    _assert_no_secret(out)
    assert "FAKE_source_value" in out
    assert "FAKE_nested_source_value" in out


def test_delegated_grep_does_not_surface_secret_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rtk_real_grep: _RtkCalls
) -> None:
    """The engine the reported repro actually went through."""
    _make_secret_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    out = supertool.op_grep("FAKE", ".", limit=100)
    assert rtk_real_grep.calls, "delegated branch not taken — this pins nothing"
    _assert_no_secret(out)
    assert "FAKE_source_value" in out


def test_delegated_grep_filters_even_when_the_engine_ignores_the_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rtk_leaky: _RtkCalls
) -> None:
    _make_secret_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    out = supertool.op_grep("FAKE", ".", limit=100)
    assert rtk_leaky.calls, "delegated branch not taken — this pins nothing"
    _assert_no_secret(out)


def _paths_in(out: str) -> set[str]:
    found = set()
    for line in out.splitlines():
        m = re.match(r"^(\./[^\s:]+)", line.strip())
        if m:
            found.add(m.group(1))
    return found


def test_both_engines_return_the_same_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rtk_real_grep: _RtkCalls
) -> None:
    """Which backend ran must never change the answer — the failure mode this
    whole review is about is a guard wired at one call site."""
    _make_secret_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    delegated = supertool.op_grep("FAKE", ".", limit=100)
    assert rtk_real_grep.calls
    monkeypatch.setattr(supertool, "_RTK_PATH", None)
    native = supertool.op_grep("FAKE", ".", limit=100)
    assert _paths_in(delegated) == _paths_in(native)


def test_grep_count_mode_does_not_surface_secret_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_secret_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    out = supertool.op_grep("FAKE", ".", limit=100, count_only=True)
    assert ".env\n" not in out and "/.env:" not in out
    assert "server.pem" not in out


def test_grep_context_mode_does_not_surface_secret_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_secret_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    out = supertool.op_grep("FAKE", ".", limit=100, context=2)
    _assert_no_secret(out)


# ---------------------------------------------------------------------------
# glob, map, tree
# ---------------------------------------------------------------------------


def test_glob_recursive_branch_hides_secret_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_secret_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    out = supertool.op_glob("**/.env*", no_auto_read=True)
    assert ".env.example" in out
    assert not re.search(r"\.env(\.production|\.local)?$", out, re.M)


def test_glob_branches_agree_with_each_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`glob:.env*` (glob.glob + post-filter) and `glob:**/.env*` (walk) are two
    implementations of one op. They disagreed: the walk branch never filtered
    files at all."""
    _make_secret_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    flat = {os.path.basename(p) for p in supertool._glob_files(
        ".env*", supertool._get_exclude_paths("glob"))}
    walked = {os.path.basename(p) for p in supertool._glob_files(
        "**/.env*", supertool._get_exclude_paths("glob"))}
    assert flat == walked


def test_glob_pem_and_key_are_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_secret_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    out = supertool.op_glob("**/*", no_auto_read=True)
    for name in ("server.pem", "tls.key", "bundle.p12", "id_rsa", "id_ed25519"):
        assert name not in out


def test_tree_hides_secret_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_secret_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    out = supertool.op_tree(".", 3, supertool._get_exclude_paths("tree"))
    assert "server.pem" not in out
    assert "id_rsa" not in out
    assert "app.py" in out


def test_map_hides_secret_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_secret_tree(tmp_path)
    (tmp_path / "keys.py").write_text("def f(): pass\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    # `.pem`/`.key` are not map extensions, so map was safe here by accident.
    # Pin the file filter itself with an entry that names a mappable file.
    excl = supertool._get_exclude_paths("map") + ("keys.py/",)
    files = supertool._collect_files(".", excl)
    assert not any(os.path.basename(f) == "keys.py" for f in files)
    assert any(os.path.basename(f) == "app.py" for f in files)


# ---------------------------------------------------------------------------
# The boundary: what must stay visible, and how to get it back
# ---------------------------------------------------------------------------


def test_env_example_and_sample_stay_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A committed placeholder is a file people legitimately grep. An exclusion
    list that hides it is the over-broad direction of this defect."""
    _make_secret_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    out = supertool.op_grep("FAKE", ".", limit=100)
    assert "FAKE_example_placeholder" in out
    assert "FAKE_sample_placeholder" in out


def test_no_exclude_still_shows_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`no-exclude` means "show me everything" and must keep meaning that."""
    _make_secret_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    out = supertool.op_grep("FAKE", ".", limit=100, no_exclude=True)
    assert "FAKE_root_env_value" in out
    assert "FAKE_pem_value" in out


def test_naming_the_file_explicitly_still_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parity with `read`: an excluded path you name yourself is still searched.
    Blocking it would buy nothing — `read:.env` was never gated — and would
    break the deliberate case."""
    _make_secret_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    out = supertool.op_grep("FAKE", ".env", limit=10)
    assert "FAKE_root_env_value" in out


# ---------------------------------------------------------------------------
# Disclosure — a silent hide is this repo's own defect class
# ---------------------------------------------------------------------------


def test_grep_report_discloses_how_many_files_were_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_secret_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    out = supertool.op_grep("FAKE", ".", limit=100)
    header = out.splitlines()[0]
    assert "hidden by exclude-paths" in header
    m = re.search(r"(\d+) files hidden by exclude-paths", header)
    assert m and int(m.group(1)) == len(SECRET_FILES) + 1  # + sub/.env


def test_a_gitfile_is_hidden_without_being_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In a git *worktree* `.git` is a gitfile, not a directory — so a counter
    keyed on file-versus-directory fired `1` on every call in the tree, about a
    pointer nobody searched for. The line is noise versus credential."""
    (tmp_path / ".git").write_text(
        "gitdir: /repo/.git/worktrees/wt\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("FAKE_ordinary\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    out = supertool.op_grep("FAKE", ".", limit=10)
    assert ".git" not in out.replace("app.py", "")
    assert "hidden" not in out

    # ...and a credential alongside it still counts.
    (tmp_path / ".env").write_text("K=FAKE_env\n", encoding="utf-8")
    out = supertool.op_grep("FAKE", ".", limit=10)
    assert "1 files hidden by exclude-paths" in out


def test_a_project_configured_exclusion_always_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """We cannot know whether a project's own entry is noise or a credential,
    over-disclosure is the safe direction, and whoever added the pattern is the
    person most likely to want to know it fired."""
    (tmp_path / "generated.py").write_text("FAKE_gen\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("FAKE_ordinary\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    monkeypatch.setattr(supertool, "_CONFIG", {
        "ops": {"grep": {"exclude-paths": ["generated.py"]}}
    })
    out = supertool.op_grep("FAKE", ".", limit=10)
    assert "1 files hidden by exclude-paths" in out


def test_no_disclosure_when_nothing_was_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The counter must not become noise on the ordinary call."""
    (tmp_path / "a.py").write_text("FAKE_ordinary\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    out = supertool.op_grep("FAKE", ".", limit=10)
    assert "hidden" not in out


def test_glob_report_discloses_how_many_files_were_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_secret_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    out = supertool.op_glob("**/.env*", no_auto_read=True)
    assert re.search(r"\d+ files hidden by exclude-paths", out)


def test_tree_discloses_how_many_files_were_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_secret_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    out = supertool.op_tree(".", 3, supertool._get_exclude_paths("tree"))
    assert re.search(r"\d+ files hidden by exclude-paths", out)


# ---------------------------------------------------------------------------
# _is_excluded: glob entries and negations
# ---------------------------------------------------------------------------


class TestIsExcludedOnFiles:
    def test_trailing_slash_entry_matches_a_file_of_that_name(self) -> None:
        """The promise the comment at `_DEFAULT_EXCLUDE_PATHS` already made."""
        assert supertool._is_excluded(".env", (".env/",))
        assert supertool._is_excluded("sub/.env", (".env/",))

    def test_glob_entry_matches_on_basename(self) -> None:
        assert supertool._is_excluded("certs/server.pem", ("*.pem",))
        assert supertool._is_excluded("id_rsa", ("id_rsa*",))
        assert supertool._is_excluded("id_rsa.pub", ("id_rsa*",))
        assert not supertool._is_excluded("src/pem.py", ("*.pem",))

    def test_negation_rescues_a_globbed_name(self) -> None:
        excl = (".env.*", "!.env.example")
        assert supertool._is_excluded(".env.production", excl)
        assert not supertool._is_excluded(".env.example", excl)

    def test_defaults_cover_the_credential_shapes(self) -> None:
        excl = supertool._get_exclude_paths("grep")
        for name in SECRET_FILES:
            assert supertool._is_excluded(name, excl), name
        for name in VISIBLE_FILES:
            assert not supertool._is_excluded(name, excl), name


class TestConfigGlobEntries:
    def test_a_wildcard_entry_from_config_actually_matches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A glob in `exclude-paths` used to be a silent no-op: the loader
        appended `/` to it, producing `*.secret/`, which matches nothing."""
        monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
        monkeypatch.setattr(supertool, "_CONFIG", {
            "ops": {"grep": {"exclude-paths": ["*.secret"]}}
        })
        excl = supertool._get_exclude_paths("grep")
        assert supertool._is_excluded("cfg/prod.secret", excl)

    def test_a_negation_from_config_reaches_is_excluded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
        monkeypatch.setattr(supertool, "_CONFIG", {
            "ops": {"grep": {"exclude-paths": ["!server.pem"]}}
        })
        excl = supertool._get_exclude_paths("grep")
        assert not supertool._is_excluded("certs/server.pem", excl)


class TestGrepExcludeFlags:
    def test_literal_noise_entries_produce_both_file_and_dir_flags(self) -> None:
        """`--exclude-dir` alone cannot skip a *file* of that name, which is
        what let the delegated engine read `.env` off disk at all."""
        flags = supertool._grep_exclude_flags((".git/",))
        assert "--exclude-dir=.git" in flags
        assert "--exclude=.git" in flags

    def test_literal_secret_entries_send_only_the_dir_flag(self) -> None:
        """`--exclude=.env` also hides the file from the post-filter, so the
        report could not say it had hidden anything (#764). See
        tests/test_delegated_grep_disclosure_764.py."""
        flags = supertool._grep_exclude_flags((".env/",))
        assert "--exclude-dir=.env" in flags
        assert "--exclude=.env" not in flags

    def test_multi_segment_entries_are_not_sent(self) -> None:
        flags = supertool._grep_exclude_flags(("Dvsi/libs/",))
        assert flags == []

    def test_wildcards_are_withheld_when_a_negation_exists(self) -> None:
        """System grep cannot express `!.env.example`, so a `--exclude=.env.*`
        would make the delegated engine hide a file the native walker shows.
        Those entries are left to the post-filter instead."""
        flags = supertool._grep_exclude_flags((".env.*", "!.env.example", ".git/"))
        assert not any(".env.*" in f for f in flags)
        assert "--exclude-dir=.git" in flags


# ---------------------------------------------------------------------------
# Whose exclusion is worth disclosing
#
# The count is the entire justification for hiding a file silently: a `*.pem`
# in a fixtures directory is survivable *because* the header says something
# was dropped. That justification holds only while the number discriminates.
#
# In a git **worktree**, `.git` is a gitfile rather than a directory — so a
# noise entry that is a directory everywhere else becomes a file, and the
# counter read >= 1 on every call in the tree, about a pointer file nobody was
# looking for. A reader learns to skip a number that is never zero, and the
# call where it says `2` because a real `.env` was hidden then looks exactly
# like the five hundred where it said `1` about a git pointer.
#
# `_hidden_suffix` already made this argument, for directories: a counter that
# is never zero is noise, not disclosure. Files-vs-directories was a *proxy*
# for the real distinction, which is noise-vs-credential; the proxy holds only
# because almost every noise entry happens to be a directory. This is where it
# breaks. Nothing below hides `.git` any less — it stays out of the result.
# ---------------------------------------------------------------------------


@pytest.fixture
def native_grep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the native walker, which is the engine that has a counter.

    The delegated path reports `scanned ? files - delegated to rtk` and no
    hidden clause at all: when grep's own `--exclude` does the hiding,
    supertool never learns how many files it skipped. That is a real and
    separate gap, reported rather than fixed here. Pinning the counter means
    pinning the engine that has one.
    """
    monkeypatch.setattr(supertool, "_RTK_CHECKED", True)
    monkeypatch.setattr(supertool, "_RTK_PATH", None)


def _write_gitfile(root: Path) -> None:
    """Exactly what `git worktree add` leaves behind: `.git` as a FILE."""
    (root / ".git").write_text(
        "gitdir: /elsewhere/.git/worktrees/wt\n", encoding="utf-8")


def test_a_worktree_gitfile_is_still_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_grep: None
) -> None:
    """Not hiding it less — it must stay out of the result."""
    _write_gitfile(tmp_path)
    (tmp_path / "app.py").write_text("gitdir = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    out = supertool.op_grep("gitdir", ".", limit=10)
    assert "/elsewhere/" not in out
    assert "app.py" in out


def test_a_worktree_gitfile_is_not_counted_as_a_hidden_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_grep: None
) -> None:
    """A noise entry must not spend the counter's credibility."""
    _write_gitfile(tmp_path)
    (tmp_path / "app.py").write_text("gitdir = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    out = supertool.op_grep("gitdir", ".", limit=10)
    assert "hidden by exclude-paths" not in out


def test_the_count_reports_credential_files_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_grep: None
) -> None:
    """One `.env` and one worktree gitfile hidden. The header says 1, not 2."""
    _write_gitfile(tmp_path)
    (tmp_path / ".env").write_text("TOK=FAKE_root_env_value\n", encoding="utf-8")
    (tmp_path / "app.py").write_text(
        'X = "FAKE_source_value"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    out = supertool.op_grep("FAKE", ".", limit=10)
    assert "FAKE_root_env_value" not in out
    m = re.search(r"(\d+) files hidden by exclude-paths", out)
    assert m and int(m.group(1)) == 1


def test_a_real_git_worktree_does_not_inflate_the_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_grep: None
) -> None:
    """The case that shipped past two reviews, because it cannot reproduce in
    a normal clone: only `git worktree add` writes `.git` as a file."""
    if not shutil.which("git"):
        pytest.skip("git unavailable")
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
        "GIT_CONFIG_SYSTEM": os.devnull,
    }

    def _git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, env=env, check=True,
                       capture_output=True)

    _git("init", "-q", "-b", "main")
    _git("config", "user.email", "t@example.invalid")
    _git("config", "user.name", "t")
    (repo / "app.py").write_text(
        'X = "FAKE_source_value"\n', encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-qm", "init")
    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", "--detach", str(wt), "main")
    assert (wt / ".git").is_file(), "fixture is wrong — .git should be a gitfile"

    monkeypatch.chdir(wt)
    out = supertool.op_grep("FAKE", ".", limit=10)
    assert "FAKE_source_value" in out
    assert "hidden by exclude-paths" not in out


def test_tree_counts_a_credential_file_but_not_a_noise_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`tree` never lists dotfiles, so the gitfile cannot reach it. The rule
    still has to hold there, pinned with a noise entry that is not a dotfile:
    `build/` is on the list, and a FILE named `build` is an ordinary thing."""
    (tmp_path / "build").write_text("stamp\n", encoding="utf-8")
    (tmp_path / "server.pem").write_text("FAKE_pem_value\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    out = supertool.op_tree(".", 3, supertool._get_exclude_paths("tree"))
    assert "server.pem" not in out and "build" not in out
    assert "app.py" in out
    m = re.search(r"(\d+) files hidden by exclude-paths", out)
    assert m and int(m.group(1)) == 1


def test_glob_does_not_count_a_worktree_gitfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_gitfile(tmp_path)
    (tmp_path / ".env").write_text("TOK=FAKE_root_env_value\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    out = supertool.op_glob(".*", no_auto_read=True)
    m = re.search(r"(\d+) files hidden by exclude-paths", out)
    assert m and int(m.group(1)) == 1


class TestDisclosableExclusion:
    def test_a_noise_entry_is_excluded_but_not_disclosable(self) -> None:
        excl = supertool._get_exclude_paths("grep")
        assert supertool._is_excluded(".git", excl)
        assert not supertool._is_disclosable_exclusion(".git", excl)

    def test_a_credential_entry_is_disclosable(self) -> None:
        excl = supertool._get_exclude_paths("grep")
        for name in (".env", ".netrc", "certs/server.pem", ".hashnode-token"):
            assert supertool._is_disclosable_exclusion(name, excl), name

    def test_every_built_in_noise_default_is_non_disclosable(self) -> None:
        excl = supertool._get_exclude_paths("grep")
        for entry in supertool._NOISE_EXCLUDE_PATHS:
            name = entry.rstrip("/")
            assert not supertool._is_disclosable_exclusion(name, excl), name

    def test_every_built_in_secret_default_is_disclosable(self) -> None:
        """The other direction: nothing on the credential half may be silent."""
        excl = supertool._get_exclude_paths("grep")
        for entry in supertool._SECRET_EXCLUDE_PATHS:
            if entry.startswith("!"):
                continue
            name = entry.rstrip("/").replace("*", "x")
            assert supertool._is_disclosable_exclusion(name, excl), entry

    def test_a_project_config_entry_is_disclosable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """We cannot know whether a user's own entry is noise or a credential.
        Over-disclosure is the safe direction, and the person who added the
        pattern is the one most likely to want to know when it fires."""
        monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
        monkeypatch.setattr(supertool, "_CONFIG", {
            "ops": {"grep": {"exclude-paths": ["*.p8"]}}
        })
        excl = supertool._get_exclude_paths("grep")
        assert supertool._is_disclosable_exclusion("keys/auth.p8", excl)

    def test_a_negated_file_is_not_disclosable(self) -> None:
        """It was never hidden, so there is nothing to disclose."""
        excl = supertool._get_exclude_paths("grep")
        assert not supertool._is_disclosable_exclusion(".env.example", excl)
