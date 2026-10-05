






























from __future__ import annotations

import sys
from typing import Optional







_ANNOUNCED: "set[str]" = set()


def _notice(text: str) -> None:




    if text in _ANNOUNCED:
        return
    _ANNOUNCED.add(text)
    print(text)
    sys.stdout.flush()


def env_int(raw: Optional[str], name: str, default: int, *,
            minimum: Optional[int] = None) -> int:





















    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        _notice(f"note: {name}={raw!r} is not a whole number "
                f"- ignoring it and using {default}.")
        return default
    if minimum is not None and value < minimum:
        _notice(f"note: {name}={raw!r} is below the minimum of {minimum} "
                f"- ignoring it and using {default}.")
        return default
    return value


def env_float(raw: Optional[str], name: str, default: float, *,
              minimum: Optional[float] = None) -> float:


    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        _notice(f"note: {name}={raw!r} is not a number "
                f"- ignoring it and using {default}.")
        return default
    if value != value:  
        _notice(f"note: {name}={raw!r} is not a usable number "
                f"- ignoring it and using {default}.")
        return default
    if minimum is not None and value < minimum:
        _notice(f"note: {name}={raw!r} is below the minimum of {minimum} "
                f"- ignoring it and using {default}.")
        return default
    return value
