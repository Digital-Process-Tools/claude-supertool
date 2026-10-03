







































from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  
import _secrets  





CREDENTIAL_SHAPE = "credential-shape"
FENCE_FORGERY = "fence-forgery"

_FENCE_PATTERNS = [
    re.compile(r"⟨\s*/?\s*\w+"),
    re.compile(r"<\|\s*/?\s*\w+.{0,20}?\|>", re.DOTALL),
    re.compile(r"<\|(im_start|im_end|system|assistant|user)\|>"),
    re.compile(r"\[/?INST\]"),
    re.compile(r"<<SYS>>|<</SYS>>"),
    re.compile(r"</(system|remote|instructions)>", re.IGNORECASE),
]






class Finding:
    __slots__ = ("axis", "detail")

    def __init__(self, axis: str, detail: str) -> None:
        self.axis = axis
        self.detail = detail

    def __eq__(self, other: object) -> bool:
        return (isinstance(other, Finding) and self.axis == other.axis
                and self.detail == other.detail)

    def __repr__(self) -> str:
        return f"Finding({self.axis!r}, {self.detail!r})"


def scan(text: str) -> List[Finding]:


    findings: List[Finding] = []
    _redacted, n = _secrets.redact(text)
    if n:
        findings.append(Finding(CREDENTIAL_SHAPE, _secrets.disclosure(n)))
    for pattern in _FENCE_PATTERNS:
        m = pattern.search(text)
        if m:
            snippet = m.group(0)
            if len(snippet) > 40:
                snippet = snippet[:40] + "…"
            findings.append(Finding(FENCE_FORGERY, f"matched {snippet!r}"))
            break
    return findings
