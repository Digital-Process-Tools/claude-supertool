"""Every op whose code reads a `force` token must name it in its `syntax` (#1269).

#1239 shipped `test_registry_syntax_drift_1239.py`, which compares an op's
declared `_FILTER_KEYS` / `_FLAGS` against the `syntax` string in its manifest.
That guard covers **4 of the 85 ops in the registry**, because it can only see
ops that bind a module-level vocabulary — and most ops parse their tokens
positionally, out of `arg.split("|")` or `sys.argv`, with nothing for an AST
walk to find. `gh-pr-merge` accepting `cleanup` while its `syntax` omitted it,
committed inside #1239's own PR, is the shape.

The three routes #1269 names were all measured against this registry before
this test was written, and only one of them survives:

* **Derive tokens from the parse.** 199 candidate tokens across 49 ops, 4 of
  them real — 2% precision. The other 195 are API state names, platform names
  and verdict labels compared against string literals for reasons that have
  nothing to do with the argument (`"completed"`, `"darwin"`, `"prunable"`).
  Unusable as a gate, and narrowing it means a taint analysis whose
  "could not trace" arm reports clean.
* **Round-trip every token the syntax names.** 34 ops name tokens, 56 tokens
  in all, and the static form of the check found **0** drifts in that
  direction plus one false positive (`radar` forwards its filter string to
  the tier modules, so `author=` is accepted and appears nowhere in
  `radar.py`). Executing it for real is worse than useless here: the ops with
  the most tokens are the ones that WRITE — `gh-pr-merge`, `git-push`,
  `git-commit`, `git-resolve` and the three publish ops — so the test would
  have to invoke the confirmation-bypass token it exists to check.
* **Require a declaration on every op.** 30 ops would need one, and a
  declaration that the positional parser does not consult is a third copy of
  the contract that drifts from both the others.

So this guard is deliberately narrow and says so: it checks **one token**,
`force`, over the whole registry. That token is worth its own sweep because it
is the only one in the vocabulary whose omission is a safety-documentation
failure rather than a convenience one — it is the bypass for the publish
confirmation gate (`_publish_safety.require_confirm`) and for the pre-flight
duplicate checks that #601 and #562 made fail-closed. A caller who cannot see
it in `supertool 'ops'` cannot recover from a refusal that names it.

Measured when this was written: **10 ops read a `force` token; 4 of them did
not name it** — `bluesky_follow`, `bluesky_like`, `devto_publish` and
`hashnode_publish`. All four declared it in the opening line of their own module
docstring, and all four in the prose of their `docs/presets/*.md` — the
registry, which is what `ops` prints, was the only surface that omitted it.

What this test does NOT cover, stated so its silence is not read as coverage:
every other token, in both directions. #1269 is the record of why.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
PRESETS = ROOT / "presets"

#: `cmd` looks like `{python} {path}github/issues.py {args}`.
_SCRIPT = re.compile(r"\{path\}([A-Za-z0-9_./-]+\.py)")

#: `force` as a whole word. `git-push`'s `force-with-lease` is a different
#: token and must not be read as this one — a substring test would report
#: `git-push` as declaring a bypass it does not have.
_DECLARES_FORCE = re.compile(r"(?<![A-Za-z0-9_-])force(?![A-Za-z0-9_-])")

TOKEN = "force"

#: Preset entries the sweep read and had no script for, because they document a
#: built-in rather than declaring a command (#2025). Populated by `_registry`
#: and asserted on below, so the exemption cannot quietly widen.
DOC_ONLY: list = []


def _docstring_nodes(tree: ast.AST) -> set:
    """The id() of every node that is a docstring rather than a value.

    Prose is excluded on purpose. Nine of the ten force-bearing ops mention
    `|force` somewhere in a docstring or an f-string error message, so a plain
    substring sweep of the file would sweep in ops that only TALK about the
    token and would then be a test about prose. Only a string the module
    actually evaluates counts as reading the token.
    """
    out = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if not body or not isinstance(body[0], ast.Expr):
            continue
        first = body[0].value
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            out.add(id(first))
    return out


def reads_force(path: Path) -> bool:
    """Does this script evaluate the exact string `force`?

    Exact equality, not containment: `auto_force` (hashnode's config key) and
    `--force-with-lease` are different tokens, and matching them here would
    put an op into the sweep for a token it does not accept.
    """
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    docs = _docstring_nodes(tree)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value == TOKEN
                and id(node) not in docs):
            return True
    return False


def _registry() -> list:
    """`(op, manifest, syntax, script)` for every op in every manifest.

    An entry whose `cmd` names a script that cannot be resolved raises rather
    than being skipped. A sweep that drops an op it could not read, and then
    reports no drift, is the defect this whole family of guards is filed
    under — and it would drop exactly the ops whose `cmd` shape changed,
    which is when you most want to be told.
    """
    import supertool

    builtins = set(supertool._valid_op_names())
    rows = []
    for manifest in sorted(PRESETS.glob("*.json")):
        data = json.loads(manifest.read_text(encoding="utf-8"))
        for name, entry in sorted((data.get("ops") or {}).items()):
            if not isinstance(entry, dict):
                continue
            cmd = str(entry.get("cmd") or "")
            if not cmd and name in builtins:
                # A doc-only entry: `presets/lsp.json` documents five ops that
                # dispatch from core (#2025). There is no preset script to
                # read, and any `force` token one of them took would live in
                # `_supertool.py`, which this sweep is not about.
                #
                # Recorded rather than dropped. `test_the_sweep_read_the_whole
                # _registry` asserts on the size of what was read, so an entry
                # that vanished here would shrink that population silently —
                # the exact shape this file's own docstring refuses.
                DOC_ONLY.append(f"{name} (presets/{manifest.name})")
                continue
            hit = _SCRIPT.search(cmd)
            if not hit:
                raise AssertionError(
                    f"{name} (presets/{manifest.name}) has a cmd this sweep "
                    f"cannot resolve to a script: {cmd!r}. Teach the sweep "
                    f"the new shape rather than letting the op fall out of "
                    f"it silently (#1269).")
            script = PRESETS / hit.group(1)
            if not script.is_file():
                raise AssertionError(
                    f"{name} (presets/{manifest.name}) names a script that "
                    f"does not exist: {script}")
            rows.append((name, manifest.name, str(entry.get("syntax") or ""),
                         script))
    return rows


REGISTRY = _registry()
FORCE_OPS = [r for r in REGISTRY if reads_force(r[3])]


def test_the_sweep_read_the_whole_registry() -> None:
    """A shrunk population passes every parametrised case and proves nothing."""
    assert len(REGISTRY) + len(DOC_ONLY) >= 80, (len(REGISTRY), DOC_ONLY)


def test_every_exempted_entry_is_a_builtin_documented_by_a_preset() -> None:
    """The exemption's blast radius, named rather than trusted.

    `not cmd and name in builtins` is two conditions, and only the second is
    hard to satisfy by accident: a preset op that lost its `cmd` in an edit
    would be exempted the moment its name happened to collide with a built-in.
    So assert the population itself, not just that the sweep survived it.
    """
    import supertool

    builtins = set(supertool._valid_op_names())
    assert DOC_ONLY, (
        "nothing is exempted any more — delete the doc-only arm in _registry "
        "rather than leaving an untested branch in the sweep")
    for row in DOC_ONLY:
        name = row.split(" ", 1)[0]
        assert name in builtins, (
            f"{row} has no cmd and is not a built-in either: the sweep skipped "
            f"an op it should have refused")


def test_the_force_sweep_found_the_ops_it_is_about() -> None:
    """The population is named, not merely counted.

    `assert len(...) >= N` alone cannot tell "the ops still read force" from
    "the AST walk stopped finding it and the count is made up of others".
    """
    names = [n for n, _m, _s, _p in FORCE_OPS]
    assert len(names) >= 8, names
    for expected in ("bluesky_publish", "devto_publish", "hashnode_publish",
                     "gh-pr-merge", "git-resolve"):
        assert expected in names, names


def test_prose_alone_does_not_put_an_op_in_the_sweep(tmp_path) -> None:
    """The docstring exclusion is the thing that makes this a real test.

    Without it every op whose refusal text says `use |force` would be swept
    in, the population would be `every op that mentions force`, and the
    parametrised case below would be asserting about English rather than
    about what the op accepts.
    """
    prose = tmp_path / "prose.py"
    prose.write_text('"""Use |force to bypass."""' + chr(10) + "x = 1",
                     encoding="utf-8")
    real = tmp_path / "real.py"
    real.write_text('"""Use |force to bypass."""' + chr(10)
                    + 'f = a == "force"', encoding="utf-8")
    near = tmp_path / "near.py"
    near.write_text('f = a == "auto_force"', encoding="utf-8")

    assert not reads_force(prose)
    assert reads_force(real)
    assert not reads_force(near)


def test_force_is_matched_as_a_word_not_a_substring() -> None:
    """`git-push:force-with-lease` is not a confirmation bypass."""
    assert _DECLARES_FORCE.search("gh-pr-merge:N[|force]")
    assert not _DECLARES_FORCE.search("git-push[:force-with-lease]")
    assert not _DECLARES_FORCE.search("x[:auto_force]")


def test_a_changelog_fragment_exists() -> None:
    from _changelog_findable import assert_change_is_findable
    assert_change_is_findable(1269)


@pytest.mark.parametrize(
    "op,manifest,syntax,script",
    FORCE_OPS,
    ids=[n for n, _m, _s, _p in FORCE_OPS])
def test_an_op_that_reads_force_declares_it_in_its_syntax(
        op: str, manifest: str, syntax: str, script: Path) -> None:
    assert _DECLARES_FORCE.search(syntax), (
        f"{op} (presets/{manifest}) reads a {TOKEN!r} token in "
        f"presets/{script.relative_to(PRESETS)} and its declared syntax does "
        f"not name it:{chr(10)}    {syntax}{chr(10)}"
        f"`force` is the bypass for the publish confirmation gate and for the "
        f"fail-closed pre-flight checks (#562, #601): the refusal tells the "
        f"caller to use it, and `supertool 'ops'` — which prints this string "
        f"and nothing else — does not tell them it exists (#1269).")
