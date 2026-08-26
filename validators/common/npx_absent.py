"""How npx says "the package you asked to run is not there, and
`--no-install` forbids fetching it" (#1948, #1949).

`grep npx validators/` is the whole population: eslint.py and stylelint.py are
the only two adapters that fall back to `npx --no-install <tool>` when the
tool itself is not on PATH. Both need to tell that refusal apart from a real
npx failure, and both were keeping their own copy of the spellings that mean
it -- eslint's `_NPX_ABSENT` (added #667/#918), stylelint carrying none at all
(#1949). This module is the one place a spelling lands from here on, so a
third adapter reaching for npx does not get to choose whether it copies the
list correctly.

Three spellings observed live, none of them stable across npm's own history:

- npm 8-10: `could not determine executable to run`
- npm 11: `unknown command: "<pkg>"`, quoting the requested package name
- npm 10.9.4 (#1948, reproduced on this machine): npx now refuses before
  ever trying to resolve a command at all --
  `npm error npx canceled due to missing packages and no YES option: [...]`.
  `--no-install` and the older `--no` flag produce the identical message and
  exit code, so "the flag stopped being honoured" is not the explanation --
  only the wording moved.

**The narrowness is deliberate and is the point of this module, not an
accident of it.** Any OTHER npx failure -- a real crash, a broken registry, a
network error -- must stay a loud fault, because swallowing an unknown
failure the same way is the mirror-image mistake #1934's whole family exists
to prevent. Only the stable part of the npm 10.9.4 sentence is matched --
`npx canceled due to missing packages` -- never the trailing package-list and
`no YES option` clause, which is exactly the part `--no`/`--no-install`
choose to phrase two different ways for the identical refusal.
"""
from __future__ import annotations

#: Spellings that name no particular package, so no `tool` name is needed to
#: match them. Checked against an already-lowercased stderr.
_GENERIC_MARKERS = (
    "could not determine executable to run",
    "npx canceled due to missing packages",
)


def is_npx_absent(stderr_lower: str, tool: str) -> bool:
    """Does `stderr_lower` (already `.lower()`'d by the caller) read as npx
    refusing to fetch `tool` under `--no-install`, in any spelling seen so
    far?

    Consulted only on the npx fallback route (see eslint.py/stylelint.py's
    own `via_npx`) -- this makes no claim about a global install failing,
    only about npx's own refusal to fetch a package it was told not to.
    `tool` is the package name npm would quote back (npm 11's spelling),
    never the validator name a repo configures.
    """
    if any(marker in stderr_lower for marker in _GENERIC_MARKERS):
        return True
    return f'unknown command: "{tool}"' in stderr_lower
