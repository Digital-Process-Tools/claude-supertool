"""Pattern-based detection of credentials in text nobody handed us (#760).

The three existing scrubbers in this repo — ``hashnode/_graphql.py::_scrub_token``
and its mirrors in ``devto/_rest.py`` and ``bluesky/_atproto.py`` — all take the
secret **as an argument**: they redact a value the process already holds. That is
the easy case and it is exact.

This module solves the other one. When you read back a transcript, a log, or a
diff, you do not know what the secrets were. There is nothing to compare
against, so detection has to be by shape — and detection by shape is a guess.

**Two rules follow from that, and they are the whole design:**

1. **Never claim safety.** ``disclosure()`` says what was looked for. It never
   says the text is now clean, because it is not — every pattern list has false
   negatives and this one certainly does. A confident "redacted" printed over a
   secret this file did not recognise would be worse than printing nothing.

2. **A false positive is not free either.** The consumer of this is
   ``claude-log``, whose entire job is showing you what you ran. Redacting a git
   SHA, or the session UUID the op is keyed on, would break the tool for its
   actual purpose. That is why there is **no entropy heuristic here**: session
   UUIDs, commit SHAs, base64 payloads and minified blobs are all high-entropy
   and all load-bearing. Entropy cannot tell them from a key.

So the line is drawn at **structure, not randomness**:

* **Known-prefix credentials** (``sk-ant-``, ``ghp_``, ``glpat-``, ``AKIA``…) —
  a vendor minted them with a distinctive prefix precisely so they could be
  recognised. Near-zero false-positive rate. This is the bulk of the value.
* **Positional shapes** — the token after ``Bearer``, the password in a
  ``user:pass@host`` URL, the body of a ``-----BEGIN … PRIVATE KEY-----`` block.
  The surrounding syntax, not the value, is what identifies them.
* **One name-gated assignment rule** — ``SOMETHING_TOKEN=value``, where the
  *name* contains a secret word and the *value* looks like a token rather than
  an English word. Noisier than the rest and deliberately the last resort.

Conceptually this is the cheap half of what a scanner like ``gitleaks`` does
(#668 proposes one as a validator): gitleaks' value is a large curated rule
corpus with per-rule entropy floors. Borrowing the *idea* of prefix-anchored
rules costs nothing; vendoring the corpus, or taking the dependency, is not
worth it for four ops that format a transcript. If #668 ever lands a real
scanner, this file is the seam to point at it.

Public API::

    redacted, n = redact(text)     # n = number of values replaced
    note = disclosure(n)           # "" when n == 0
"""
from __future__ import annotations

import re

#: Every replacement starts with this. Callers assert on it to prove a
#: redaction happened rather than merely that a secret was absent — absence
#: alone also holds when the op crashed.
MARKER_PREFIX = "[REDACTED:"


def _marker(label: str) -> str:
    return f"{MARKER_PREFIX}{label}]"


# ---------------------------------------------------------------------------
# Rules. Each is (label, compiled regex). The group named `secret` is what gets
# replaced — everything else in the match is context that stays, so the reader
# can still see *that* a curl carried an Authorization header, just not what.
# ---------------------------------------------------------------------------

#: Every vendor-prefix rule is wrapped in these. Without them the prefixes fire
#: INSIDE unrelated alphanumeric runs, which a sweep over 49M characters of real
#: transcript proved is not theoretical: `ASIA` matched inside a base64 payload
#: 21 times, and `sk-` matched inside the branch name `kevin-task-1771387649`.
#: `\\b` cannot help — base64 and identifiers are word characters throughout, so
#: there is no boundary to find. Explicit "not preceded/followed by alphanumeric"
#: is what actually anchors these.
_L = r"(?<![A-Za-z0-9])"
_R = r"(?![A-Za-z0-9])"


def _prefixed(body: str) -> str:
    """Anchor a vendor-prefix pattern so it cannot match mid-token."""
    return _L + r"(?P<secret>" + body + r")" + _R


#: Every token prefix GitLab documents, read off its own "Security > Tokens"
#: page (https://docs.gitlab.com/security/tokens/) on 2026-08-14. Thirteen, all
#: hyphenated -- GitLab mints no underscore form of any of them, which is what
#: #1645 was: four `gl-*` error classifiers tested for ``glpat_`` and the one
#: test over them used the same wrong spelling, so nothing disagreed.
#:
#: A tuple rather than a regex literal because two subsystems read it -- the
#: redaction rule below, and `mentions_gitlab_token` for the classifiers. Four
#: files restating a prefix list is how they came to disagree with the fifth.
#: `tests/test_glab_token_prefix_1645.py` holds the documented list and asserts
#: this against it; that file is the citation, this is the single definition.
GITLAB_TOKEN_PREFIXES: tuple[str, ...] = (
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

_GITLAB_PREFIX_ALT = "|".join(re.escape(p) for p in GITLAB_TOKEN_PREFIXES)

#: One pattern, used both as the redaction rule below and as the classifiers'
#: test. A credential is a prefix AND a body -- `gldt-`, `glft-`, `glwt-` and
#: `glrt-` are five characters, and a bare substring test over them reads
#: `gldt-staging.example.internal` and `projects/glft-report` as tokens. The
#: caller that would do so routes a `glab` failure to "run glab auth login"
#: and DISCARDS the remote's own message, so a loose test there does not fail
#: safe: it trades one wrong hint for another and deletes the evidence.
_GITLAB_TOKEN_RE = re.compile(
    _prefixed(r"(?:" + _GITLAB_PREFIX_ALT + r")[A-Za-z0-9_\-]{16,}"))


def mentions_gitlab_token(text: object) -> bool:
    """Does this text carry something shaped like a GitLab token?

    The same pattern the redaction rule uses, deliberately: the two questions
    are one question, and a classifier that recognises a credential the
    redactor does not is the pair disagreeing again, which is what #1645 was.

    This is NOT a redactor and must never be used as one -- it says only that
    a match exists, never that anything was removed. `redact()` is.
    """
    return bool(_GITLAB_TOKEN_RE.search(str(text or "")))


_RULES: list[tuple[str, re.Pattern[str]]] = [
    # -- private key blocks: first, they can contain anything ---------------
    ("private-key", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?P<secret>.*?)-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL)),

    # -- vendor prefixes ----------------------------------------------------
    ("anthropic-api-key", re.compile(_prefixed(r"sk-ant-[A-Za-z0-9_\-]{4,}"))),
    ("openai-project-key", re.compile(_prefixed(r"sk-proj-[A-Za-z0-9_\-]{16,}"))),
    ("openai-api-key", re.compile(_prefixed(r"sk-[A-Za-z0-9]{32,}"))),
    ("github-token", re.compile(_prefixed(r"gh[pousr]_[A-Za-z0-9]{20,}"))),
    ("github-pat", re.compile(_prefixed(r"github_pat_[A-Za-z0-9_]{20,}"))),
    # Seven of the thirteen documented prefixes were absent here until #1645 --
    # `glrtr-`, `gloas-`, `glft-`, `glimt-`, `glagent-`, `glwt-` and `glffct-`
    # are minted by GitLab and were unrecognised, so a transcript carrying one
    # was relayed whole.
    ("gitlab-token", _GITLAB_TOKEN_RE),
    ("aws-access-key-id", re.compile(_prefixed(r"(?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}"))),
    ("slack-token", re.compile(_prefixed(r"xox[abprse]-[A-Za-z0-9\-]{10,}"))),
    ("google-api-key", re.compile(_prefixed(r"AIza[A-Za-z0-9_\-]{35}"))),
    ("stripe-key", re.compile(_prefixed(r"[rs]k_(?:live|test)_[A-Za-z0-9]{16,}"))),
    ("npm-token", re.compile(_prefixed(r"npm_[A-Za-z0-9]{30,}"))),
    ("pypi-token", re.compile(_prefixed(r"pypi-[A-Za-z0-9_\-]{16,}"))),
    ("huggingface-token", re.compile(_prefixed(r"hf_[A-Za-z0-9]{30,}"))),
    ("jwt", re.compile(_prefixed(r"eyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"))),

    # -- positional shapes --------------------------------------------------
    ("bearer-token", re.compile(
        r"(?i)\bbearer\s+(?P<secret>[A-Za-z0-9._\-~+/]{12,}={0,2})")),
    # `Basic` alone is an English word. An unanchored `basic <12+ chars>` rule
    # fired 9 times on prose in the same sweep ("with basic module test ..."),
    # so this one requires the header that gives it its meaning. `Bearer` is a
    # coined term and produced no false positives, so it stays unanchored.
    ("basic-auth-header", re.compile(
        r"(?i)\bauthorization\s*:\s*basic\s+(?P<secret>[A-Za-z0-9+/]{12,}={0,2})")),
    ("url-password", re.compile(
        r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://[^\s/:@]+:)(?P<secret>[^\s/@]{3,})@")),
]

#: Names that make an assignment's right-hand side suspect.
_SECRET_WORD = r"(?:API[_\-]?KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIALS?|PRIVATE[_\-]?KEY|ACCESS[_\-]?KEY|APIKEY)"

#: NAME=value / "name": "value". The value class excludes quotes, whitespace,
#: shell metacharacters and `$ { < [` — so `$VAR`, `${VAR}`, `<placeholder>`
#: and an already-inserted `[REDACTED:…]` marker can never be matched.
_ASSIGNMENT = re.compile(
    r"(?i)(?P<name>[A-Za-z0-9_.\-]*" + _SECRET_WORD + r"[A-Za-z0-9_.\-]*)"
    r"\s*[=:]\s*"
    r"(?P<q>[\"']?)"
    r"(?P<secret>[^\s\"'`;|&<>()\[\]{}$,]{8,})"
    r"(?P=q)"
)

#: Characters that, on their own, make a value a visible placeholder.
_MASK_CHARS = set("*xX.•-_")


def _is_tokenish(value: str) -> bool:
    """Does this assigned value look like a credential rather than prose?

    Prose reaches the assignment rule constantly — "the token: something
    important" is a sentence, not a leak. Requiring a digit, mixed case, or
    real length separates `supersecretvalue` from `something`. It is a
    heuristic and it is the noisiest thing in this file, which is why it gates
    only the last rule.
    """
    if not value or set(value) <= _MASK_CHARS:
        return False
    if value.isdigit():
        # `tokens=1771387649(input=...` is a counter, and this domain is full of
        # them — 4 of the sweep's assignment matches were token *counts*. A
        # purely numeric credential would not be identifiable by shape anyway.
        return False
    if any(c.isdigit() for c in value):
        return True
    if len(value) >= 12 and any(c.islower() for c in value) and any(c.isupper() for c in value):
        # Mixed case alone is far too weak: `tokens=TokenUsage(input=...)` is a
        # Python repr, and CamelCase identifiers are everywhere in a transcript.
        # The length floor is what makes the signal mean anything.
        return True
    return len(value) >= 16


def _replace_group(text: str, pattern: re.Pattern[str], label: str,
                   gate=None) -> tuple[str, int]:
    """Replace only the `secret` group of every match, keeping the context."""
    count = 0

    def _sub(m: re.Match[str]) -> str:
        nonlocal count
        value = m.group("secret")
        if not value or MARKER_PREFIX in value:
            return m.group(0)
        if gate is not None and not gate(value):
            return m.group(0)
        count += 1
        start, end = m.span("secret")
        whole_start = m.start()
        return m.group(0)[: start - whole_start] + _marker(label) + m.group(0)[end - whole_start:]

    return pattern.sub(_sub, text), count


def redact(text: str | None) -> tuple[str, int]:
    """Replace values matching known credential shapes with a labelled marker.

    Returns ``(redacted_text, count)``. ``count`` is the number of values
    replaced — it is what the disclosure line reports, and it is emphatically
    not "the number of secrets in the text".
    """
    if not text:
        return ("" if text is None else text), 0
    total = 0
    for label, pattern in _RULES:
        text, n = _replace_group(text, pattern, label)
        total += n
    text, n = _replace_group(text, _ASSIGNMENT, "secret-assignment", gate=_is_tokenish)
    total += n
    return text, total


def disclosure(count: int, flag: str = ":raw") -> str:
    """One line naming what was removed and how to get it back.

    Empty when nothing was redacted — an op that redacted nothing must not
    print a security banner, or the banner stops being read.

    Deliberately worded as "matched known patterns", never "the output is
    safe". Redaction here is a refusal that says so, not a guarantee.
    """
    if count <= 0:
        return ""
    plural = "value was" if count == 1 else "values were"
    return (
        f"[!] {count} {plural} matched known secret patterns and replaced by a "
        f"labelled marker below. Detection is pattern-based, so it can miss secrets "
        f"it has no pattern for — this is not a guarantee the output is clean. "
        f"Re-run with {flag} to see them verbatim."
    )
