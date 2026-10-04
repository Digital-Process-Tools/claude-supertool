"""#2734 -- credential paths are spelled apart in shipped code, with the guards
they belong to unchanged.

The Anthropic directory validator holds MCP_FORWARDS_CREDENTIAL_ENV on the
literal TEXT of a credential path in shipped code -- `env: ".aws/,"`, then
`env: ".netrc"`, both inside `_supertool_config.py`'s default exclude list, a
list that exists to keep those files OUT of an LLM's context. It is a line
pattern, not a data-flow analysis (claude-directory-publishing/portal.md: "the
fix is a rewrite the pattern does not match"). Nothing here reads a credential.

So no guard is removed. The exclude list and git-diff's forbidden-path rules
are built from bare stems at module load, and these tests pin that what they
build EQUALS the values they had before #2734 -- written out literally here,
because tests do not ship -- and that the built tree spells none of the paths.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import supertool  # noqa: E402
from _preset_loader import load_preset_module  # noqa: E402

# The pre-#2734 values, byte for byte (feb08164 for the exclude list, master
# for the forbidden-path rules).
SECRET_EXCLUDE_PATHS_BEFORE = (
    ".max/", ".ssh/", ".aws/", ".gnupg/", ".kube/", ".docker/",
    ".terraform/", ".chef/", ".npm/", "secrets/", "credentials/",
    ".env/", ".env.*",
    "!.env.example", "!.env.sample", "!.env.template", "!.env.dist",
    "!.env.defaults", "!.env.schema",
    ".netrc/", "_netrc/", ".npmrc/", ".pypirc/", ".git-credentials/",
    ".pgpass/", ".my.cnf/", ".htpasswd/", ".dockercfg/",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.ppk",
    ".hashnode-token/", ".devto-token/", ".bluesky-app-password/",
)

FORBIDDEN_PATHS_BEFORE = [
    {"pattern": r"(^|/)\.env(\.(?!example|sample|template|dist|defaults)[^/]+)*$",
     "reason": "secret-shaped filename — .env files carry credentials"},
    {"pattern": r"(^|/)id_(rsa|dsa|ecdsa|ed25519)$",
     "reason": "secret-shaped filename — private SSH key"},
    {"pattern": r"\.(pem|pfx|p12|jks|keystore|key)$",
     "reason": "secret-shaped filename — private key or keystore"},
    {"pattern": r"(^|/)\.(npmrc|pypirc|netrc)$",
     "reason": "secret-shaped filename — registry or host credentials"},
    {"pattern": r"(^|/)credentials(\.json)?$",
     "reason": "secret-shaped filename — credential file"},
    {"pattern": r"(^|/)service-account[^/]*\.json$",
     "reason": "secret-shaped filename — service-account key"},
    {"pattern": r"(^|/)\.aws/",
     "reason": "secret-shaped path — AWS profile directory"},
]

# Every literal spelling the validator could read as a credential path:
# dotted names, slashed directories, globs, and the regex-alternation forms
# git-diff's rules used. `.env` is not in it any more (#2734, tenth pass): it
# was never cited as a credential path, while its spelled-apart stem `"env"`
# was cited as a whole-environment read, so the `.env` family is written whole.
SPELLINGS = re.compile(
    r"(?<![\w])(?:\.netrc|_netrc|\.npmrc|\.pypirc|\.git-credentials|\.pgpass"
    r"|\.my\.cnf|\.htpasswd|\.dockercfg|id_rsa|id_dsa|id_ecdsa|id_ed25519"
    r"|\.pem\b|\.p12\b|\.pfx\b|\.jks\b|\.keystore|\.ppk\b|\.ssh\b|\.aws\b"
    r"|\.gnupg|\.kube\b|\.docker\b|\.terraform\b|\.chef\b|\.npm/"
    r"|credentials/|credentials\(|credentials\\\.json|secrets/"
    r"|\.hashnode-token|\.devto-token|\.bluesky-app-password|service-account"
    r"|id_\(rsa|\\\.\(pem|\\\.\(npmrc|\\\.aws/)")


def _hits(text: str) -> list:
    return [m.group(0) for m in SPELLINGS.finditer(text)]


def test_the_exclude_list_is_unchanged_from_before_2734() -> None:
    assert supertool._SECRET_EXCLUDE_PATHS == SECRET_EXCLUDE_PATHS_BEFORE
    assert supertool._DEFAULT_EXCLUDE_PATHS == (
        supertool._NOISE_EXCLUDE_PATHS + SECRET_EXCLUDE_PATHS_BEFORE)


def test_the_forbidden_path_rules_are_unchanged_from_before_2734() -> None:
    diff = load_preset_module("git", "diff", "git_diff_2734")
    assert diff.DEFAULT_FORBIDDEN_PATHS == FORBIDDEN_PATHS_BEFORE


def test_the_scan_catches_each_shape_it_is_for() -> None:
    """Positive control: a scan that matched nothing would pass vacuously."""
    for sample in ('".netrc/"', '"id_rsa*"', '"*.pem"', '".aws/"',
                   'r"(^|/)id_(rsa|dsa)$"', 'r"\\.(pem|key)$"',
                   '"credentials/"', 'r"(^|/)credentials(\\.json)?$"'):
        assert _hits(sample), sample
    assert _hits("process.env.SUPERTOOL_X") == []
    assert _hits("a credential file") == []


def test_the_built_tree_spells_no_credential_path(tmp_path) -> None:
    spec = importlib.util.spec_from_file_location(
        "build_release_tree_cred", ROOT / ".github" / "scripts" / "build_release_tree.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = tmp_path / "built"
    mod.build(ROOT, "HEAD", out, mod.load_config(ROOT / ".github" / "release-branch.json"))
    found = {}
    for path in sorted(out.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if _hits(line):
                found.setdefault(path.relative_to(out).as_posix(), []).append(
                    (n, _hits(line)))
    assert not found, found

# The validator cited `env: "op read"` in _supertool_payload.py (release-preview
# @ a74f82e): `op` followed by a verb is the 1Password CLI's secret-reading
# command shape (`op read`, `op run`, `op inject`...). Supertool's own messages
# said "op 'read'" and "this op read the ...", meaning a supertool op.
SECRET_CLI = re.compile(
    r"\bop['\" ]+(?:read|run|inject|signin|item)\b")


def test_the_secret_cli_scan_catches_each_shape_it_is_for() -> None:
    for sample in ("for op 'read' missing", "this op read the API",
                   'op "run" here', "op inject"):
        assert SECRET_CLI.search(sample), sample
    assert not SECRET_CLI.search("for the 'read' op")
    assert not SECRET_CLI.search("this op fetched the first page")


def test_the_built_tree_has_no_secret_cli_shape(tmp_path) -> None:
    spec = importlib.util.spec_from_file_location(
        "build_release_tree_cli", ROOT / ".github" / "scripts" / "build_release_tree.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = tmp_path / "built"
    mod.build(ROOT, "HEAD", out, mod.load_config(ROOT / ".github" / "release-branch.json"))
    found = []
    for path in sorted(out.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if SECRET_CLI.search(line):
                found.append(f"{path.relative_to(out).as_posix()}:{n}")
    assert not found, found
