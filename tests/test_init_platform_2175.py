"""`_init_platform` github/gitlab asymmetry is deliberate, not drift (#2175).

github is an exact host match (`host == "github.com"`) -- #1212 found an
earlier `endswith("github.com")` check matched `evilgithub.com`, and this
function's github branch is written tight for exactly that reason.

gitlab is a substring match (`"gitlab" in host`) on purpose: it mirrors
`presets/_remote_default.py`'s `origin_slug`, which needs substring
matching so a self-hosted instance like `gitlab.dp.tools` still resolves to
"gitlab". Tightening the gitlab branch to match github's exactness would
break that self-hosted case; loosening the github branch to match gitlab's
looseness would reopen #1212.

This test pins both behaviors so a future edit that makes the two branches
"consistent" -- in either direction -- goes red here instead of drifting
silently, which is the failure #2175 was filed to prevent.
"""
from __future__ import annotations

import supertool


def test_github_exact_match_only() -> None:
    assert supertool._init_platform("github.com") == "github"


def test_github_spoofed_host_refused() -> None:
    # The #1212 case: a substring/suffix match here would treat an
    # attacker-controlled host as github.com.
    assert supertool._init_platform("evilgithub.com") is None


def test_gitlab_com_matches() -> None:
    assert supertool._init_platform("gitlab.com") == "gitlab"


def test_gitlab_self_hosted_matches() -> None:
    # The case the substring convention exists to support -- a company's
    # own self-hosted GitLab instance, not gitlab.com itself.
    assert supertool._init_platform("gitlab.dp.tools") == "gitlab"


def test_gitlab_substring_is_the_documented_tradeoff() -> None:
    # Deliberately loose, per the docstring's cross-reference to
    # `origin_slug`: any host merely containing "gitlab" resolves to
    # "gitlab". This is not a bug to "fix" by tightening -- see #2175.
    assert supertool._init_platform("notgitlab.com") == "gitlab"


def test_unrecognised_host_is_none() -> None:
    assert supertool._init_platform("bitbucket.org") is None
