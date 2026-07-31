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

ENV_VAR = "SUPERTOOL_REPO"


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
