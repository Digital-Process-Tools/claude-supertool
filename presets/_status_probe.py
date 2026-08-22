"""Did the probe establish that the target is missing, or access denied? (#1864)

Filed by the #1846 lane as the class it stopped at the boundary of: #1823 and
#1846 fixed `"401" in err` -- three characters tested against the whole of a
CLI's stderr -- for the credential reading. The same shape is used for the
**not-found** and **permission** readings:

    if "404" in s or "not found" in s or "could not resolve" in s:
        return ...not found...
    if "403" in s or "forbidden" in s:
        return ...permission denied...

`404` and `403` sit inside a user id, a run id or a request URL exactly as
`401` does. `gh` renders a run's own API path into its stderr
(``.../actions/runs/12404999``), and go-gitlab writes the request URL into
every error string it constructs, so any project, job, pipeline or run id
carrying those digits made a server error or a throttle classify as *missing*
or *forbidden* -- a claim about the target's existence or the caller's access
that nothing established.

So the markers here match a **status**, never a number -- the same shape
`_auth_probe.py` uses for the credential reading. ``http 404`` and ``http
403`` are the bare status lines a CLI sometimes renders with no reason phrase
(`gh: HTTP 404` for a job's log blob, `gh: HTTP 403` for a scope error);
``not found`` and ``forbidden`` are the reason phrase either CLI or API
writes, alone or attached to the status (`404 Not Found`, `403 Forbidden`,
`404 Project Not Found`); ``could not resolve`` is `gh`'s and `glab`'s own
prose for a target that does not exist; ``permission denied`` is `glab`'s own
prose for an instance-wide read a project token cannot make. A bare ``404``
or ``403`` is none of those, and the test that keeps it out is structural
rather than by example (`test_status_classifier_status_not_number_1864.py`),
because this is a list somebody widens later -- the same argument
`_auth_probe.py` makes about its own markers, over the same evidence.

**One copy, for the same reason `_auth_probe.py` is one copy.** Eighteen
files under `presets/github/` and `presets/gitlab/` (plus
`presets/_declared_workflows.py`) carried a hand-written pair of these
predicates; a nineteenth hand-written copy is how the class comes back.

**Not folded into `_auth_probe.py`.** That module's docstring, its tests and
its callers are about one question -- is there a usable credential -- and its
`extra` parameter exists to keep GitLab's auth vocabulary out of GitHub's
markers. Not-found and forbidden are a different question with their own
vocabulary and no per-platform variance observed so far (unlike auth, where
`glab` writes prose `gh` does not). A second predicate, not a second tuple
inside `NOT_AUTHENTICATED_MARKERS`.

What this module deliberately does NOT decide is what a caller does with a
declined answer. Saying "this did not establish the target is missing" (or
"forbidden") is the whole of its job.
"""
from __future__ import annotations

#: Substrings that mean the probe got an answer establishing the target does
#: not exist. Read on the failure path only, never on a success.
#:
#: Lowercase, and compared against a lowercased stderr.
#:
#: **No entry here may be a bare status number.** That is the defect this
#: module exists for, and a marker list is exactly where it comes back.
NOT_FOUND_MARKERS = (
    "could not resolve",  # gh's and glab's own prose for a target that does not exist
    "not found",           # the reason phrase either CLI/API renders for a 404, alone or with the status
    "http 404",            # the bare status line a CLI sometimes renders with no reason phrase
)


#: Substrings that mean the probe got an answer establishing access is denied.
#: Same rules as `NOT_FOUND_MARKERS` above.
FORBIDDEN_MARKERS = (
    "forbidden",           # the reason phrase either CLI/API renders for a 403, alone or with the status
    "permission denied",   # glab's own prose for an instance-wide read a project token cannot make
    "http 403",            # the bare status line a CLI sometimes renders with no reason phrase
)


def says_not_found(stderr: str) -> bool:
    """Did this stderr *state* that the target does not exist?

    False is not "the target exists". It is "this text did not establish an
    answer either way" -- the third state the caller owes its reader, and the
    one the caller must not print a not-found remedy for.
    """
    low = (stderr or "").lower()
    return any(marker in low for marker in NOT_FOUND_MARKERS)


def says_forbidden(stderr: str) -> bool:
    """Did this stderr *state* that access to the target is denied?

    Same third-state contract as `says_not_found` above.
    """
    low = (stderr or "").lower()
    return any(marker in low for marker in FORBIDDEN_MARKERS)
