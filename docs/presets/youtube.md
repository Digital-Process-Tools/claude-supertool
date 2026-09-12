# youtube

Discovery ops for YouTube via the Data API v3 -- search for videos, read one's
metadata plus its top comments, and list a channel's uploads. API-key auth
only: this slice does not implement the OAuth2 write ops (`youtube_comment`,
`youtube_reply`, `youtube_like`, `youtube_status_since`) the original request
also described -- see "Scope" below.

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

Preset JSON: `presets/youtube.json`. Helper scripts: `presets/youtube/` -- `search.py`, `read.py`, `list.py`, plus `_auth.py` (API key resolution), `_yt.py` (the shared GET helper) and `_sanitize.py` (untrusted-content wrapping). The `{path}` placeholder in `cmd` resolves to `presets/youtube/` at runtime.

Every call goes through `presets/youtube/_yt.py::get()`, which in turn goes through `presets/_http.py`'s shared opener -- so the API key travels only in the query string of a same-origin request and is redacted out of any error text before it reaches an exception message, and no bare `urllib.request.urlopen(` call site was introduced (`tests/test_security_redirect.py::test_no_bare_urlopen_call_sites_remain_under_presets` sweeps for exactly that). See [contributing.md](../contributing.md#http-requests-go-through-presets_httppy).

## Scope: read ops only

[#227](https://github.com/Digital-Process-Tools/claude-supertool/issues/227), the issue this preset implements, proposed seven ops and an OAuth2 write path (`youtube_comment`, `youtube_reply`, `youtube_like`, `youtube_status_since`) alongside the three read ops here, but its own "Implementation order" section proposes splitting the work into two pull requests -- read ops first, API-key only, "low blast radius" in its own words, with the OAuth2 flow, an audit log and a rate cap for the write ops to follow behind their own review. This preset implements only the first half. A follow-up issue covers the write ops, their OAuth2 token cache, and the audience-match/rate-limit guardrails the original issue also describes for them.
