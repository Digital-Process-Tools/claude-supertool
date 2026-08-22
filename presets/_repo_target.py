"""The repo a call is about, when it is not the one the cwd is standing in (#673).

`gh-*` read ops derived their target from the cwd's git remote and offered no
override, so a repo cloned elsewhere — or not cloned at all — was unreachable
through the ops even though `gh-issue-create` had taken a `repo` key in its
payload since it shipped. Core resolves the leading `repo:OWNER/NAME` op and
exports it here as ``SUPERTOOL_REPO``; every preset in the family reads it
through this module so the flag, the API-path substitution and the error
wording stay one decision rather than five copies.

#676 added the GitLab half below. It is not the same fix wearing the same name:
`glab api` has no repo flag at all, so the target is substituted *into* the
`projects/:id` path segment rather than appended beside it, and a GitLab target
may carry subgroups (`group/subgroup/project`) where a GitHub one may not.

Absence is a first-class answer: with no target set every function here returns
the empty/None form and the caller behaves exactly as it did before.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _st_hint  # noqa: E402  (a runnable invocation, not a relative path that may not exist — #905)

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


#: What a GitHub `owner/name` may be made of. Used only to decide whether a
#: slug read back off the API is fit to be pasted into a printed command; a
#: slug that does not match is not repaired, it is declined.
_SLUG = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")


def api_path_for_display(suffix: str, slug: str) -> str:
    """:func:`api_path`, with a slug the caller has already read filled in.

    **For a path that is printed for a human to paste, never for argv.** The two
    are different consumers of one string and #1670 is the third time in this
    codebase they were treated as one. `gh api` expands `{owner}`/`{repo}` from
    the cwd's remote, which is right for a command this process runs *here* and
    wrong for a command handed to a reader: pasted in another checkout the same
    line names another repository and says nothing about having changed meaning.
    The receipt around it already prints the concrete slug twice, in its own
    header and in its `URL:` line.

    A repo target still wins, and is still *replaced rather than accompanied*
    (#1281): with a target set this returns exactly what :func:`api_path`
    returns, so the printed line and the executed one cannot disagree about
    which repository the call is about. `slug` is the cwd's identity and is not
    that answer.

    A `slug` that is empty or not `owner/name` is declined rather than pasted
    in — the placeholders are a correct command, and a path built out of a
    partial answer is not.
    """
    if owner_repo() is not None or not _SLUG.fullmatch(slug or ""):
        return api_path(suffix)
    return f"repos/{slug}/{suffix}"


def cwd_slug(timeout: int = 15) -> str:
    """``OWNER/NAME`` for the repository the cwd is standing in, ``""`` if unknown.

    The other half of :func:`api_path_for_display`, which #1670 shipped without
    (#1679): it takes a slug the caller "has already read", and outside
    `pr_merge` no caller had one. `gh-check` and `gh-job` print four `gh api`
    commands for a reader to paste and had no way to name a repository in them.

    Empty on every failure — gh missing, not logged in, not a repository, a
    timeout, unparseable JSON. That is not a swallowed error: the *caller* is
    `api_path_for_display`, whose documented answer to a slug it cannot use is
    to print gh's own placeholders, which are a correct command. There is no
    third thing to report here, and an exception out of a helper that exists to
    decorate an error message would replace the message the reader needed.

    A subprocess in a module that had none: this is where the question "which
    repository is this call about" already lives, and the alternative was a
    sixth hand-rolled copy of `gh repo view --json nameWithOwner`.

    **Not the function a caller printing a header wants** — see
    :func:`effective_slug`, which #1701 added after re-deriving the five copies
    this docstring used to list. Three of them were migrated there; the other
    two ask for a second field in the same call and are not copies of this.
    """
    if target():
        # The target is the answer, and reading the cwd would not improve it.
        return ""
    try:
        r = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner"],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    if r.returncode != 0:
        return ""
    try:
        data = json.loads(r.stdout)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("nameWithOwner") or "")


def effective_slug(timeout: int = 15) -> str:
    """``OWNER/NAME`` this call is *about* — the target, else the cwd's, else ``""``.

    The target-first sibling of :func:`cwd_slug`, and the difference between
    them is the whole of #1701. `cwd_slug` answers *which repository is this
    directory*, and under a `repo:` target it deliberately answers ``""``: its
    caller :func:`api_path_for_display` substitutes the target itself, so
    reading the cwd there would put the wrong repository into a command printed
    for a human to paste. A caller that wants a slug for a **header** wants the
    opposite precedence, because the target is the repository the call was made
    about. So #1701 could not be closed by adopting `cwd_slug` at the five
    sites it named; it needed this.

    Three sites use it — `github/labels.py`, `watch/tiers/gh_prs.py` and
    `claims/check.py` — and all three had already spelled it by hand as
    `target() or <gh repo view>`. The other two the issue counted are not this
    question: `github/branch.py::_repo_identity` and
    `github/pr_merge.py::_repo_identity` read `nameWithOwner,defaultBranchRef`
    in one call and return a third element carrying the error their `main()`
    aborts on, so they already tell *could not ask* from *asked, got nothing*.
    Handing them a two-state slug would trade a three-state answer for a
    two-state one, which is this repo's own defect class arriving through a
    cleanup. They keep their own reader, and that is why the count is three.

    Two states here, not three, on evidence rather than on brevity: **no caller
    of this consumes a reason.** Each keeps its own absence sentinel at its own
    boundary — ``""``, ``"?"``, ``None`` — which is what each rendered before
    #1701 and after it. A third state nobody reads is a render decision made in
    the wrong module.
    """
    return target() or cwd_slug(timeout)


def api_path_printable(suffix: str, timeout: int = 15) -> str:
    """:func:`api_path_for_display` with the slug read back here.

    The one call for a `gh api` path that is **printed for a human to paste**.
    Under a `repo:` target no call is made and the target is substituted, so
    the printed line and the executed one cannot disagree; with no target the
    cwd's own slug is read and filled in; and when that read does not answer,
    :func:`api_path_for_display` declines to gh's placeholders.

    The read is not cached. Every call site is a terminal error path that runs
    at most once per invocation, and a cache keyed on nothing would have to be
    invalidated by a `chdir` this module cannot observe.
    """
    if owner_repo() is not None:
        return api_path(suffix)
    return api_path_for_display(suffix, cwd_slug(timeout))


#: gh answered, and the answer was "there is no repository here". A
#: **measurement** — evidence about the cwd, or about the target.
ABSENT = "absent"
#: gh did not answer: missing, hung, unauthenticated, 503, unparseable. The
#: absence of a measurement, which is a different fact from :data:`ABSENT` and
#: is the whole of #1789.
UNKNOWN = "unknown"

#: Substrings of gh's own stderr that mean *asked, and there is no repository
#: here* rather than *could not ask*.
#:
#: **Deliberately narrower than the twelve classifying call sites**, not a copy
#: of them — `pr.py`, `issue.py`, `issues.py` (three), `run.py`, `job.py`,
#: `labels.py`, `prs.py`, `branch.py`, `check.py` (two). Every marker here
#: refines one of theirs, so the one caller that has NOT classified cannot
#: reach a thirteenth spelling of the verdict; but the refinement is strict.
#: `no git remotes` and `git remotes found` narrow their bare `git remotes`,
#: `could not determine base repository` narrows their `could not determine`,
#: and :data:`_ABSENT_TARGET` drops their broadest marker, `not found`,
#: altogether. Until #1807 this comment claimed the two lists held the same
#: phrases, which sent the next reader to widen the wrong one.
#:
#: The narrowing is load-bearing, because these are not the same kind of
#: classifier. A call site is an **ordered if-chain** over one lookup: its
#: `repo` arm is tried before `notfound`, `auth` and `ratelimit`, and it runs
#: only after the caller has turned a missing binary and a timeout into their
#: own messages (`run.py`'s `except FileNotFoundError`). Its broad phrases are
#: fenced by everything tried ahead of them. This is a **standalone two-way**
#: classifier with no ordering and nothing filtered out first, so the same
#: words carry a different risk: borrowing their bare `not found` would call
#: `gh not found - install from ...` ABSENT, which is #1789 reintroduced in
#: the module written to fix it.
#:
#: So the list stays short of a full taxonomy: anything unrecognised falls to
#: :data:`UNKNOWN`, because the two mistakes do not cost the same. Calling a
#: real non-repo `unknown` prints a hedged sentence; calling a 503 `absent`
#: prints a false claim about the reader's machine and sends them to fix
#: something that was never broken. A gh rewording that satisfies only a broad
#: marker therefore lands on `unknown` here and `absent` at a call site — a
#: real divergence, in the direction that asserts nothing false.
#:
#: `tests/test_repo_target_marker_vocabulary_1807.py` derives both
#: vocabularies from the tree and fails if these paragraphs stop being true.
_ABSENT_CWD = (
    "not a git repository",
    "no git remotes",
    "git remotes found",
    "github host",
    "could not determine base repository",
)
_ABSENT_TARGET = (
    "could not resolve to a repository",
    "http 404",
)

#: How much of gh's stderr is worth pasting into one error line.
_DETAIL_CAP = 200


def _one_line(text: str) -> str:
    """gh's stderr, reduced to something that can sit inside our own sentence.

    Not `_untrusted.flat`, on purpose: this module is stdlib-only and its tests
    load it by path before any preset has put `presets/` on `sys.path`, so
    importing a sibling here would be a new way for an error path to fail. What
    is needed is narrower than `flat` anyway — one line, no control characters,
    bounded length — because the text lands mid-sentence in a message the
    reader takes as the tool's, and a multi-line hint from gh must not be able
    to add a line of its own to it.

    **Where it lands is quoted with `!r`, and that is the other half of this.**
    Flattening bounds the text to one line but not to a known span within it:
    gh's own messages routinely contain brackets — `fatal: not a git repository
    (or any of the parent directories)` — so a parenthesised span cannot tell
    the reader where gh stops speaking and the tool resumes. `repr` escapes the
    quote it delimits with, so the span is unambiguous whatever gh sent, and
    the text after it is visibly the tool's again.
    """
    first = ""
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if stripped:
            first = stripped
            break
    cleaned = "".join(ch if ch.isprintable() else " " for ch in first).strip()
    if len(cleaned) > _DETAIL_CAP:
        cleaned = cleaned[:_DETAIL_CAP - 1].rstrip() + "…"
    return cleaned


def classify_detail(detail: str, slug: str | None = None) -> str:
    """:data:`ABSENT` or :data:`UNKNOWN` for an error string gh produced.

    `slug` picks the vocabulary. gh complains about a *directory* one way and
    about a *named repository* another, and reading a target's 404 against the
    cwd's markers would classify every misspelled target as `unknown`.
    """
    low = (detail or "").lower()
    markers = _ABSENT_TARGET if slug else _ABSENT_CWD
    return ABSENT if any(m in low for m in markers) else UNKNOWN


def no_repo_error(cli_example: str, detail: str | None = None) -> str:
    """The message for "gh could not work out which repo this is".

    Before #673 this said `cwd is not a GitHub repo`, which was a complete and
    honest answer while the cwd was the only way to name a repo. It is not any
    more, and the cases have to be told apart:

    * **No target given** — the cwd really is the problem, and the message now
      names the second route as well as the first.
    * **A target was given** — the cwd is irrelevant. Blaming it would send the
      reader to fix something that is not broken while the real fault (a typo,
      or no access) goes unnamed.
    * **gh did not answer at all** — #1789. Both sentences above are *factual
      claims about the reader's environment*, and a caller that reached here on
      any gh failure whatsoever makes this function assert one it cannot
      support. Observed downstream on 2026-08-17 during a GraphQL outage:
      `gh-pr-merge:1:squash|force` printed *cwd is not a GitHub repo* in a
      working clone of a real repository, and succeeded on the same op minutes
      later. The cost is not only the wrong sentence — the first message's
      third remedy, *run gh directly with --repo*, means raw `gh pr merge` for
      that op, which this repo's own guard refuses and which skips the leg
      reconciliation and the post-merge read-back. A blip must not push a
      maintainer off the audited path, so that remedy stays on the arm whose
      claim was measured.

    **`detail` is gh's own error, and the third arm is reachable only through
    it.** Not a lookup of this module's own: twelve of the thirteen call sites
    already match gh's stderr against the not-a-repo family *before* calling,
    so they arrive having measured `absent`, and a second lookup run here a
    second later could contradict a correct measurement with a fresher one —
    the same class of defect one layer up. They pass nothing and keep byte for
    byte the message they had. `pr_merge.py` is the thirteenth and the one
    #1789 was filed against: it reaches here on *any* failure of its identity
    read, holding the reason in `ident_err`, and used to drop it.

    #1701 asked whether `cwd_slug()` should carry a third state and decided
    against, because no caller read one — sound then, and still sound. The
    reader #1789 identified is this function, and it is a *different* lookup
    from `cwd_slug`'s: the callers hand it a result they already have.
    """
    value = target()
    state = classify_detail(detail, value) if detail is not None else ABSENT
    # An `unknown` never renders without a reason. A caller that passes an
    # empty string has still said "I could not ask" — that is the state, and
    # the blank is disclosed rather than turned into empty parentheses.
    why = _one_line(detail or "") or "gh gave no reason"
    if value:
        if state == UNKNOWN:
            return (
                f"ERROR: repo target {value!r} could not be checked — the "
                f"lookup did not answer ({why!r}). Whether the target is wrong "
                f"and whether gh could reach GitHub are both UNKNOWN from "
                f"here. Retry; if it persists: gh repo view {value}"
            )
        return (
            f"ERROR: repo target {value!r} could not be resolved by gh. "
            f"Check the spelling and your access: gh repo view {value}"
        )
    example = _st_hint.st_hint("repo:OWNER/NAME", cli_example)
    if state == UNKNOWN:
        return (
            f"ERROR: could not work out which GitHub repo this is — the "
            f"lookup did not answer ({why!r}). That is not the same as the cwd "
            f"not being a GitHub repo, and which of the two it is is UNKNOWN "
            f"from here. Retry; if it persists, check gh (gh auth status; "
            f"gh repo view), or name a repo with a leading repo: op "
            f"({example})."
        )
    return (
        "ERROR: cwd is not a GitHub repo and no repo target was given. "
        "cd into a GitHub-cloned repo, name one with a leading repo: op "
        f"({example}), "
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
# the GitLab half — a flag on the subcommands, a substitution in the API (#676)
# ---------------------------------------------------------------------------

#: What `glab api` expands from the cwd's remote. It is a whole path segment,
#: so the match is anchored on the segment boundary rather than on the prefix:
#: `projects/:idle/x` names a project called `:idle` and is not this.
GL_PROJECT_PLACEHOLDER = "projects/:id"


def gl_project() -> str | None:
    """The target as a url-encoded GitLab project id, or None.

    GitLab addresses a project by its **full path** — `group/subgroup/project`
    — and that path is one path *segment* of the API url, so every `/` inside
    it is percent-encoded. ``safe=""`` rather than the default ``safe="/"``,
    which is the whole point: the default would leave the separators alone and
    produce `projects/group/subgroup/project`, a route that does not exist.
    """
    value = target()
    if not value:
        return None
    return urllib.parse.quote(value, safe="")


def gl_args() -> list[str]:
    """``["-R", GROUP/PROJECT]`` for a glab subcommand, or ``[]``.

    ``glab issue view`` and ``glab mr view`` take ``-R``; ``glab api`` does
    not, which is the asymmetry #676 exists for. Use :func:`gl_api_path` there.
    """
    value = target()
    return ["-R", value] if value else []


def gl_api_path(path: str) -> str:
    """A `glab api` path with the target substituted into ``projects/:id``.

    Substituted, not accompanied: `:id` is glab's own placeholder for the
    cwd's project, so leaving it in place beside a target would mean the call
    still resolved from the directory it was made in — which is the behaviour
    the target exists to override.

    A path that does not begin with the placeholder segment is returned
    untouched. That is the honest answer rather than a convenience: this
    module cannot know where the project id sits in an arbitrary route, and
    guessing would rewrite a path the caller had already spelled correctly.
    """
    project = gl_project()
    if not project or not path.startswith(GL_PROJECT_PLACEHOLDER):
        return path
    rest = path[len(GL_PROJECT_PLACEHOLDER):]
    if rest and rest[0] not in "/?":
        return path
    return f"projects/{project}{rest}"


def gl_not_found_hint() -> str:
    """The verification step that matches whichever scope glab actually used.

    Same argument as :func:`not_found_hint` on the GitHub side: under a target
    the cwd had no part in the lookup, so "verify you're in the right repo"
    sends the reader to fix something that is not broken.
    """
    value = target()
    if value:
        return f"Check the number, or the repo target (glab repo view {value})."
    return "Check the number or verify you're in the right repo."


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
