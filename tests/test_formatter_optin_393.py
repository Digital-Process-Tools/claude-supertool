"""#393 — a formatter only runs where the repo shows it opts in.

Editing a repo that has no prettier config used to come back with the whole
file reformatted, because the policy came from the *shell's* cwd rather than
from the repo holding the file.
"""
from pathlib import Path

import supertool


SPEC = {
    "cmd": "prettier --write {file}",
    "match": "*.md",
    "hooks_into": ["edit"],
}


def _repo(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / ".git").mkdir()
    return root


def test_no_config_means_no_reformat(tmp_path: Path) -> None:
    root = _repo(tmp_path, "no-prettier")
    f = root / "README.md"
    f.write_text("| a | b |\\n")
    assert not supertool._repo_opts_into_formatter("prettier", SPEC, str(f))


def test_config_file_opts_in(tmp_path: Path) -> None:
    root = _repo(tmp_path, "with-prettier")
    (root / ".prettierrc").write_text("{}\\n")
    f = root / "docs" / "x.md"
    f.parent.mkdir()
    f.write_text("x\\n")
    assert supertool._repo_opts_into_formatter("prettier", SPEC, str(f))


def test_manifest_mention_opts_in(tmp_path: Path) -> None:
    root = _repo(tmp_path, "manifest")
    (root / "package.json").write_text('{"prettier": {"semi": false}}')
    f = root / "x.md"
    f.write_text("x\\n")
    assert supertool._repo_opts_into_formatter("prettier", SPEC, str(f))


def test_search_stops_at_the_repo_root(tmp_path: Path) -> None:
    """A parent repo's config must not opt a nested repo in — that is the
    cross-repo leak the issue reported, one directory closer."""
    outer = _repo(tmp_path, "outer")
    (outer / ".prettierrc").write_text("{}\\n")
    inner = outer / "inner"
    inner.mkdir()
    (inner / ".git").mkdir()
    f = inner / "x.md"
    f.write_text("x\\n")
    assert not supertool._repo_opts_into_formatter("prettier", SPEC, str(f))


def test_explicit_env_standard_counts_as_opt_in(tmp_path: Path) -> None:
    """DVSI runs phpcbf with PSR12 and no phpcs.xml — the spec carries the rules."""
    root = _repo(tmp_path, "php")
    f = root / "Foo.php"
    f.write_text("<?php\\n")
    spec = {"cmd": "phpcbf {file}", "match": "*.php", "hooks_into": ["edit"],
            "env": {"PHPCBF_STANDARD": "PSR12"}}
    assert supertool._repo_opts_into_formatter("phpcbf", spec, str(f))


def test_unknown_tool_is_never_gated(tmp_path: Path) -> None:
    root = _repo(tmp_path, "custom")
    f = root / "x.md"
    f.write_text("x\\n")
    spec = dict(SPEC, cmd="my-house-style {file}")
    assert supertool._repo_opts_into_formatter("house-style", spec, str(f))


def test_requires_config_false_forces_always_run(tmp_path: Path) -> None:
    root = _repo(tmp_path, "forced")
    f = root / "x.md"
    f.write_text("x\\n")
    assert supertool._repo_opts_into_formatter(
        "prettier", dict(SPEC, requires_config=False), str(f))


def test_requires_config_names_its_own_marker(tmp_path: Path) -> None:
    root = _repo(tmp_path, "custom-marker")
    f = root / "x.md"
    f.write_text("x\\n")
    spec = dict(SPEC, requires_config=["house.toml"])
    assert not supertool._repo_opts_into_formatter("prettier", spec, str(f))
    (root / "house.toml").write_text("")
    assert supertool._repo_opts_into_formatter("prettier", spec, str(f))


def test_env_escape_hatch(tmp_path: Path, monkeypatch) -> None:
    root = _repo(tmp_path, "escape")
    f = root / "x.md"
    f.write_text("x\\n")
    monkeypatch.setenv("SUPERTOOL_FORMAT_WITHOUT_CONFIG", "1")
    assert supertool._repo_opts_into_formatter("prettier", SPEC, str(f))


def test_applicable_formatters_honours_the_gate(tmp_path: Path, monkeypatch) -> None:
    root = _repo(tmp_path, "applicable")
    f = root / "x.md"
    f.write_text("x\\n")
    monkeypatch.setattr(supertool, "_load_config", lambda: {"formatters": {"prettier": SPEC}})
    assert supertool._applicable_formatters("edit", str(f)) == {}
    (root / ".prettierrc").write_text("{}\\n")
    assert set(supertool._applicable_formatters("edit", str(f))) == {"prettier"}
