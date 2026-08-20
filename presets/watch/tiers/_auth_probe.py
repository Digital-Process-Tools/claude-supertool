"""Did the probe establish that there is no usable credential? (#1823)

Both radar tiers asked that question with the same predicate and got the same
answer wrong in the same way:

    if "not logged in" in err.lower() or "401" in err:
        raise ...("gh not authenticated. Run: gh auth login")

`"401" in err` is a bare three-character substring tested against the whole of
a CLI's stderr. GitHub puts those three characters in a user id
(``rate limit exceeded for user ID 44012345``), in a request id
(``[request-id: C401:1F2A:9B3D]``), in an epoch and in a byte count; GitLab
puts them in a correlation id. Every one of those failures rendered as *the
credential is gone*, which is the one reading with a printable remedy -- and a
maintainer loop that reads `gh auth login` has a documented action
(re-authenticate, interactive, outside the loop's authority) where the correct
action was to retry. #1823 caught it between two successful authenticated calls
seconds apart, and a bare re-run passed.

So the markers here match a **status**, never a number. ``http 401`` is what
`gh` and `glab` render when a request came back 401; ``401 unauthorized`` is
the status line with its reason phrase; ``unauthorized``, ``bad credentials``,
``bad token`` and ``token expired`` are prose a server or a CLI writes only
about a credential. A bare ``401`` is none of those, and the test that keeps it
out is structural rather than by example (`test_radar_auth_state_1823.py`),
because this is a list somebody widens later.

**One copy, for the same reason `_snapshot.py` is one copy.** `gh_prs` and
`gl_mrs` are deliberately parallel modules -- `gh_prs`'s own docstring argues
at length why generalising them would bend one platform's semantics to fit the
other's. What transfers is not the tier, it is this predicate: two hand-written
lists of what "not authenticated" looks like is how they drift, and they had
already drifted (`gh_prs` grew exit-code 4 and a transport whitelist in #1568;
`gl_mrs` still has neither).

What this module deliberately does NOT decide is what to do about a failure it
declines. Saying "this did not establish an auth problem" is the whole of its
job; whether the caller then calls that unreachable, throttled or a plain
product error stays in the tier, where the exit codes and the transport
vocabulary are platform-specific.
"""
from __future__ import annotations

#: Substrings that mean the probe got an answer establishing the credential is
#: unusable. Read on the failure path only, never on a success.
#:
#: Lowercase, and compared against a lowercased stderr -- `gh` writes
#: ``HTTP 401``, GitLab's API writes ``401 Unauthorized``, and a predicate that
#: cared about the difference would be one more thing to get wrong.
#:
#: **No entry here may be a bare status number.** That is the defect this
#: module exists for, and a marker list is exactly where it comes back.
NOT_AUTHENTICATED_MARKERS = (
    "not logged in",      # gh's and glab's own prose when they hold no token
    "http 401",           # the status line either CLI renders for a rejection
    "401 unauthorized",   # the same status with its reason phrase
    "unauthorized",       # GitLab's API body, and 401's reason phrase alone
    "bad credentials",    # GitHub's own body for a rejected token
    "bad token",
    "token expired",
)


def says_not_authenticated(stderr: str) -> bool:
    """Did this stderr *state* that the credential is unusable?

    False is not "the credential is fine". It is "this text did not establish
    an answer either way", which is the third state the caller owes its reader
    -- and the one the caller must not print a re-authentication remedy for.
    """
    low = (stderr or "").lower()
    return any(marker in low for marker in NOT_AUTHENTICATED_MARKERS)
