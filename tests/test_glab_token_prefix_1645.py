"""GitLab token prefixes: `glpat-`, not `glpat_` (#1645).

Four `_format_error` classifiers — `gitlab/{issue,mr,pipeline,job}.py` — tested a
glab error for ``glpat_``. GitLab mints ``glpat-``. So a glab failure that names a
real token prefix missed the "not authenticated" arm and got the generic relay
instead, which is the wrong sentence and also the one that echoes the remote's
text back.

**Why this file exists rather than a corrected literal in place.** The defect was
not the spelling, it was that the only check on it was a fixture written to agree
with it: `tests/test_security_glab.py`'s ``FAKE_TOKEN`` used ``glpat_`` too, so
the pair agreed with each other and with nothing outside the repo. Correcting
both literals reproduces that structure exactly.

So the prefixes below are quoted from **GitLab's own documentation** — the
"Security > Tokens" page, ``https://docs.gitlab.com/security/tokens/``, read
2026-08-14 — and this file is the one place in the repo that holds them as a
literal. Two independent consumers are then asserted against it: `presets/_secrets`
(which redacts them) and the four classifiers (which route on them). A literal is
genuinely unavoidable — nothing in CI can reach that page and no test can mint a
real token — but it is now singular, cited, dated, and load-bearing for two
subsystems, so the next wrong spelling fails a named test instead of agreeing
with itself.

The negative case is asserted too: the underscore form GitLab does not mint must
NOT reach the authentication arm, or a build that matched everything would pass.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    path = _ROOT / rel
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sys.path.insert(0, str(_ROOT / "presets"))
_secrets = _load("presets/_secrets.py", "supertool_secrets_1645")
_MODULES = {
    "issue": _load("presets/gitlab/issue.py", "gitlab_issue_1645"),
    "mr": _load("presets/gitlab/mr.py", "gitlab_mr_1645"),
    "pipeline": _load("presets/gitlab/pipeline.py", "gitlab_pipeline_1645"),
    "job": _load("presets/gitlab/job.py", "gitlab_job_1645"),
}

#: Every token prefix GitLab documents, read off
#: https://docs.gitlab.com/security/tokens/ on 2026-08-14. Thirteen of them,
#: every one hyphenated; there is no underscore form of any GitLab token.
#: `glpat-` covers personal, project, group and impersonation tokens.
DOCUMENTED_PREFIXES = (
    "glpat-",    # personal / project / group access, impersonation
    "gloas-",    # OAuth application secret
    "gldt-",     # deploy token
    "glrt-",     # runner authentication
    "glrtr-",    # runner authentication, created via a registration token
    "glcbt-",    # CI/CD job token
    "glptt-",    # pipeline trigger
    "glft-",     # feed
    "glimt-",    # incoming mail
    "glagent-",  # agent for Kubernetes
    "glwt-",     # workspace
    "glsoat-",   # SCIM OAuth
    "glffct-",   # feature flags client
)

#: A body long enough to satisfy the redactor's 16-character floor. Not a
#: credential and never was: no GitLab instance minted it.
_BODY = "FAKEfake0123456789xyz"


def _sample(prefix: str) -> str:
    return prefix + _BODY


# ---------------------------------------------------------------------------
# the repo's own prefix list, against the documentation
# ---------------------------------------------------------------------------

def test_the_shared_prefix_tuple_is_the_documented_one() -> None:
    """One list, and it is GitLab's.

    `_secrets` holds it because the redactor already needed it and is the
    consumer with the harshest failure mode. The classifiers read it from
    there rather than restating it — restating it is how four files came to
    disagree with the fifth.
    """
    assert set(_secrets.GITLAB_TOKEN_PREFIXES) == set(DOCUMENTED_PREFIXES)


@pytest.mark.parametrize("prefix", DOCUMENTED_PREFIXES)
def test_every_documented_prefix_is_redacted(prefix: str) -> None:
    """The redactor and the classifiers must recognise the same population.

    A prefix the classifier routes on but the redactor does not scrub is the
    worse half of this pair, so it is pinned per prefix rather than in bulk.
    """
    redacted, n = _secrets.redact(f"glab error: token {_sample(prefix)} rejected")
    assert n == 1, f"{prefix} was not recognised as a credential: {redacted}"
    assert _sample(prefix) not in redacted


def test_the_underscore_spelling_is_not_a_gitlab_prefix() -> None:
    """The typo, asserted as a typo.

    Without this the whole file passes against a build that matches any string
    at all, which is the property #1645 was filed about.
    """
    assert not _secrets.mentions_gitlab_token("glab: token glpat_FAKEfake0123 bad")
    assert _secrets.mentions_gitlab_token("glab: token glpat-FAKEfake0123 bad")


# ---------------------------------------------------------------------------
# and the four classifiers route on it
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("modname", sorted(_MODULES))
@pytest.mark.parametrize("prefix", DOCUMENTED_PREFIXES)
def test_a_token_prefix_takes_the_authentication_arm(modname: str,
                                                     prefix: str) -> None:
    """The stderr carries no other trigger word, so only the prefix can route it.

    `401`, `unauthorized`, `authenticate`, `bad token` and `token expired` are
    all absent deliberately: with any of them present the arm is reached
    whatever the prefix spelling is, and the test proves nothing.
    """
    mod = _MODULES[modname]
    result = mod._format_error(
        f"request rejected for {_sample(prefix)}", "resource", "7")
    assert "glab not authenticated" in result, (
        f"{modname}: {prefix} did not reach the authentication arm: {result!r}")
    assert _sample(prefix) not in result, (
        f"{modname}: the generic relay echoed the token back: {result!r}")


@pytest.mark.parametrize("modname", sorted(_MODULES))
def test_an_ordinary_failure_still_gets_the_generic_relay(modname: str) -> None:
    """The arm must stay narrow — an error with no credential marker and no
    auth wording is not an authentication failure, and calling it one sends
    the reader to `glab auth login` for a problem that is not there."""
    mod = _MODULES[modname]
    result = mod._format_error("connection reset by peer", "resource", "7")
    assert "glab not authenticated" not in result, result
