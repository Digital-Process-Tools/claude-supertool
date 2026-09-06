"""One symlink decision for the whole suite, probed rather than assumed (#1143).

Nine symlink-dependent tests each answered "can I make a symlink here" for
themselves, in five different spellings, split into two kinds:

  * ``skipif(os.name == "nt")`` / ``sys.platform == "win32"`` /
    ``platform.system() == "Windows"``. These are not checks. They hardcode
    "Windows cannot", so a runner that *can* -- Developer Mode, an elevated
    service account -- still never executes them, and the suite reports a
    coverage it does not have on the platform whose reds are load-bearing in
    this repo.
  * a try/except around ``os.symlink`` with a per-file skip message. Honest,
    but five wordings inside a leg reporting ~680 skips is not a legible blind
    spot.

**The count this feeds is a subset, and says so (#1274).** Only the two
mechanisms in this module -- ``requires_symlink`` and ``require_symlink()`` --
produce a skip carrying ``TOKEN``. A test held off a privilege-less runner by an
unrelated collection-time marker skips without one, and a symlink call inside an
``except OSError`` arm does not skip at all. ``conftest`` prints the denominator
and the exclusions next to the number rather than letting it read as a total;
``tests/test_symlink_gating_register_1232.py`` is the full population.

``symlink_support()`` asks the filesystem once per process and caches the
answer together with its reason. Where the privilege is present the tests now
run -- including on Windows, which no platform-name gate could ever allow.
Where it is absent they still skip, but under one shared reason carrying
``TOKEN``, which ``conftest`` states in the report header and counts in the
terminal summary.

**A junction is not a symlink.** ``os.symlink`` can return on NTFS having made
something ``os.path.islink`` denies; a fixture built on one exercises the
non-link branch and asserts nothing -- a vacuous pass on the one platform that
matters. The probe therefore checks ``islink`` on what it just created, and
reports unavailable rather than claiming a capability it does not have.

``PLATFORM_SEMANTICS`` is the deliberate escape hatch, and it is narrow: a test
whose *assertion* is platform-specific -- ``test_read.py::test_read_meta_symlink``
compares a rendered link target that Windows spells through a UNC prefix -- is
not blocked by a missing privilege and must not pretend to be. It stays
platform-gated, and prefixing its reason with this constant is how it says so
out loud instead of hiding among the capability skips.
"""
from __future__ import annotations

import os
import tempfile
from typing import Optional, Tuple

import pytest

#: Grep handle. Appears in every skip this module produces, so `N skipped` in a
#: Windows leg can be resolved to `N skipped for this one stated reason`.
TOKEN = "symlink-capability(#1143)"

#: Prefix for a skip that is genuinely about platform *semantics*, not about the
#: create-symlink privilege. Declared rather than inferred -- see module docstring.
PLATFORM_SEMANTICS = "platform symlink semantics, not a privilege (#1143): "

_PROBE = None  # type: Optional[Tuple[bool, str]]


def _probe() -> Tuple[bool, str]:
    """Actually create a symlink, and check what came back."""
    try:
        with tempfile.TemporaryDirectory() as d:
            # A real target, not a dangling one. `os.symlink` picks file-vs-dir
            # link type on Windows from whether the target exists, and the
            # directory this probe runs in has to be removable afterwards on
            # every platform -- a probe that leaves scratch behind is its own
            # small defect.
            target = os.path.join(d, "probe.target")
            with open(target, "w", encoding="utf-8") as fh:
                fh.write("")
            link = os.path.join(d, "probe.link")
            os.symlink(target, link)
            is_link = os.path.islink(link)
    except (OSError, NotImplementedError, AttributeError) as e:
        return False, "{0}: {1}".format(type(e).__name__, e)
    if not is_link:
        return False, (
            "os.symlink returned without raising, but os.path.islink denies the "
            "result -- an NTFS junction, which exercises the non-link branch and "
            "would assert nothing")
    return True, ""


def symlink_support() -> Tuple[bool, str]:
    """``(available, reason)``. Probed once per process, then cached.

    ``reason`` is empty when available and always non-empty when not: a bare
    ``False`` would be the absence-without-a-reason this repo keeps filing.
    """
    global _PROBE
    if _PROBE is None:
        _PROBE = _probe()
    return _PROBE


def skip_reason() -> str:
    """The one shared skip reason, or ``""`` when there is nothing to skip."""
    available, why = symlink_support()
    if available:
        return ""
    return TOKEN + ": cannot create a symlink here -- " + why


def verdict_line() -> str:
    """One line for the report header -- printed on green legs too."""
    available, why = symlink_support()
    if available:
        return TOKEN + ": available -- symlink-dependent tests will run here"
    return TOKEN + ": unavailable -- " + why


def require_symlink() -> None:
    """Runtime form, for a test that needs the privilege mid-body."""
    if not symlink_support()[0]:
        pytest.skip(skip_reason())


#: Grep handle for the second capability gap this module answers: not "can I
#: create a symlink" but "can I set a symlink's OWN mtime without following
#: it to the target". Distinct from `TOKEN` on purpose -- a runner can create
#: symlinks fine (Developer Mode, an elevated account) and still lack this:
#: measured on GitHub Actions `windows-latest`, Python 3.9, `os.utime(link,
#: times, follow_symlinks=False)` raised `NotImplementedError: utime:
#: follow_symlinks unavailable on this platform` even though the symlink
#: itself was created without incident one line above.
UTIME_TOKEN = "symlink-utime-capability(#1379)"


def require_symlink_utime() -> None:
    """Skip a test that needs to set a symlink's own mtime, where the
    platform's `os.utime` cannot do that without following the link.

    Checked separately from `require_symlink()`: creating the symlink can
    succeed while timing it (without following) still raises, so a test
    gated only on the create privilege would fail rather than skip there.
    """
    require_symlink()
    if os.utime not in os.supports_follow_symlinks:
        pytest.skip(
            UTIME_TOKEN + ": os.utime(..., follow_symlinks=False) is not "
            "supported on this platform")


#: Decorator form. Evaluated at import, like every other ``skipif``.
requires_symlink = pytest.mark.skipif(
    not symlink_support()[0],
    reason=skip_reason() or (TOKEN + ": available"),
)
