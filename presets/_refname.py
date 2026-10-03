






























from __future__ import annotations

import re
import shlex








ORDINARY_REF = re.compile(r"^[A-Za-z0-9._/-]+\Z")


def ordinary(ref: str) -> bool:











    return bool(ref) and not ref.startswith("-") and bool(ORDINARY_REF.match(ref))


def shell_ref(ref: str) -> str:

















    if ordinary(ref):
        return ref
    quoted = shlex.quote(ref)
    if quoted == ref:





        quoted = "'" + ref.replace("'", "'\\''") + "'"
    return quoted


def warning(refs: list[str]) -> str | None:






    odd = [r for r in refs if not ordinary(r)]
    if not odd:
        return None
    plural = "s" if len(odd) != 1 else ""
    return (
        f"  # ⚠ {len(odd)} name{plural} below contain characters a shell acts "
        f"on, and have been quoted — read the command before running it."
    )
