


























from __future__ import annotations


def check(payload: dict, accepted: set[str], aliases: dict[str, str],
          op: str) -> str | None:







    known = set(accepted) | set(aliases)
    unknown = sorted(k for k in payload if k not in known)
    if not unknown:
        return None
    plural = "key" if len(unknown) == 1 else "keys"
    unk = ", ".join(repr(k) for k in unknown)
    acc = ", ".join(repr(k) for k in sorted(known))
    return (
        f"ERROR: {op} payload carries unrecognised {plural} {unk} -- nothing "
        f"was written. Accepted keys: {acc}."
    )


def resolve_aliases(payload: dict,
                     aliases: dict[str, str]) -> tuple[dict, str | None]:










    result = dict(payload)
    for alias, canonical in aliases.items():
        if alias not in result:
            continue
        alias_value = result.pop(alias)
        if canonical in result and result[canonical] != alias_value:
            return payload, (
                f"ERROR: payload has both {canonical!r} and {alias!r} "
                f"(an alias for {canonical!r}) with different values -- use "
                f"one"
            )
        result.setdefault(canonical, alias_value)
    return result, None
