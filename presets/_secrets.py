
















































from __future__ import annotations

import re




MARKER_PREFIX = "[REDACTED:"


def _marker(label: str) -> str:
    return f"{MARKER_PREFIX}{label}]"















_L = r"(?<![A-Za-z0-9])"
_R = r"(?![A-Za-z0-9])"


def _prefixed(body: str) -> str:

    return _L + r"(?P<secret>" + body + r")" + _R













GITLAB_TOKEN_PREFIXES: tuple[str, ...] = (
    "glpat-",    
    "gloas-",    
    "gldt-",     
    "glrt-",     
    "glrtr-",    
    "glcbt-",    
    "glptt-",    
    "glft-",     
    "glimt-",    
    "glagent-",  
    "glwt-",     
    "glsoat-",   
    "glffct-",   
)

_GITLAB_PREFIX_ALT = "|".join(re.escape(p) for p in GITLAB_TOKEN_PREFIXES)








_GITLAB_TOKEN_RE = re.compile(
    _prefixed(r"(?:" + _GITLAB_PREFIX_ALT + r")[A-Za-z0-9_\-]{16,}"))


def mentions_gitlab_token(text: object) -> bool:









    return bool(_GITLAB_TOKEN_RE.search(str(text or "")))


_RULES: list[tuple[str, re.Pattern[str]]] = [

    ("private-key", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?P<secret>.*?)-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL)),


    ("anthropic-api-key", re.compile(_prefixed(r"sk-ant-[A-Za-z0-9_\-]{4,}"))),
    ("openai-project-key", re.compile(_prefixed(r"sk-proj-[A-Za-z0-9_\-]{16,}"))),
    ("openai-api-key", re.compile(_prefixed(r"sk-[A-Za-z0-9]{32,}"))),
    ("github-token", re.compile(_prefixed(r"gh[pousr]_[A-Za-z0-9]{20,}"))),
    ("github-pat", re.compile(_prefixed(r"github_pat_[A-Za-z0-9_]{20,}"))),




    ("gitlab-token", _GITLAB_TOKEN_RE),
    ("aws-access-key-id", re.compile(_prefixed(r"(?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}"))),
    ("slack-token", re.compile(_prefixed(r"xox[abprse]-[A-Za-z0-9\-]{10,}"))),
    ("google-api-key", re.compile(_prefixed(r"AIza[A-Za-z0-9_\-]{35}"))),
    ("stripe-key", re.compile(_prefixed(r"[rs]k_(?:live|test)_[A-Za-z0-9]{16,}"))),
    ("npm-token", re.compile(_prefixed(r"npm_[A-Za-z0-9]{30,}"))),
    ("pypi-token", re.compile(_prefixed(r"pypi-[A-Za-z0-9_\-]{16,}"))),
    ("huggingface-token", re.compile(_prefixed(r"hf_[A-Za-z0-9]{30,}"))),
    ("jwt", re.compile(_prefixed(r"eyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"))),


    ("bearer-token", re.compile(
        r"(?i)\bbearer\s+(?P<secret>[A-Za-z0-9._\-~+/]{12,}={0,2})")),




    ("basic-auth-header", re.compile(
        r"(?i)\bauthorization\s*:\s*basic\s+(?P<secret>[A-Za-z0-9+/]{12,}={0,2})")),
    ("url-password", re.compile(
        r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://[^\s/:@]+:)(?P<secret>[^\s/@]{3,})@")),
]


_SECRET_WORD = r"(?:API[_\-]?KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIALS?|PRIVATE[_\-]?KEY|ACCESS[_\-]?KEY|APIKEY)"




_ASSIGNMENT = re.compile(
    r"(?i)(?P<name>[A-Za-z0-9_.\-]*" + _SECRET_WORD + r"[A-Za-z0-9_.\-]*)"
    r"\s*[=:]\s*"
    r"(?P<q>[\"']?)"
    r"(?P<secret>[^\s\"'`;|&<>()\[\]{}$,]{8,})"
    r"(?P=q)"
)


_MASK_CHARS = set("*xX.•-_")


def _is_tokenish(value: str) -> bool:








    if not value or set(value) <= _MASK_CHARS:
        return False
    if value.isdigit():



        return False
    if any(c.isdigit() for c in value):
        return True
    if len(value) >= 12 and any(c.islower() for c in value) and any(c.isupper() for c in value):



        return True
    return len(value) >= 16


def _replace_group(text: str, pattern: re.Pattern[str], label: str,
                   gate=None) -> tuple[str, int]:

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








    if count <= 0:
        return ""
    plural = "value was" if count == 1 else "values were"
    return (
        f"[!] {count} {plural} matched known secret patterns and replaced by a "
        f"labelled marker below. Detection is pattern-based, so it can miss secrets "
        f"it has no pattern for — this is not a guarantee the output is clean. "
        f"Re-run with {flag} to see them verbatim."
    )
