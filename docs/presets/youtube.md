# youtube

Discovery and engagement ops for YouTube via the Data API v3 -- search for
videos, read one's metadata plus its top comments, list a channel's uploads,
post a comment or a reply, like a video, and sweep one's own videos for new
comments. The read ops (`youtube_search`/`youtube_read`/`youtube_list`) use an
API key; `youtube_comment`/`youtube_reply`/`youtube_like` write as the
authorised user over OAuth2, and `youtube_status_since` reads over the same
OAuth2 grant because "own videos" is only answerable for the authorised
account (#227, #2593).

## Requires

- `python3`
- `YOUTUBE_API_KEY` env var -- an API key from the
  [Google Cloud console](https://console.cloud.google.com/apis/credentials),
  with the YouTube Data API v3 enabled on that project. Alternatively,
  `~/.config/youtube/api_key`, or a `.youtube-api-key` file in the current
  directory (first hit wins, in that order -- the same resolution order
  `presets/bluesky/_auth.py` uses for its own credentials).

## Ops

| Op | Syntax | What it returns |
|----|--------|-----------------|
| `youtube_search` | `youtube_search:QUERY[\|N]` | Videos matching the query: title, channel, publish date, watch URL |
| `youtube_read` | `youtube_read:VIDEO_ID_OR_URL` | Video title, channel, stats, top N inline comments (with comment IDs) |
| `youtube_list` | `youtube_list:CHANNEL[\|N]` | A channel's uploads: title, publish date, watch URL per video |
| `youtube_auth` | `youtube_auth[:status]` | Runs the one-time OAuth2 consent flow, or reports what is cached |
| `youtube_comment` | `youtube_comment:VIDEO_ID_OR_URL\|TEXT_OR_file://PATH[\|force][\|force-dup]` | Posts one top-level comment, then reads it back and reports whether it could confirm it |
| `youtube_reply` | `youtube_reply:COMMENT_ID\|TEXT_OR_file://PATH[\|force][\|force-dup]` | Posts one reply to an existing comment, then reads it back the same way |
| `youtube_like` | `youtube_like:VIDEO_ID_OR_URL[\|force][\|force-dup]` | Sets this account's rating on a video to "like", then reads the rating back |
| `youtube_status_since` | `youtube_status_since[:ISO]` | New top-level comments on this account's own videos since ISO (default: 24 hours ago) |

`N` defaults to `SUPERTOOL_DEFAULT_LIMIT` (10), same env knob as `bluesky_list`/`bluesky_search`. `youtube_read`'s inline comment count is `SUPERTOOL_INLINE_COMMENTS` (5), same knob `bluesky_read` uses for inline replies.

`CHANNEL` (for `youtube_list`) accepts a channel ID (`UC...`, 24 characters), an `@handle`, a bare name (tried as a handle -- YouTube's legacy `/c/` and `/user/` custom URLs carry no ID of their own), or a full `youtube.com/channel/...` or `youtube.com/@...` URL.

`VIDEO_ID_OR_URL` (for `youtube_read`) accepts a bare video ID, or a `youtube.com/watch?v=...`, `youtu.be/...`, `youtube.com/shorts/...` or `youtube.com/embed/...` URL.

## Quota

The YouTube Data API v3's default daily quota is 10,000 units. `youtube_search` (`search.list`) costs 100 units per call -- the most expensive endpoint in the API, since full-text video search has no cheaper surface. `youtube_read` costs 2 units (`videos.list` + `commentThreads.list`, 1 unit each). `youtube_list` costs 2 units (`channels.list` to resolve the uploads playlist, `playlistItems.list` to read it, 1 unit each).

## Comments disabled degrades, it does not fail

`youtube_read` fetches the video's metadata and its comments as two separate calls. A video with comments disabled answers the second call with `403 Forbidden` -- `youtube_read` reports that as a note in place of the comments section (`--- comments unavailable: ... ---`) rather than failing the whole read, since the video's title, channel and stats are still perfectly readable.

## Untrusted content

`youtube_read`'s description and comment text come from strangers. Both are scanned for known prompt-injection patterns the same way `bluesky_read` scans a post body and its replies -- a `⚠ POSSIBLE INJECTION` line is prefixed when one matches -- and the description is additionally wrapped in `<<UNTRUSTED YOUTUBE-VIDEO CONTENT — START ...>>` markers via `presets/youtube/_sanitize.py`, a straight copy of `presets/bluesky/_sanitize.py` (the comment at the top of that file already documents that it is duplicated per preset directory on purpose, to keep each preset self-contained).

## Authoring notes

Preset JSON: `presets/youtube.json`. Helper scripts: `presets/youtube/` -- `search.py`, `read.py`, `list.py`, `comment.py`, `reply.py`, `like.py`, `status_since.py`, plus `_auth.py` (API key resolution), `_oauth.py` (the OAuth2 flow and token cache), `_sentinel.py` (the write log behind the duplicate/rate guards), `_yt.py` (the shared GET/authorised-call helper) and `_sanitize.py` (untrusted-content wrapping). The `{path}` placeholder in `cmd` resolves to `presets/youtube/` at runtime.

Every call goes through `presets/youtube/_yt.py::get()`, which in turn goes through `presets/_http.py`'s shared opener -- so the API key travels only in the query string of a same-origin request and is redacted out of any error text before it reaches an exception message, and no bare `urllib.request.urlopen(` call site was introduced (`tests/test_security_redirect.py::test_no_bare_urlopen_call_sites_remain_under_presets` sweeps for exactly that). See [contributing.md](../contributing.md#http-requests-go-through-presets_httppy).

## Writing a comment

`youtube_comment` needs OAuth2, not an API key: `commentThreads.insert` acts as
a user. Set it up once.

1. In the [Google Cloud console](https://console.cloud.google.com/apis/credentials),
   Create credentials -> OAuth client ID -> **Desktop app**, on the project that
   already has the YouTube Data API v3 enabled.
2. Download the JSON and save it as `~/.config/youtube/client_secret.json`.
3. `supertool 'youtube_auth'` -- this opens a consent page pointed at a loopback
   port on 127.0.0.1 and stores a refresh token at
   `~/.config/youtube/oauth_token.json`, mode 0600.
4. `supertool 'youtube_auth:status'` to confirm. It answers in three states:
   AUTHORISED, NOT AUTHORISED, and **CANNOT TELL** for a cache that will not
   parse -- which is not the same claim as never having authorised, and has a
   different remedy.

`youtube_auth` is a separate op rather than something `youtube_comment` triggers
on demand. A write op that can open a browser and block for five minutes hangs a
non-interactive session with no output, and that failure looks exactly like a
slow API. An absent token is a refusal naming `youtube_auth` instead.

The flow uses PKCE (RFC 7636) and checks the `state` parameter on the way back.
A Desktop-app client secret ships on the user's machine and is not secret in the
sense the name suggests, and the loopback port is reachable by any local process.

### A 2xx is not visibility

This is the part worth reading twice. YouTube holds or shadow-bans programmatic
comments **silently** -- the insert returns 200, the resource comes back with an
id, and nobody else ever sees it. So `youtube_comment` does not report success on
the write. It reads the comment back afterwards and prints one of three verdicts:

| `read-back:` | Means |
|----|----|
| `verified` | Read back, and the text matches what was sent |
| `MISMATCH` | Read back, and the text does **not** match |
| `could-not-verify` | The read-back itself failed, or returned no such thread |

`could-not-verify` is not a pass and does not render like one. And `verified` is
only a claim about what *this account* can see: the account that posted a held
comment can generally still read it. Confirm in a logged-out browser before
believing a comment landed. The op says so in its own output.

### Guardrails

Three, and each one refuses rather than warns. Two of them take **different**
override tokens, passed as trailing fields in either order: `|force` confirms
the publish, `|force-dup` overrides the sentinel. One does not grant the other.
They shared a single `|force` in the first draft, which left the duplicate guard
off on every invocation that actually published, since `|force` is what an
operator has to pass to publish at all.

- **Confirmation.** No `|force` and no `no_publish_confirm`, no publish. Shared
  with every other publishing op through `presets/_publish_safety.py`. `|force`
  buys this and nothing else.
- **One comment per video, 5 writes per hour.** Recorded in
  `~/.config/youtube/sent.jsonl` (0600). The cap is #227's number and is not
  derived from quota: 50 units a comment against 10,000 a day would allow 200,
  and the number that matters is the one the spam classifier watches. A log that
  exists and **cannot be read** is `cannot-tell`, which is treated as a refusal --
  an unreadable log is not an empty one, and this preset has no delete op, so an
  unknown is the one state where doing nothing is clearly right. A *missing* log
  is a real answer and is fine: nothing was written yet. Overridden by
  `|force-dup`, which is a separate token for exactly this reason.
- **Authorship disclosure.** `[AI-generated]` is appended unless
  `no_publish_disclosure` is set, and the output says `(disclosure: suppressed)`
  when it was not.

`youtube_comment` costs 50 quota units for the insert plus 1 for the read-back.

## Replying to a comment

`youtube_reply:COMMENT_ID|TEXT_OR_file://PATH[|force][|force-dup]` posts one
reply via `comments.insert` (`parentId=COMMENT_ID`), then reads it back through
`comments.list` -- the same three-verdict read-back `youtube_comment` uses
(`verified`/`MISMATCH`/`could-not-verify`), for the same reason.

It shares `youtube_comment`'s guardrails, with one deliberate difference: the
duplicate guard is keyed on the **parent `COMMENT_ID`**, not the video. A
reply is a write against one specific comment thread, and keying it on the
video would refuse a second reply to a *different* comment on a video this
account already replied on once, which is not the duplicate the guard exists
to catch. The written `sent.jsonl` entry's `video_id` field holds the parent
comment id for this op; `op="youtube_reply"` in the same entry is what tells
the two apart when reading the log by hand.

`youtube_reply` costs 50 quota units for the insert plus 1 for the read-back,
the same split `youtube_comment` costs.

## Liking a video

`youtube_like:VIDEO_ID_OR_URL[|force][|force-dup]` sets this account's rating
on a video to "like" via `videos.rate`, then reads it back with
`videos.getRating` and reports the same three verdicts. There is no
authorship-disclosure marker here -- a like carries no text for
`[AI-generated]` to be appended to -- but `|force` still confirms the action
and `|force-dup` is still the separate token that overrides the duplicate
guard, for the same reason `youtube_comment`'s two tokens are kept separate
rather than folded into one.

The duplicate guard here is keyed on the plain `VIDEO_ID`, the same key
`youtube_comment` uses. A comment and a like on the same video therefore
refuse each other without `|force-dup` -- a conservative default (one video,
one footprint, until overridden), not a claim that liking and commenting are
the same action.

`youtube_like` costs 50 quota units for the rate call plus 1 for the
read-back.

## Sweeping for new comments on your own videos

`youtube_status_since[:ISO]` lists new top-level comments on this account's
own videos since `ISO` (default: 24 hours ago), across up to
`SUPERTOOL_DEFAULT_LIMIT` recent uploads. It needs the same OAuth2 grant the
write ops use -- "own videos" is only answerable for the authorised account,
via `channels.list?mine=true` -- but it never writes, so it has no sentinel,
no rate cap and no confirmation gate. A video with comments disabled degrades
to a `--- comments unavailable: ... ---` note for that one video rather than
failing the whole sweep, the same degrade `youtube_read` documents above.

`youtube_status_since` costs 1 unit for `channels.list`, 1 for
`playlistItems.list`, and 1 per video checked for `commentThreads.list` (up to
`SUPERTOOL_DEFAULT_LIMIT` videos) -- so a default sweep costs at most
`SUPERTOOL_DEFAULT_LIMIT + 2` units.

All three reuse `_oauth.get_access_token`, `_yt.authorized`, `_sentinel` and
`_publish_safety` unchanged -- #227's own note that these three ops are
"argv and an endpoint each" against machinery `youtube_auth`/`youtube_comment`
already proved out.
