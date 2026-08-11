"""The repo a call is about, when it is not the one the cwd is standing in (#673).

`gh-*` read ops derived their target from the cwd's git remote and offered no
override, so a repo cloned elsewhere — or not cloned at all — was unreachable
through the ops even though `gh-issue-create` had taken a `repo` key in its
payload since it shipped. Core resolves the leading `repo:OWNER/NAME` op and
exports it here as ``SUPERTOOL_REPO``; every preset in the family reads it
through this module so the flag, the API-path substitution and the error
wording stay one decision rather than five copies.

Absence is a first-class answer: with no target set every function here returns
the empty/None form and the caller behaves exactly as it did before.
"""
from __future__ import annotations

import os
import urllib.parse

ENV_VAR = "SUPERTOOL_REPO"

GITHUB_HOST = "github.com"


def target() -> str | None:
    """``OWNER/NAME`` when this call named a repo, else None.

    Core validates the shape before exporting, so a value present here is
    already well-formed. A blank env var is read as absence, not as an empty
    target — an exported-but-empty variable is how a shell accident looks.
    """
    value = (os.environ.get(ENV_VAR) or "").strip()
    return value or None


def owner_repo() -> tuple[str, str] | None:
    """``(owner, name)`` split of the target, or None when there is none."""
    value = target()
    if not value or value.count("/") != 1:
        return None
    owner, name = value.split("/", 1)
    if not owner or not name:
        return None
    return owner, name


def gh_args() -> list[str]:
    """``["--repo", OWNER/NAME]`` for a gh subcommand, or ``[]``.

    Never for ``gh api``: that subcommand has no ``--repo``. Use
    :func:`api_path` there instead.
    """
    value = target()
    return ["--repo", value] if value else []


def api_path(suffix: str) -> str:
    """A ``gh api`` repo path, with the target substituted when there is one.

    ``gh api`` expands the literal ``{owner}`` / ``{repo}`` placeholders from
    the cwd's remote. That expansion is exactly what a repo target has to
    override, so the placeholders are replaced rather than accompanied.
    """
    pair = owner_repo()
    if pair is None:
        return f"repos/{{owner}}/{{repo}}/{suffix}"
    owner, name = pair
    return f"repos/{owner}/{name}/{suffix}"


def no_repo_error(cli_example: str) -> str:
    """The message for "gh could not work out which repo this is".

    Before #673 this said `cwd is not a GitHub repo`, which was a complete and
    honest answer while the cwd was the only way to name a repo. It is not any
    more, and the two cases have to be told apart:

    * **No target given** — the cwd really is the problem, and the message now
      names the second route as well as the first.
    * **A target was given** — the cwd is irrelevant. Blaming it would send the
      reader to fix something that is not broken while the real fault (a typo,
      or no access) goes unnamed.
    """
    value = target()
    if value:
        return (
            f"ERROR: repo target {value!r} could not be resolved by gh. "
            f"Check the spelling and your access: gh repo view {value}"
        )
    return (
        "ERROR: cwd is not a GitHub repo and no repo target was given. "
        "cd into a GitHub-cloned repo, name one with a leading repo: op "
        f"(./supertool 'repo:OWNER/NAME' '{cli_example}'), "
        "or run gh directly with --repo OWNER/REPO."
    )


def not_found_scope() -> str:
    """How to describe *where* something was not found, in the caller's sentence.

    Reads as "PR #265 not found **in this repo**" / "**in owner/name**". The
    cwd phrasing is wrong under a target for the same reason as above: it sends
    the reader to check a working directory that had no part in the lookup.
    """
    value = target()
    return f"in {value}" if value else "in this repo"


def not_found_hint() -> str:
    """The verification step that matches whichever scope was actually used."""
    value = target()
    if value:
        return f"Check the number, or the repo target (gh repo view {value})."
    return "Check the number or verify you're in the right repo (gh repo view)."


# ---------------------------------------------------------------------------
# the repo a *url* is about — one implementation, two callers (#907)
# ---------------------------------------------------------------------------


def is_github_host(host: str) -> bool:
    """True for ``github.com`` itself and for a ``.``-boundary subdomain of it.

    A suffix test is the defect this exists to prevent: ``endswith("github.com")``
    also accepts ``evilgithub.com`` and ``notgithub.com`` (#1180, CodeQL
    ``py/incomplete-url-substring-sanitization``). The boundary is the dot, and
    the dot is written out rather than implied. A trailing root dot is stripped,
    so the FQDN spelling ``github.com.`` is the same host.

    **GitHub Enterprise Server is deliberately not accepted** (#907, which asked
    for a decision rather than an assumption). A GHES install lives on an
    operator-chosen host — ``github.acme.example`` — which is not a subdomain of
    ``github.com``, so no loosening of this predicate reaches it; only
    configuration could. Nothing else in this repo supports GHES: ``gh``'s own
    ``GH_HOST`` is read nowhere, and the one operator-widenable host list
    (``SUPERTOOL_IMAGE_HOSTS`` in ``github/issue.py``) widens where *bytes* may
    be fetched from, not which repository an API call is aimed at. The
    subdomains this arm does admit are GitHub's own — ``www.``, ``api.``,
    ``gist.`` — and calling them "Enterprise" was wrong in two comments and one
    test docstring until #907. If GHES is ever wanted, it belongs in config next
    to ``SUPERTOOL_IMAGE_HOSTS``, not in a widened literal here.
    """
    host = (host or "").lower().rstrip(".")
    return host == GITHUB_HOST or host.endswith("." + GITHUB_HOST)


def url_host(url: str) -> str:
    """The lowercased host of *url*, ``""`` when it has none or cannot be parsed.

    ``urlsplit`` rather than ``url.split("/")[2]``, because the authority is not
    the host: it may carry ``user@`` and ``:port``, and a ``#`` or ``?`` can end
    it early. ``https://github.com@evil.example/o/r`` has host ``evil.example``
    — index arithmetic read the whole ``github.com@evil.example`` as the host,
    which happens to be refused, but refuses a genuine ``https://user@github.com/o/r``
    with it. A malformed authority (``https://[::1/x``) makes ``urlsplit`` raise
    ``ValueError``; that is an unparseable url, not a crash for the caller.
    """
    try:
        return (urllib.parse.urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return ""


def github_owner_repo(url: str) -> tuple[str, str] | None:
    """``(owner, name)`` from a GitHub url, or None when *url* yields neither.

    The first two path segments, taken from the parsed path so a ``#`` or ``?``
    ends it: ``https://github.com/o#x/r/issues/1`` names the repo ``o``, not the
    repo ``o#x`` that splitting the raw string on ``/`` reported — a repository
    nobody named, on a real host, with nothing downstream having any reason to
    doubt it.
    """
    try:
        parts = urllib.parse.urlsplit(url or "")
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if not is_github_host(host):
        return None
    segments = [part for part in parts.path.split("/") if part]
    if len(segments) < 2:
        return None
    return segments[0], segments[1]
