"""#1727 — one ASCII-digit test, shared, on every path a caller can reach.

`str.isdigit()` is True for two classes of character that are not an id, and
they fail differently:

* **Unicode decimals** — `٢` (U+0662), `۲`, `২`. `str.isdecimal()` is True too,
  so `int()` converts them and the op proceeds against a number the caller
  never typed. This is the class the issue names.
* **Superscripts** — `²` (U+00B2), `³`. `str.isdigit()` is True and
  `str.isdecimal()` is False, so `int()` **raises**. The issue does not name
  this class, and it is the one that turns an inconsistency into a defect:
  every `isdigit()`-then-`int()` site is an uncaught `ValueError` on
  caller-supplied text. `presets/watch/tiers/_snapshot.py` already documented
  it; nothing else did.

The issue was filed on `pr_create.py`, where the string is parsed out of `gh`'s
own stdout and the blast radius is genuinely low. The sweep it asked for found
the second class on paths a caller controls directly, and those lead:

    supertool 'gh-issues:per=²'   -> ValueError: invalid literal for int()
    supertool 'gh-prs:per=²'      -> ValueError  (same guard, _filter_tokens.py)
    supertool 'gh-following:²'    -> ValueError
    supertool 'gh-starred:²'      -> ValueError

`presets/_filter_tokens.py::bad_values` is the worst of them: it is the
function whose entire job is to name a filter value the op cannot honour, and
it crashed on the value it exists to refuse — the guard dying on its own input.

Every "must refuse" case below is paired with an ASCII control in the same test,
because a refusal assertion passes when the harness is broken and nothing runs.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parent.parent
PRESETS = ROOT / "presets"
if str(PRESETS) not in sys.path:
    sys.path.insert(0, str(PRESETS))


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, PRESETS / rel)
    assert spec is not None and spec.loader is not None, rel
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


digits = _load("_digits.py", "st_digits_1727")
filter_tokens = _load("_filter_tokens.py", "st_filter_tokens_1727")
job_argv = _load("_job_argv.py", "st_job_argv_1727")
check = _load("github/check.py", "st_gh_check_1727")
pr_merge = _load("github/pr_merge.py", "st_gh_pr_merge_1727")
pr_create = _load("github/pr_create.py", "st_gh_pr_create_1727")
following = _load("github/following.py", "st_gh_following_1727")
starred = _load("github/starred.py", "st_gh_starred_1727")
find_starable = _load("github/find_starable.py", "st_gh_find_starable_1727")
find_followable = _load("github/find_followable.py", "st_gh_find_followable_1727")
gh_run = _load("github/run.py", "st_gh_run_1727")

#: `int()` converts these to something the caller did not type.
DECIMAL_LOOKALIKES = ("٢", "۲", "২", "١٢٣")
#: `int()` raises on these. `str.isdigit()` is True for both groups.
SUPERSCRIPTS = ("²", "³")
NOT_IDS = DECIMAL_LOOKALIKES + SUPERSCRIPTS
LF = chr(10)


# --- the mechanism, pinned so the two classes stay distinguishable ---------

def test_both_classes_pass_isdigit_and_only_one_survives_int() -> None:
    """Not the bar — a control on the premise the rest of the file rests on.

    If CPython ever changed either answer, every assertion below would still
    pass while testing something else.
    """
    for ch in NOT_IDS:
        assert ch.isdigit(), ch
    for ch in DECIMAL_LOOKALIKES:
        assert ch.isdecimal() and int(ch) >= 0, ch
    for ch in SUPERSCRIPTS:
        assert not ch.isdecimal(), ch
        with pytest.raises(ValueError):
            int(ch)


# --- the shared test itself ------------------------------------------------

def test_is_ascii_int_accepts_ascii_digits() -> None:
    for good in ("0", "1", "42", "1727", "00"):
        assert digits.is_ascii_int(good) is True, good


def test_is_ascii_int_refuses_every_non_ascii_digit() -> None:
    for bad in NOT_IDS:
        assert digits.is_ascii_int(bad) is False, bad
    # Same fixture, positive half: a broken import cannot make this pass.
    assert digits.is_ascii_int("123") is True


def test_is_ascii_int_anchors_with_capital_z_not_dollar() -> None:
    """`^[0-9]+$` accepts a trailing newline (#1188). This is the whole reason
    the regex is written once rather than retyped per caller."""
    for bad in ("5" + LF, LF + "5", "", " 5", "5 ", "+5", "-5", "12a", "1_2"):
        assert digits.is_ascii_int(bad) is False, repr(bad)
    assert digits.is_ascii_int("5") is True


# --- presets/_filter_tokens.py — the lead finding --------------------------

DOMAINS = {
    "per": filter_tokens.POSITIVE_INT,
    "iids": filter_tokens.POSITIVE_INT_LIST,
}


def test_bad_values_refuses_a_superscript_instead_of_raising() -> None:
    """`gh-issues:per=²` / `gh-prs:per=²` / `gl-mrs:per=²` raised an uncaught
    ValueError out of the guard whose job is to refuse the value."""
    for bad in NOT_IDS:
        offenders = filter_tokens.bad_values({"per": bad}, DOMAINS)
        assert [k for k, _v, _e in offenders] == ["per"], (bad, offenders)
    # Positive control in the same fixture: a real limit is still accepted.
    assert filter_tokens.bad_values({"per": "30"}, DOMAINS) == []


def test_bad_values_refuses_a_superscript_inside_a_list() -> None:
    for bad in NOT_IDS:
        offenders = filter_tokens.bad_values({"iids": "1,%s,3" % bad}, DOMAINS)
        assert [k for k, _v, _e in offenders] == ["iids"], (bad, offenders)
    assert filter_tokens.bad_values({"iids": "1,2,3"}, DOMAINS) == []


# --- presets/github/check.py — caller argv ---------------------------------

class _Spy:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, value: str) -> int:
        self.calls.append(value)
        return 0


@pytest.fixture()
def check_spies(monkeypatch: Any) -> tuple[_Spy, _Spy]:
    show, listed = _Spy(), _Spy()
    monkeypatch.setattr(check, "_show_check", show)
    monkeypatch.setattr(check, "_list_pr", listed)
    return show, listed


def _check_main(monkeypatch: Any, argv: list[str]) -> int:
    monkeypatch.setattr(sys, "argv", ["check.py"] + argv)
    return check.main()


def test_gh_check_refuses_a_non_ascii_check_run_id(monkeypatch: Any,
                                                   check_spies: Any,
                                                   capsys: Any) -> None:
    show, listed = check_spies
    for bad in NOT_IDS:
        assert _check_main(monkeypatch, [bad]) == 1, bad
        assert "ERROR" in capsys.readouterr().out
    assert show.calls == [] and listed.calls == [], (show.calls, listed.calls)
    # Positive control, same fixture: an ASCII id still reaches the fetch.
    assert _check_main(monkeypatch, ["93211401185"]) == 0
    assert show.calls == ["93211401185"]


def test_gh_check_pr_refuses_a_non_ascii_pr_number(monkeypatch: Any,
                                                   check_spies: Any,
                                                   capsys: Any) -> None:
    show, listed = check_spies
    for bad in NOT_IDS:
        assert _check_main(monkeypatch, ["pr", bad]) == 1, bad
        assert "ERROR" in capsys.readouterr().out
    assert listed.calls == [], listed.calls
    assert _check_main(monkeypatch, ["pr", "1727"]) == 0
    assert listed.calls == ["1727"]


# --- presets/github/pr_merge.py — caller argv ------------------------------

def test_gh_pr_merge_refuses_a_non_ascii_number() -> None:
    for bad in NOT_IDS:
        number, _m, _f, _c, err = pr_merge.parse_argv([bad])
        assert err and number == "", (bad, number, err)
    number, method, _f, _c, err = pr_merge.parse_argv(["1727", "squash"])
    assert (number, method, err) == ("1727", "squash", "")


# --- presets/github/{following,starred}.py — the ValueError sites ----------

class _FakeGh:
    """Stands in for `subprocess.run`. Records the URL it was asked for."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def run(self, args: list[str], **_kw: Any) -> subprocess.CompletedProcess:
        self.urls.append(args[-1])
        return subprocess.CompletedProcess(args, 0, "[]", "")


@pytest.mark.parametrize("mod", [following, starred], ids=["following", "starred"])
def test_limit_arg_never_raises_on_a_superscript(monkeypatch: Any,
                                                 mod: Any) -> None:
    """`supertool 'gh-following:²'` died with `invalid literal for int()`
    before anything was fetched. A junk limit falls back to the default, which
    is what every other non-numeric argument to these ops already did."""
    for bad in NOT_IDS:
        fake = _FakeGh()
        monkeypatch.setattr(mod, "subprocess", fake)
        assert mod.main(bad) == 0, bad
        assert fake.urls and "per_page=30" in fake.urls[0], (bad, fake.urls)
    # Positive control: a real limit is still honoured, so the fix is not
    # "ignore the argument".
    fake = _FakeGh()
    monkeypatch.setattr(mod, "subprocess", fake)
    assert mod.main("7") == 0
    assert "per_page=7" in fake.urls[0], fake.urls


# --- presets/github/find_{starable,followable}.py --------------------------

def test_find_starable_limit_never_raises() -> None:
    for bad in NOT_IDS:
        topic, n = find_starable.parse_args("python|%s" % bad)
        assert topic == "python" and n == 30, (bad, n)
    assert find_starable.parse_args("python|7") == ("python", 7)


def test_find_followable_limit_never_raises() -> None:
    for bad in NOT_IDS:
        repo, n = find_followable.parse_args("o/r|%s" % bad)
        assert repo == "o/r" and n == 100, (bad, n)
    assert find_followable.parse_args("o/r|7") == ("o/r", 7)


# --- presets/github/pr_create.py — the filed site --------------------------

REPO = "Digital-Process-Tools/claude-supertool"


class _CreateHarness:
    def __init__(self, url: str) -> None:
        self.url = url
        self.json_calls: list[list[str]] = []

    def gh(self, args: list[str], timeout: int = 30):
        if args[:2] == ["pr", "create"]:
            return subprocess.CompletedProcess(args, 0, self.url + LF, "")
        raise AssertionError("unexpected gh call: %r" % (args,))

    def gh_json(self, args: list[str], timeout: int = 30):
        self.json_calls.append(list(args))
        if args[:2] == ["repo", "view"]:
            return ({"defaultBranchRef": {"name": "master"}}, "")
        if args[:2] == ["pr", "view"]:
            return ({"statusCheckRollup": [], "headRefOid": "a" * 40,
                     "createdAt": "2999-01-01T00:00:00Z"}, "")
        raise AssertionError("unexpected gh_json call: %r" % (args,))


def _create(monkeypatch: Any, tmp_path: Any, url: str) -> _CreateHarness:
    payload = tmp_path / "pr.json"
    payload.write_text(json.dumps(
        {"repo": REPO, "title": "a change", "base": "master",
         "body": "Closes #1727"}), encoding="utf-8")
    h = _CreateHarness(url)
    monkeypatch.setattr(pr_create, "_gh", h.gh)
    monkeypatch.setattr(pr_create, "_gh_json", h.gh_json)
    monkeypatch.setattr(pr_create, "_current_branch", lambda: ("fix/1727", ""))
    monkeypatch.setattr(sys, "argv", ["pr_create.py", str(payload)])
    pr_create.main()
    return h


def test_pr_create_read_back_is_gated_on_ascii_digits(monkeypatch: Any,
                                                      tmp_path: Any,
                                                      capsys: Any) -> None:
    """The filed line. Low blast radius by construction — the number is parsed
    out of `gh`'s own stdout — and the point is that the gate reads as the same
    decision its neighbour in `run.py` documents."""
    for bad in NOT_IDS:
        h = _create(monkeypatch, tmp_path, "https://github.com/o/r/pull/%s" % bad)
        capsys.readouterr()
        views = [a for a in h.json_calls if a[:2] == ["pr", "view"]]
        assert views == [], (bad, views)
    # Positive control: an ordinary number still triggers the read-back.
    h = _create(monkeypatch, tmp_path, "https://github.com/o/r/pull/1727")
    capsys.readouterr()
    assert [a for a in h.json_calls if a[:2] == ["pr", "view"]], h.json_calls


# --- the two guards that were already right stay right ---------------------

def test_run_and_job_id_guards_are_unchanged_by_the_dedup() -> None:
    """`refuse_run_id` and `refuse_job_id` were the correct spellings before
    this change and now share the test rather than each carrying one."""
    for bad in NOT_IDS:
        assert gh_run.refuse_run_id(bad), bad
        assert job_argv.refuse_job_id("gh-job", "GitHub", bad), bad
    assert gh_run.refuse_run_id("93211401185") == ""
    assert job_argv.refuse_job_id("gh-job", "GitHub", "93211401185") == ""


# --- the recurrence guard the issue actually asked for ---------------------

#: Files this change made answer the question one way. A fourth spelling
#: appearing in one of them is the failure mode #1727 was filed about, so it
#: fails here rather than being noticed by the next audit.
GUARDED = (
    "_filter_tokens.py",
    "_job_argv.py",
    "github/check.py",
    "github/find_followable.py",
    "github/find_starable.py",
    "github/following.py",
    "github/pr.py",
    "github/pr_create.py",
    "github/pr_merge.py",
    "github/run.py",
    "github/starred.py",
)


def test_no_guarded_file_reaches_for_isdigit_again() -> None:
    """Code lines only. Prose in this repo quotes `str.isdigit()` in backticks
    to say why it is wrong, and a test that forbade the word would forbid the
    explanation."""
    offenders: list[str] = []
    for rel in GUARDED:
        path = PRESETS / rel
        assert path.exists(), rel
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or "`" in line:
                continue
            if ".isdigit(" in line or ".isnumeric(" in line:
                offenders.append("%s:%d: %s" % (rel, n, stripped))
    assert offenders == [], LF.join(offenders)


def test_the_recurrence_guard_can_actually_see_one() -> None:
    """The negative assertion above passes when the file list is wrong, the
    paths do not exist, or the reader skips everything. `presets/git/push.py`
    is outside this change and still holds `.isdigit()` code lines, so it is a
    live positive control rather than a synthetic one."""
    path = PRESETS / "git" / "push.py"
    hits = [n for n, line in
            enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if ".isdigit(" in line and not line.strip().startswith("#")
            and "`" not in line]
    assert hits, ("the control file no longer contains what the scan looks "
                  "for — the scan above proves nothing until this is re-aimed")
