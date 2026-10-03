#!/usr/bin/env python3






























from __future__ import annotations


COMMENT = "#"


def split_annotation(line: str) -> tuple[str, str]:






    text = str(line)
    for i, ch in enumerate(text):
        if ch == COMMENT and i > 0 and text[i - 1].isspace():
            return text[:i].strip(), text[i:].strip()
    return text.strip(), ""


def is_comment(line: str) -> bool:

    return str(line).strip().startswith(COMMENT)


def unusable(value: str) -> str:










    text = str(value)
    if not text:
        return "empty after the inline annotation was removed"
    if any(c.isspace() for c in text):
        return ("contains whitespace, so it is not one name — an inline "
                "comment must be introduced by `#`")
    if COMMENT in text:
        return ("contains '#', which no GitHub owner, repository or login "
                "may — an annotation must have whitespace before its '#'")
    return ""
