



































































from __future__ import annotations

import os
import pathlib
import re
import sys






sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from linebreaks import split_lines


def safe_realpath(file: str) -> str | None:





































    if "\x00" in file:
        return None
    try:
        return os.path.realpath(file)
    except (OSError, ValueError):
        return None


def _is_windows(platform: str | None) -> bool:



    return (platform if platform is not None else sys.platform).startswith("win")


def path_variants(file: str, platform: str | None = None) -> list[str]:










    if not _is_windows(platform):
        return [file]
    variants = [file]
    for v in (file.replace("\\", "/"), file.replace("/", "\\")):
        if v not in variants:
            variants.append(v)
    for v in list(variants):
        if len(v) >= 2 and v[1] == ":" and v[0].isalpha():
            flipped = (v[0].lower() if v[0].isupper() else v[0].upper()) + v[1:]
            if flipped not in variants:
                variants.append(flipped)
    return variants


def anchor(file: str, tail: str, platform: str | None = None,
           extra_paths: list[str] | None = None) -> re.Pattern[str]:















































    bases = [file] + list(extra_paths or [])
    all_variants: list[str] = []
    for base in bases:
        for v in path_variants(base, platform):
            if v not in all_variants:
                all_variants.append(v)
    alternatives = "|".join(re.escape(v) for v in all_variants)














    return re.compile(r"(?<!\S)(?:" + alternatives + r")" + tail)
















_LOOSE_LEADING_PATH_RE = re.compile(r"^(.*?):\d+\b")


def unanchored_path_hint(output: str) -> str | None:







    for line in split_lines(output):
        m = _LOOSE_LEADING_PATH_RE.match(line)
        if m:
            return m.group(1)
    return None


def anchor_miss_message(file: str, output: str, fallback: str) -> str:










    hint = unanchored_path_hint(output) if output else None
    if hint is None or hint == file:
        return fallback
    return (f"anchor matched no accepted spelling of the invoked path "
            f"{file!r}; the tool's own output appears to start with "
            f"{hint!r} instead -- {fallback}")
