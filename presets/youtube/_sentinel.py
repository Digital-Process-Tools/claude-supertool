"""The write log behind youtube_comment's two guardrails (#227).

`~/.config/youtube/sent.jsonl`, one JSON object per line, appended after every
successful write. It answers two questions and nothing else:

  * **have we already commented on this video?** YouTube does not dedupe, so a
    retried op posts a second public comment under the same name. There is no
    delete op in this preset, so the sentinel is the only thing standing
    between a retry and a visible double-post.
  * **how many writes in the last hour?** #227 caps this at 5 regardless of
    quota, because YouTube's spam heuristics act on posting *rate* and act
    silently -- the comment returns 200 and is shadow-banned. Quota is not the
    binding constraint and treating it as one is how the account gets flagged.

## Three states, never two

`check()` returns `ok`, a refusal, or **`cannot-tell`**, and the caller treats
the third as a refusal rather than a pass. A log that cannot be read is not an
empty log: an unreadable file, a permissions error or a corrupt line all mean
"we do not know whether this was already posted", and publishing over that
unknown is the defect this repository keeps filing (CLAUDE.md, "An absence
produced by the tool, read as an absence in the world"). The cost of being
wrong in the safe direction is one `|force`; the cost in the other direction
is a public artifact on somebody else's video that this preset cannot remove.

A *missing* file is a different thing and is `ok`: nothing was ever written,
which is a real answer and the normal first-run state.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

CONFIG_DIR = Path(os.path.expanduser("~/.config/youtube"))
LOG_PATH = CONFIG_DIR / "sent.jsonl"

#: #227's cap. Deliberately not derived from the quota budget: 50 units a
#: comment against 10k/day would allow 200, and the number that matters here
#: is the one the spam classifier watches.
MAX_WRITES_PER_HOUR = 5
_HOUR_S = 3600.0


class LogUnreadable(Exception):
    """The log exists but could not be read or parsed. Never an empty log."""


def _entries() -> list:
    """Every recorded write, newest last. Raises `LogUnreadable`, never lies.

    A missing file is `[]` -- nothing was written yet, which is an answer. A
    file that exists and will not parse raises: see the module docstring.
    """
    if not LOG_PATH.is_file():
        return []
    try:
        raw = LOG_PATH.read_text(encoding="utf-8")
    except OSError as e:
        raise LogUnreadable(f"{LOG_PATH}: {e}") from e
    out = []
    for lineno, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            raise LogUnreadable(
                f"{LOG_PATH}:{lineno} is not valid JSON ({e}). The sentinel "
                "cannot say whether this video was already commented on."
            ) from e
        if isinstance(obj, dict):
            out.append(obj)
    return out


def check(video_id: str, *, now: Optional[float] = None) -> tuple[str, str]:
    """`(verdict, reason)` where verdict is "ok", "refuse" or "cannot-tell".

    The caller must treat "cannot-tell" as a refusal. It is returned separately
    from "refuse" so the message can say which of the two happened -- "you
    already commented here" and "I could not find out" are different facts and
    lead to different next actions.
    """
    now = time.time() if now is None else now
    try:
        entries = _entries()
    except LogUnreadable as e:
        return "cannot-tell", (
            f"the write log could not be read -- {e}. Whether this video was "
            "already commented on is UNKNOWN, and a duplicate comment cannot "
            "be deleted by this preset."
        )
    for entry in entries:
        if entry.get("video_id") == video_id:
            when = entry.get("ts", "?")
            cid = entry.get("comment_id", "?")
            return "refuse", (
                f"already commented on {video_id} at {when} "
                f"(comment_id={cid}). YouTube does not dedupe and this preset "
                "has no delete op."
            )
    recent = [e for e in entries
              if isinstance(e.get("epoch"), (int, float))
              and now - float(e["epoch"]) < _HOUR_S]
    if len(recent) >= MAX_WRITES_PER_HOUR:
        oldest = min(float(e["epoch"]) for e in recent)
        wait_s = int(_HOUR_S - (now - oldest)) + 1
        return "refuse", (
            f"{len(recent)} writes in the last hour, cap is "
            f"{MAX_WRITES_PER_HOUR} (#227). YouTube's spam heuristics act on "
            f"rate and act silently. Next slot in about {wait_s}s."
        )
    return "ok", ""


def record(*, op: str, video_id: str, comment_id: str, url: str,
           verification: str) -> None:
    """Append one write. Best-effort: a failure here must not look like a
    failed write, because the write already happened -- it is reported on
    stderr by the caller instead, with what could not be recorded.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "epoch": time.time(),
        "op": op,
        "video_id": video_id,
        "comment_id": comment_id,
        "url": url,
        "verification": verification,
    }
    # 0600 at creation: this file records what the account published and when,
    # which is a behavioural profile even though it holds no credential.
    fd = os.open(str(LOG_PATH), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
