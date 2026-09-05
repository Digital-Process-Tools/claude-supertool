"""#1299 — `plugin-marketplace`: did this release reach anyone?

The question was hand-rolled three times, and both obvious routes produce an
**absence that reads like an answer** — this repo's defect class on a question
whose whole point is "did it ship".

Measured 2026-08-11 against the live catalogues:

* ``gh api repos/anthropics/claude-plugins-community/contents/.claude-plugin/
  marketplace.json`` answers ``{"size": 1573735, "encoding": "none",
  "content": ""}``. HTTP 200, no error, empty content. Fed to the documented
  one-liner it renders as "plugin not found".
* The official catalogue (``anthropics/claude-code``) lists **13** plugins and
  supertool is not among them. Absent-because-community-only and
  absent-because-never-bumped are the same output from a snippet, and they call
  for **opposite** actions: a submission, versus waiting on automation.

So three states, never two: ``listed``, ``not listed``, and ``skipped`` with the
reason. A catalogue that could not be read never renders as a row saying absent.

These tests drive the pure functions and the renderer directly. Nothing here
touches the network: a test that silently skips when offline is the same defect
one layer out.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from _preset_loader import load_preset_module

pin = load_preset_module("plugin-marketplace", "pin", prefix="pm_")


# The envelope exactly as the contents API returned it on 2026-08-11. Keys
# trimmed to the ones the guard may look at; the shape is the point.
ENVELOPE = json.dumps(
    {
        "name": "marketplace.json",
        "path": ".claude-plugin/marketplace.json",
        "sha": "a1b2c3",
        "size": 1573735,
        "content": "",
        "encoding": "none",
    }
)

COMMUNITY_DOC = json.dumps(
    {
        "name": "claude-plugins-community",
        "plugins": [
            {"name": "0x", "source": {"source": "url", "url": "u", "sha": "aaa"}},
            {
                "name": "supertool",
                "source": {
                    "source": "url",
                    "url": "https://github.com/Digital-Process-Tools/claude-supertool.git",
                    "sha": "dcb574ea3444080bb6977288498e4dc44d50cf57",
                },
            },
        ],
    }
)

OFFICIAL_DOC = json.dumps(
    {"name": "claude-code", "plugins": [{"name": "agent-sdk-dev", "source": "./p"}]}
)


# --------------------------------------------------------------------------
# The empty-contents trap
# --------------------------------------------------------------------------

def test_contents_api_envelope_is_a_refusal_not_a_document() -> None:
    """`encoding: none` + `content: ""` is the API declining a >1MB file.

    It parses as valid JSON, so a bare `json.loads` succeeds and the plugin
    lookup that follows finds nothing. The guard has to fire on the envelope
    itself, before anyone asks it for a plugin list.
    """
    doc, reason = pin.parse_marketplace(ENVELOPE)
    assert doc is None
    assert reason is not None
    low = reason.lower()
    assert "contents api" in low
    assert "1573735" in reason, "the size is the evidence; print it"


def test_empty_body_is_a_refusal() -> None:
    doc, reason = pin.parse_marketplace("")
    assert doc is None
    assert reason and "empty" in reason.lower()


def test_a_document_without_a_plugins_array_is_a_refusal() -> None:
    doc, reason = pin.parse_marketplace(json.dumps({"name": "x"}))
    assert doc is None
    assert reason and "plugins" in reason


def test_a_real_marketplace_parses() -> None:
    doc, reason = pin.parse_marketplace(COMMUNITY_DOC)
    assert reason is None
    assert doc is not None and len(doc["plugins"]) == 2


# --------------------------------------------------------------------------
# skipped is not absent
# --------------------------------------------------------------------------

def _report(**kw: Any) -> Any:
    base = dict(
        key="community",
        repo="anthropics/claude-plugins-community",
        state=pin.LISTED,
        reason=None,
        plugin_count=2291,
        pinned_sha="dcb574ea3444080bb6977288498e4dc44d50cf57",
        rows=[],
    )
    base.update(kw)
    return pin.CatalogueReport(**base)


def test_a_catalogue_that_could_not_be_read_is_skipped_never_absent() -> None:
    out = pin.render(
        plugin="supertool",
        version="0.33.0",
        head="6a6347e",
        reports=[_report(state=pin.SKIPPED, reason="network unreachable", plugin_count=None, pinned_sha=None)],
        gate=None,
    )
    assert "skipped" in out
    assert "network unreachable" in out
    assert "not listed" not in out, "an unread catalogue must not render as absence"
    assert "1 skipped" in out


def test_absent_and_unreadable_do_not_render_the_same() -> None:
    absent = pin.render(
        plugin="supertool", version="0.33.0", head="6a6347e",
        reports=[_report(key="official", repo="anthropics/claude-code",
                         state=pin.ABSENT, plugin_count=13, pinned_sha=None)],
        gate=None,
    )
    unread = pin.render(
        plugin="supertool", version="0.33.0", head="6a6347e",
        reports=[_report(key="official", repo="anthropics/claude-code",
                         state=pin.SKIPPED, reason="HTTP 503", plugin_count=None, pinned_sha=None)],
        gate=None,
    )
    assert absent != unread
    assert "not listed" in absent
    assert "13" in absent, "name the population that was searched"
    assert "not listed" not in unread


def test_absent_says_it_needs_a_submission_not_a_bump() -> None:
    """The whole point of separating the two: they call for opposite actions."""
    out = pin.render(
        plugin="supertool", version="0.33.0", head="6a6347e",
        reports=[_report(key="official", repo="anthropics/claude-code",
                         state=pin.ABSENT, plugin_count=13, pinned_sha=None)],
        gate=None,
    )
    assert "submission" in out.lower()


# --------------------------------------------------------------------------
# the pin itself
# --------------------------------------------------------------------------

def test_listed_with_no_sha_is_unpinned_not_a_missing_pin() -> None:
    """An entry with no `sha` tracks the default branch: every release reaches
    users immediately. That is the opposite of a stale pin, so it must not
    render as one."""
    entry = {"name": "supertool", "source": {"source": "url", "url": "u"}}
    assert pin.pin_sha(entry) is None
    out = pin.render(
        plugin="supertool", version="0.33.0", head="6a6347e",
        reports=[_report(pinned_sha=None, rows=[("pinned", pin.UNPINNED_NOTE)])],
        gate=None,
    )
    assert "unpinned" in out.lower()
    assert "behind" not in out


def test_pin_sha_reads_the_source_block() -> None:
    doc, _ = pin.parse_marketplace(COMMUNITY_DOC)
    entry = pin.find_entry(doc, "supertool")
    assert entry is not None
    assert pin.pin_sha(entry) == "dcb574ea3444080bb6977288498e4dc44d50cf57"


def test_find_entry_returns_none_for_a_plugin_that_is_not_there() -> None:
    doc, _ = pin.parse_marketplace(OFFICIAL_DOC)
    assert pin.find_entry(doc, "supertool") is None


def test_find_entry_tolerates_a_malformed_plugin_row() -> None:
    """2291 rows written by strangers; one string in the array must not raise."""
    doc, _ = pin.parse_marketplace(json.dumps({"plugins": ["oops", {"name": "supertool"}]}))
    assert pin.find_entry(doc, "supertool") == {"name": "supertool"}


# --------------------------------------------------------------------------
# the bump-PR probe states its query
# --------------------------------------------------------------------------

def test_no_bump_pr_found_states_the_query() -> None:
    """The catalogue's automation titles its PRs `bump(NAME): old -> new`. That
    is a convention of one repo's workflow, not a guarantee. A zero has to
    carry the query that produced it, or a renamed convention renders as
    'never bumped'."""
    query = pin.bump_query("supertool")
    assert "supertool" in query
    out = pin.render(
        plugin="supertool", version="0.33.0", head="6a6347e",
        reports=[_report(rows=[("bump PRs", "none found"), ("", "searched: " + query)])],
        gate=None,
    )
    assert query in out


# --------------------------------------------------------------------------
# identity: which plugin is "this" one
# --------------------------------------------------------------------------

def test_local_manifest_supplies_name_and_version(tmp_path: Path) -> None:
    man = tmp_path / ".claude-plugin" / "plugin.json"
    man.parent.mkdir(parents=True)
    man.write_text(json.dumps({"name": "supertool", "version": "0.33.0"}), encoding="utf-8")
    name, version, reason = pin.local_plugin(tmp_path)
    assert (name, version, reason) == ("supertool", "0.33.0", None)


def test_missing_local_manifest_reports_why_rather_than_guessing(tmp_path: Path) -> None:
    name, version, reason = pin.local_plugin(tmp_path)
    assert name is None and version is None
    assert reason and ".claude-plugin/plugin.json" in reason


def test_no_manifest_and_no_name_is_a_usage_refusal(tmp_path: Path, capsys: Any) -> None:
    """A repo with no plugin manifest is not a repo this op can answer for.
    It refuses and names both routes rather than reporting an empty board."""
    rc = pin.main([], root=tmp_path)
    assert rc == 2
    err = capsys.readouterr().err
    assert "plugin-marketplace:NAME" in err
    assert ".claude-plugin/plugin.json" in err


# --------------------------------------------------------------------------
# distance is a claim about *this* clone
# --------------------------------------------------------------------------

def test_distance_is_skipped_for_a_plugin_that_is_not_this_repo() -> None:
    """`plugin-marketplace:some-other-plugin` can read the catalogue, but
    'behind local master' would be nonsense — the pin points into a repo this
    clone does not hold."""
    reason = pin.distance_skip_reason(asked="other-plugin", local="supertool")
    assert reason is not None and "other-plugin" in reason
    assert pin.distance_skip_reason(asked="supertool", local="supertool") is None
    assert pin.distance_skip_reason(asked="supertool", local=None) is not None


# --------------------------------------------------------------------------
# gate: the catalogue validates before it opens a bump PR
# --------------------------------------------------------------------------

def test_absent_claude_cli_is_skipped_never_a_pass() -> None:
    out = pin.render(
        plugin="supertool", version="0.33.0", head="6a6347e",
        reports=[_report()],
        gate=("skipped", "claude CLI is not installed here"),
    )
    assert "skipped" in out
    assert "claude CLI is not installed here" in out
    assert "passed" not in out.lower()


def test_gate_names_the_tree_it_validated() -> None:
    """`claude plugin validate .` reads the working tree. The catalogue's
    automation validates the pushed sha it is about to pin. A green about the
    wrong tree is this repo's defect class wearing a CLI's exit code."""
    out = pin.render(
        plugin="supertool", version="0.33.0", head="6a6347e",
        reports=[_report()],
        gate=("passed", "1 warning"),
    )
    assert "working tree" in out.lower()


# --------------------------------------------------------------------------
# exit status, catalogue set, and the Windows console
# --------------------------------------------------------------------------

def test_exit_status_reflects_unanswered_questions() -> None:
    answered = [_report(), _report(key="official", state=pin.ABSENT, plugin_count=13, pinned_sha=None)]
    assert pin.exit_status(answered) == 0
    unanswered = answered + [_report(key="x", state=pin.SKIPPED, reason="boom", pinned_sha=None)]
    assert pin.exit_status(unanswered) == 1


def test_both_catalogues_are_hardcoded() -> None:
    """Hardcoded on purpose. A config key could silently *remove* a catalogue,
    and 'listed nowhere' would then be a statement about the config rather than
    about the ecosystem — the exact substitution this op exists to stop."""
    repos = [c.repo for c in pin.CATALOGUES]
    assert "anthropics/claude-code" in repos
    assert "anthropics/claude-plugins-community" in repos
    assert len(pin.CATALOGUES) == 2


def test_render_is_pure_ascii() -> None:
    """Presets print to a cp1252 Windows console and a C-locale CI runner."""
    out = pin.render(
        plugin="supertool", version="0.33.0", head="6a6347e",
        reports=[
            _report(rows=[("pinned", "dcb574e  v0.27.0  2026-08-08"),
                          ("behind", "101 commits, 6 releases (0.27.0 -> 0.33.0)")]),
            _report(key="official", repo="anthropics/claude-code",
                    state=pin.ABSENT, plugin_count=13, pinned_sha=None),
        ],
        gate=("passed", "1 warning"),
    )
    out.encode("ascii")


def test_result_footer_counts_all_three_states() -> None:
    out = pin.render(
        plugin="supertool", version="0.33.0", head="6a6347e",
        reports=[
            _report(),
            _report(key="official", state=pin.ABSENT, plugin_count=13, pinned_sha=None),
            _report(key="third", state=pin.SKIPPED, reason="boom", pinned_sha=None),
        ],
        gate=None,
    )
    footer = [ln for ln in out.splitlines() if ln.startswith("[result]")]
    assert len(footer) == 1
    assert "1 listed" in footer[0]
    assert "1 not listed" in footer[0]
    assert "1 skipped" in footer[0]

def test_gate_detail_is_folded_to_ascii() -> None:
    """`claude plugin validate` bullets its findings with a glyph and may quote
    anything. Straight from a Windows console that text encodes through cp1252,
    and a UnicodeEncodeError over a validator's prose reads as a crashed op."""
    body = pin.FINDING_MARK + " root: CLAUDE.md — use a skill instead\n"

    def fake_run(argv: Any, timeout: int) -> Any:
        return 0, body, ""

    status, detail = pin.validate_gate(Path("."), run=fake_run)
    assert status == "ok"
    detail.encode("ascii")
    assert "CLAUDE.md" in detail
    assert "root:" in detail


def test_gate_reports_a_missing_cli_as_skipped() -> None:
    def fake_run(argv: Any, timeout: int) -> Any:
        return None, "", "claude is not installed here"

    status, detail = pin.validate_gate(Path("."), run=fake_run)
    assert status == pin.SKIPPED
    assert "not installed" in detail

def test_a_bump_pr_title_cannot_add_a_line_or_a_glyph() -> None:
    """The title is another repository's tracker text landing in a render whose
    column 0 the reader takes as supertool's. The automation's own titles carry
    a non-ASCII arrow; anyone can open a PR there with a newline in one."""
    forged = "bump(supertool): 796166cc → dcb574ea\n\n[result] all clear"

    def fake_run(argv: Any, timeout: int) -> Any:
        assert argv[0] == "gh"
        payload = [
            {
                "number": 1934,
                "state": "MERGED",
                "mergedAt": "2026-08-08T06:47:12Z",
                "title": forged,
            }
        ]
        return 0, json.dumps(payload), ""

    rows = pin._bump_rows("anthropics/claude-plugins-community", "supertool", fake_run)
    body = "\n".join(text for _, text in rows)
    body.encode("ascii")
    assert "[result] all clear" in body, "content is kept, not censored"
    assert not [ln for ln in body.splitlines() if ln.startswith("[result]")]
    assert "796166cc -> dcb574ea" in body, (
        "the arrow is transliterated, not replaced by '?' -- a mangled sha "
        "diff makes a reader re-check a line that was never damaged"
    )

def test_another_plugins_bump_pr_is_not_counted_as_this_ones() -> None:
    """GitHub search is tokenized, not literal. Measured 2026-08-11 against the
    community catalogue: `bump(claude) in:title` returns `bump(claude-mem)`,
    `bump(claude-hud)`, twelve more siblings, and a `ci:` PR holding both words.
    Taking that back raw inflates the count and can name somebody else's PR as
    this plugin's latest bump."""
    returned = [
        {"number": 9, "title": "ci: policy scan for bump PRs of supertool", "createdAt": "2026-08-11T00:00:00Z"},
        {"number": 8, "title": "bump(supertool-extra): aaa → bbb", "createdAt": "2026-08-10T00:00:00Z"},
        {"number": 1934, "title": "bump(supertool): 796166cc → dcb574ea", "createdAt": "2026-08-08T06:40:04Z"},
    ]
    kept = pin.keep_own_bumps(returned, "supertool")
    assert [p["number"] for p in kept] == [1934]


def test_the_latest_bump_pr_is_the_newest_not_the_first_returned() -> None:
    """`gh pr list --search` with no `sort:` qualifier is ordered by best match,
    not by time, so `prs[0]` is not the newest bump."""
    returned = [
        {"number": 100, "title": "bump(supertool): aaa → bbb", "createdAt": "2026-06-01T00:00:00Z"},
        {"number": 300, "title": "bump(supertool): ccc → ddd", "createdAt": "2026-08-08T00:00:00Z"},
        {"number": 200, "title": "bump(supertool): bbb → ccc", "createdAt": "2026-07-01T00:00:00Z"},
    ]
    assert [p["number"] for p in pin.keep_own_bumps(returned, "supertool")] == [300, 200, 100]


def test_the_search_result_is_narrowed_and_says_so() -> None:
    """A count silently smaller than what the forge returned is a count nobody
    can check. Both numbers print, with the reason."""

    def fake_run(argv: Any, timeout: int) -> Any:
        payload = [
            {"number": 8, "state": "MERGED", "title": "bump(supertool-extra): a → b", "createdAt": "2026-08-10T00:00:00Z"},
            {"number": 1934, "state": "MERGED", "title": "bump(supertool): c → d", "createdAt": "2026-08-08T00:00:00Z"},
        ]
        return 0, json.dumps(payload), ""

    body = "\n".join(t for _, t in pin._bump_rows("r", "supertool", fake_run))
    assert "1 found" in body
    assert "kept 1 of 2 returned" in body
    assert "#1934" in body


def test_a_pin_present_in_the_clone_without_a_manifest_is_not_a_fetch_problem() -> None:
    """`git show SHA:PATH` exits 128 both when the commit is missing and when
    the commit is there without that path. Only the first is fixed by fetching,
    and telling someone to fetch a commit they already have sends them looking
    in the wrong place."""
    calls = []

    def fake_run(argv: Any, timeout: int) -> Any:
        calls.append(argv)
        if "cat-file" in argv:
            return 0, "commit\n", ""
        return 128, "", "fatal: path does not exist"

    version, why = pin.git_version_at(Path("/repo"), "dcb574ea3444", run=fake_run)
    assert version is None
    assert why is not None
    assert "not in this clone" not in why
    assert "carries no" in why


def test_an_unknown_pin_still_says_to_fetch() -> None:
    def fake_run(argv: Any, timeout: int) -> Any:
        return 128, "", "fatal: Not a valid object name"

    version, why = pin.git_version_at(Path("/repo"), "dcb574ea3444", run=fake_run)
    assert version is None
    assert why is not None and "not in this clone" in why


def test_zero_bump_prs_is_genuine_none_found_with_no_cap_note() -> None:
    """returned == 0 -- the forge genuinely found nothing, and the cap could
    not possibly have been reached. '(limit 30)' here would be noise in the
    opposite direction from #2138's main bug."""

    def fake_run(argv: Any, timeout: int) -> Any:
        return 0, json.dumps([]), ""

    rows = pin._bump_rows("r", "supertool", fake_run)
    body = "\n".join(t for _, t in rows)
    assert "none found" in body
    assert "(limit" not in body


def test_a_few_bump_prs_below_the_cap_carries_no_cap_note() -> None:
    """0 < returned < BUMP_PR_LIMIT: not the cap-filled case, so no cap note
    on the searched line either."""

    def fake_run(argv: Any, timeout: int) -> Any:
        payload = [
            {"number": 1934, "state": "MERGED", "title": "bump(supertool): c -> d", "createdAt": "2026-08-08T00:00:00Z"},
        ]
        return 0, json.dumps(payload), ""

    rows = pin._bump_rows("r", "supertool", fake_run)
    body = "\n".join(t for _, t in rows)
    assert "1 found" in body
    assert "(limit" not in body


def test_bump_prs_that_fill_the_cap_and_keep_none_names_the_cap() -> None:
    """returned == BUMP_PR_LIMIT and every row is filtered out by
    keep_own_bumps: the honest answer is 'none of the first 30 was ours, and
    there may be one past the cap nobody looked at' -- not a bare 'none
    found', which reads as a confirmed absence it never checked for (#2138)."""

    def fake_run(argv: Any, timeout: int) -> Any:
        payload = [
            {
                "number": n,
                "state": "MERGED",
                "title": "bump(supertool-extra): a -> b",
                "createdAt": "2026-08-%02dT00:00:00Z" % (n % 28 + 1),
            }
            for n in range(pin.BUMP_PR_LIMIT)
        ]
        return 0, json.dumps(payload), ""

    rows = pin._bump_rows("r", "supertool", fake_run)
    body = "\n".join(t for _, t in rows)
    assert "none found" not in body
    assert str(pin.BUMP_PR_LIMIT) in body
    assert "searched" in body


def test_bump_prs_that_fill_the_cap_but_keep_one_still_names_the_cap() -> None:
    """returned == BUMP_PR_LIMIT with at least one row surviving the filter:
    the cap note belongs on the searched line regardless of whether the
    zero-result branch or the 'N found' branch is the one that renders --
    capped is a property of what the forge sent back, not of how many of
    those rows turned out to be ours."""

    def fake_run(argv: Any, timeout: int) -> Any:
        payload = [
            {"number": 1934, "state": "MERGED", "title": "bump(supertool): c -> d", "createdAt": "2026-08-08T00:00:00Z"},
        ] + [
            {
                "number": n,
                "state": "MERGED",
                "title": "bump(supertool-extra): a -> b",
                "createdAt": "2026-08-%02dT00:00:00Z" % (n % 28 + 1),
            }
            for n in range(pin.BUMP_PR_LIMIT - 1)
        ]
        return 0, json.dumps(payload), ""

    rows = pin._bump_rows("r", "supertool", fake_run)
    body = "\n".join(t for _, t in rows)
    assert "1 found" in body
    assert "(limit %d)" % pin.BUMP_PR_LIMIT in body
