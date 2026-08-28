"""Every shipped op is either mapped to a raw command or a recorded absence (#1384).

`replaces` reached 15 of 87 preset ops across three families over three PRs
(#1347, #1393, #1420 and this one). Nothing said which of the other 72 were
*decided* and which were merely never looked at, and those two render
identically -- this repository's own defect class, an absence produced by the
work read as an absence in the world, pointed at its own guard.

So the population is partitioned here and the partition is asserted total. An
op added to any shipped preset belongs to `_MAPPED` or to `_ABSENT` with a
reason, or this file goes red. It is the only thing that makes "15 of 87" a
number a reader can act on.

Two things this file deliberately does **not** do:

* It does not judge whether a reason is good. That argument lives in
  `docs/presets/<name>.md` and in the per-family tests, which assert the
  behaviour rather than the prose.
* It does not cover builtin ops (`read`, `grep`, `glob`, ...). Those never
  reach the guard's population at all, which is a decision with its own file:
  `tests/test_guard_builtin_ops_absent_1384.py`.
"""
from __future__ import annotations

import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PRESETS = _ROOT / "presets"


def _shipped_ops() -> dict:
    """op name -> its definition, over every shipped preset."""
    out = {}
    for path in sorted(_PRESETS.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for name, definition in (data.get("ops") or {}).items():
            if isinstance(definition, dict):
                out[name] = definition
    return out


# --------------------------------------------------------------------------
# Mapped: the op declares the raw invocation it supersedes.
# --------------------------------------------------------------------------

_MAPPED = {
    # presets/git.json -- #1420
    "git-status", "git-commit", "git-push", "git-worktrees",
    # presets/github.json -- #1347 (four) and #1384 step 3 (seven)
    "gh-issue", "gh-issue-create", "gh-issues", "gh-job", "gh-labels",
    "gh-branch", "gh-pr", "gh-pr-create", "gh-pr-merge", "gh-prs", "gh-run",
    # presets/github.json -- #1739
    "gh-pr-edit",
    # presets/gitlab.json -- #1393
    "gl-issue", "gl-issue-create", "gl-mr", "gl-mrs", "gl-pipeline",
    "gl-job", "gl-api",
}

# --------------------------------------------------------------------------
# Absent: no entry, on purpose. One line each, because the reason is the
# whole content of the decision -- `raw_command_guard: false` is repo-global,
# so an over-broad entry here disarms every mapping above it.
# --------------------------------------------------------------------------

_NO_CLI = ("an HTTP API op with no CLI to supersede: there is no first-party "
           "command-line client for this service, so no raw invocation exists "
           "to claim")
_SUPERTOOL_OWN = ("it operates on supertool's own state, which no third-party "
                  "command produces")

_ABSENT = {
    # --- presets/git.json (reasons in docs/presets/git.md) -----------------
    "git-diff": "raw `git diff` spans revision ranges, machine formats and "
                "pathspecs the op has no spelling for, and a range carries no "
                "flag to exclude it by",
    "git-checkout": "`git checkout <arg>` is two operations sharing one name "
                    "and the op refuses the pathspec one (#756) -- the same "
                    "positional-value discrimination as `git push origin <tag>`",
    "git-merge": "`git merge --abort` / `--continue` are what `git-conflicts` "
                 "prints as its own hint, and `--no-ff` / `--squash` / `-X` "
                 "have no op spelling",
    "git-trail": "its raw form is `git log -S`, and a clustered `-Spattern` "
                 "is not the flag `-S`, so a mapping would fire on one "
                 "spelling and not the other",
    "git-blame": "the op needs a LINE; whole-file `git blame` has no "
                 "replacement",
    "git-investigate": "a flag combination over `git log` whose other uses "
                       "the op does not answer",
    "git-diverge": "a flag combination over `git rev-list --left-right`, "
                   "whose other uses the op does not answer",
    "git-conflicts": "a flag combination over `git diff` and "
                     "`git ls-files -u`, whose other uses the op does not "
                     "answer",
    "git-resolve": "its raw form is `git checkout --ours` / `--theirs`, which "
                   "the op refuses outright on source extensions, so a "
                   "mapping would name an op that declines the command",

    # --- presets/github.json (reasons in docs/presets/github.md) ----------
    "gh-check": "its raw form is `gh api .../check-runs/<id>`, and `gh api` "
                "is the escape hatch the whole schema depends on staying "
                "unclaimed",
    "gh-follow": "sits on `gh api -X PUT user/following/<login>`; `gh` has no "
                 "CLI verb for it",
    "gh-following": "sits on `gh api user/following`; `gh` has no CLI verb "
                    "for it either",
    "gh-star": "sits on `gh api -X PUT user/starred/<owner>/<repo>`; `gh` "
               "has no CLI verb for it either",
    "gh-starred": "sits on `gh api user/starred`; `gh` has no CLI verb for it "
                  "either",
    "gh-batch-follow": "a rate-delayed loop over a file of logins; no raw "
                       "command is the same call",
    "gh-batch-star": "a rate-delayed loop over a file of repositories; no raw "
                     "command is the same call",
    "gh-find-followable": "a composite of a stargazer and a contributor list "
                          "with deduplication and org filtering",
    "gh-find-starable": "a composite of a topic search with a star sort",

    # --- presets/gitlab.json ----------------------------------------------
    "gl-runners": "the runner fleet is read through the REST API and joined "
                  "with the job queue to derive STARVED; `glab` ships no "
                  "runner subcommand that answers it",

    # --- presets/bluesky.json, devto.json, hashnode.json ------------------
    "bluesky_publish": _NO_CLI, "bluesky_read": _NO_CLI,
    "bluesky_list": _NO_CLI, "bluesky_search": _NO_CLI,
    "bluesky_like": _NO_CLI, "bluesky_repost": _NO_CLI,
    "bluesky_follow": _NO_CLI, "bluesky_status_since": _NO_CLI,
    "devto_publish": _NO_CLI, "devto_read": _NO_CLI, "devto_list": _NO_CLI,
    "devto_browse": _NO_CLI, "devto_comment": _NO_CLI,
    "devto_comments": _NO_CLI, "devto_react": _NO_CLI,
    "devto_status_since": _NO_CLI,
    "hashnode_publish": _NO_CLI, "hashnode_read": _NO_CLI,
    "hashnode_list": _NO_CLI, "hashnode_browse": _NO_CLI,
    "hashnode_search": _NO_CLI, "hashnode_comment": _NO_CLI,
    "hashnode_comments": _NO_CLI, "hashnode_reply": _NO_CLI,
    "hashnode_react": _NO_CLI, "hashnode_status_since": _NO_CLI,

    # --- presets/slack.json -------------------------------------------------
    "slack_publish": _NO_CLI,

    # --- presets/watch.json ------------------------------------------------
    "watch": "`gh pr checks --watch` and `gh run watch` are FOREGROUND "
             "pollers on one id; this registers a background watcher with "
             "delivery, so a refusal would name an op that does something "
             "else",
    "unwatch": _SUPERTOOL_OWN,
    "watches": _SUPERTOOL_OWN,
    "channel": _SUPERTOOL_OWN,
    "radar": "a composite board over several tiers; no raw command is the "
             "same question",

    # --- presets/mcp.json --------------------------------------------------
    "mcp_daemon": _SUPERTOOL_OWN, "mcp_status": _SUPERTOOL_OWN,
    "mcp_stop": _SUPERTOOL_OWN, "mcp_stop_all": _SUPERTOOL_OWN,

    # --- presets/claude-log.json -------------------------------------------
    "claude-log-list": "reads Claude Code's own transcript store; the raw "
                       "form is a `jq` expression over JSONL, which is a "
                       "shape rather than a command",
    "claude-log-tail": "reads the same transcript store; the raw form is a "
                       "`jq` expression over JSONL, which is a shape rather "
                       "than a command",
    "claude-log-summary": "reads the same transcript store; the raw form is a "
                          "`jq` expression over JSONL, which is a shape "
                          "rather than a command",
    "claude-log-cost": "reads the same transcript store and applies per-model "
                       "pricing arithmetic on top; no raw command carries the "
                       "price table",

    # --- presets/xml.json --------------------------------------------------
    "xml": "`xmllint --xpath` is the nearest raw form, and it is neither "
           "installed by default on macOS or Windows nor the same selector "
           "language; claiming a command most users do not have blocks "
           "nothing and dead-ends the users who do",
    "xml_attr": "same `xmllint --xpath` reasoning: not installed by default, "
                "and not the same selector language",
    "xml_count": "same `xmllint --xpath` reasoning: not installed by default, "
                 "and not the same selector language",

    # --- one-op presets ----------------------------------------------------
    "claims": _SUPERTOOL_OWN,
    "dashboard": "a composite of one `gh pr list`, one `gh label list`, one "
                 "`gh issue list` and one `gh repo view`; the ops those four "
                 "map to are claimed individually and the board is not one "
                 "of them",
    "plugin-marketplace": _SUPERTOOL_OWN,
}


def test_the_partition_is_total():
    """Every shipped op is a decision, and no op is two decisions.

    This is the assertion the file exists for: a new op that declares no
    `replaces` and carries no reason here is indistinguishable from one that
    was considered and declined, and the second is the only acceptable state.
    """
    ops = set(_shipped_ops())
    assert ops, "no shipped op could be enumerated, so nothing was checked"
    assert not (_MAPPED & set(_ABSENT)), sorted(_MAPPED & set(_ABSENT))
    unrecorded = ops - _MAPPED - set(_ABSENT)
    assert not unrecorded, (
        "these shipped ops are neither mapped nor recorded as a deliberate "
        "absence: " + ", ".join(sorted(unrecorded)) + ". Add a `replaces` "
        "entry, or a one-line reason to _ABSENT in this file.")
    stale = (_MAPPED | set(_ABSENT)) - ops
    assert not stale, (
        "these ops are recorded here and no longer exist: "
        + ", ".join(sorted(stale)))


def test_the_mapped_set_is_what_the_presets_actually_declare():
    """`_MAPPED` is a claim about the files, not a wish.

    Without this the partition above stays total while every mapping is
    deleted: the op names would simply move from one side of the census to
    neither, and `unrecorded` is computed against the union.
    """
    declared = {name for name, definition in _shipped_ops().items()
                if "replaces" in definition}
    assert declared == _MAPPED, {
        "declared but not in _MAPPED": sorted(declared - _MAPPED),
        "in _MAPPED but not declared": sorted(_MAPPED - declared),
    }


def test_every_absence_carries_a_reason_somebody_can_argue_with():
    """A blank or one-word reason is the absence this file exists to catch."""
    for name, reason in sorted(_ABSENT.items()):
        assert isinstance(reason, str), name
        assert len(reason.split()) >= 8, (name, reason)


def test_the_census_covers_every_preset_file():
    """A preset whose ops reached neither column would pass silently above.

    `_shipped_ops` globs, so a new preset file is picked up -- but a preset
    that failed to parse would raise here rather than shrink the population,
    and one that parsed to no ops is named.
    """
    empty = []
    for path in sorted(_PRESETS.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        # A `builtin-ops` section documents ops the preset does not define
        # (#2025): their `replaces` mapping, if any, is a fact about the
        # built-in and belongs to core, so they are outside this census by
        # construction rather than missing from it. A manifest with neither
        # section is still the empty preset this test is about.
        if data.get("ops") or data.get("builtin-ops"):
            continue
        empty.append(path.name)
    assert not empty, ("preset(s) with no ops: " + ", ".join(empty)
                       + " -- a preset contributing nothing to the census "
                         "makes its coverage number meaningless")
