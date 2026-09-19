#!/usr/bin/env python3
"""youtube_auth[:status] -- the one-time OAuth2 browser flow for write ops (#227).

`youtube_auth` runs the flow and stores a refresh token.
`youtube_auth:status` reports what is cached without touching the network or
opening anything, which is the form to reach for when the question is "is this
machine set up" rather than "set this machine up".

Separate from the write ops on purpose: a write op that can open a browser and
block for five minutes hangs a non-interactive session with no output, and that
failure is indistinguishable from a slow API. `youtube_comment` refuses and
names this command instead.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))  # for _console
from _console import use_utf8_stdout  # noqa: E402
from _oauth import (  # noqa: E402
    CLIENT_SECRET_PATH,
    SCOPE,
    TOKEN_PATH,
    OAuthError,
    _read_token_file,
    authorize,
)
from _sanitize import safe_short  # noqa: E402


def _status() -> int:
    """Three states, and `cannot tell` is one of them."""
    print(f"client secret: {CLIENT_SECRET_PATH} "
          f"{'present' if CLIENT_SECRET_PATH.is_file() else 'ABSENT'}")
    try:
        cached = _read_token_file()
    except OAuthError as e:
        # Same escaping as the scope print and main()'s error arm below --
        # this wraps a local-file OSError/JSONDecodeError rather than a
        # network body, but the same "misreports, not forges" reasoning
        # applies, and it is not guaranteed single-line either.
        print(f"token cache:   {TOKEN_PATH} UNREADABLE -- "
              f"{repr(safe_short(str(e), 300))}")
        print("verdict:       CANNOT TELL -- neither authorised nor known "
              "unauthorised. Delete the file and run youtube_auth.")
        return 1
    if not cached:
        print(f"token cache:   {TOKEN_PATH} absent")
        print("verdict:       NOT AUTHORISED -- run youtube_auth")
        return 1
    if not cached.get("refresh_token"):
        print(f"token cache:   {TOKEN_PATH} present but carries no refresh_token")
        print("verdict:       NOT AUTHORISED -- run youtube_auth")
        return 1
    left = float(cached.get("expires_at") or 0) - time.time()
    freshness = (f"access token valid for {int(left)}s" if left > 0
                 else "access token expired (it is refreshed on next use)")
    print(f"token cache:   {TOKEN_PATH} present, {freshness}")
    # The scope string comes back from the token cache written by whatever
    # Google's token endpoint returned -- not attacker-chosen, but the same
    # "misreports, not forges" reasoning as the OAuth error bodies below
    # applies: a newline in it would still land at column 0 of this receipt
    # (trap.d/227.oauth-error-body-unescaped-in-receipt.md).
    print(f"scope:         "
          f"{repr(safe_short(str(cached.get('scope', '(unrecorded)')), 300))}")
    if SCOPE not in str(cached.get("scope", "")):
        print(f"verdict:       AUTHORISED, but {SCOPE} is not in the recorded "
              "scope -- writes will 403. Re-run youtube_auth.")
        return 1
    print("verdict:       AUTHORISED for write ops")
    return 0


def main(arg: str) -> None:
    # `youtube_auth:status` prints the recorded scope and the token path, both
    # read off disk and neither guaranteed ASCII -- a home directory with a
    # non-Latin username is enough.
    use_utf8_stdout()
    if arg.strip().lower() in ("status", "check"):
        sys.exit(_status())
    if arg.strip():
        sys.stderr.write("ERROR: usage youtube_auth[:status]\n")
        sys.exit(2)
    try:
        payload = authorize()
    except OAuthError as e:
        sys.stderr.write(f"ERROR: {repr(safe_short(str(e), 300))}\n")
        sys.exit(1)
    print(f"youtube_auth OK token={TOKEN_PATH} scope={payload.get('scope', SCOPE)}")
    print("Next: supertool 'youtube_auth:status' to confirm, then "
          "youtube_comment:VIDEO|TEXT to write.")


if __name__ == "__main__":
    main(":".join(sys.argv[1:]) if len(sys.argv) > 1 else "")
