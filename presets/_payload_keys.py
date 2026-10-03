"""Refuse an unrecognised `@payload` key before anything is created (#2123).

`gh-issue-create` and `gl-issue-create` used to silently discard any payload
key they did not read: a `body` key on `gl-issue-create` (which wants
`description`) created an issue with no description at all, and the reverse
-- `description` on `gh-issue-create` (which wants `body`) -- did the same
thing minutes later, in the opposite direction. Both calls returned `PASS`,
an issue number and a URL. A payload key that goes nowhere is
indistinguishable from a payload key that worked, which is this codebase's
own standing defect class (see `CLAUDE.md`, "The defect this codebase keeps
having") applied to a write instead of a read.

**Why this is a shared helper and not a shared payload loader.** The issue
this module fixes (#2123) asks the wider question -- should every `@payload`
op route through one core loader that knows every op's key set? Checked
before building anything: no such loader exists today. `gh-issue-create`,
`gl-issue-create`, `gh-pr-create` and `gh-pr-edit` each already carry their
own private `_load_payload`, independently, and have since before this
issue -- introducing a registration mechanism so a shared loader could learn
each op's accepted keys would be a larger change than four ops warrant, and
nothing here needs it: `check` and `resolve_aliases` take the accepted set
and the alias map as arguments, so each op still owns its own vocabulary and
only the (unknown-key, alias-conflict) arithmetic is shared -- the same
shape `_repo_target.py` already uses for the repo-target reconciliation
these same four ops share. The wider question -- one core loader for every
`@payload` op -- is reported, not answered, in #2123's own follow-up.
"""
from __future__ import annotations


def check(payload: dict, accepted: set[str], aliases: dict[str, str],
          op: str) -> str | None:
    """`None` if every key in `payload` is consumed by `op`, else a refusal
    naming the offending keys and the full accepted set (aliases included).

    Called before anything is written -- create-wrong is the worst of the
    three outcomes a payload key can produce (create-correct, refuse,
    create-wrong), because only a human noticing recovers it.
    """
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
    """`(payload, error)` -- every alias key folded onto its canonical name.

    `aliases` maps an alias spelling to the canonical key the rest of the op
    reads (e.g. `{"description": "body"}` on `gh-issue-create`, matching
    what the GitHub API itself calls the field). The alias key is removed
    from the result; the canonical key is left alone if it is already
    present and agrees, and the call is refused -- not resolved by
    preferring one silently -- if the two disagree, the same way a silent
    precedence anywhere else in this tool is refused rather than guessed.
    """
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
