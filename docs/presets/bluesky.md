# bluesky

Bluesky publishing, discovery, and engagement via the AT Protocol. Replaces raw `curl` calls against the ATP lexicon — `bluesky_publish` auto-detects URLs and adds clickable facets, `bluesky_read` returns inline replies with AT URIs so you can chain into `bluesky_publish` for threaded replies, and `bluesky_status_since` pulls the native notifications endpoint for a single-call engagement briefing.

## Requires

- `python3`
- `BLUESKY_HANDLE` env var — your handle, e.g. `max-ai-dev.bsky.social`
- `BLUESKY_APP_PASSWORD` env var — app password from [bsky.app/settings/app-passwords](https://bsky.app/settings/app-passwords) (not your login password)

## Ops

| Op | Syntax | What it returns |
|----|--------|-----------------|
| `bluesky_publish` | `bluesky_publish:TEXT_OR_FILE_OR_file://PATH[|REPLY_TO_AT_URI[|force]]` | Published post AT URI + web URL |
| `bluesky_list` | `bluesky_list[:N]` or `bluesky_list:HANDLE[|N]` | Author feed: post text, AT URI, date, like + repost counts |
| `bluesky_read` | `bluesky_read:AT_URI_OR_WEB_URL` | Post body, author, stats, top inline replies with URIs, reply chain hints |
| `bluesky_search` | `bluesky_search:QUERY[|N]` | Recent posts matching query: text, author, AT URI, engagement counts |
| `bluesky_like` | `bluesky_like:AT_URI_OR_WEB_URL[\|force]` | Likes a post; returns confirmation |
| `bluesky_follow` | `bluesky_follow:HANDLE_OR_DID[\|force]` | Follows a user by handle or DID; returns confirmation |
| `bluesky_repost` | `bluesky_repost:AT_URI_OR_WEB_URL` | Reposts (boosts) a post to your followers; returns confirmation |
| `bluesky_status_since` | `bluesky_status_since[:ISO_TIMESTAMP]` | Briefing: new mentions, replies, likes, follows since timestamp (auto-tracks last check) |

## Common workflows

**Post a thread:**
```bash
./supertool 'bluesky_publish:First post in the thread — here is the setup.'
# copy the AT URI from output, then:
./supertool 'bluesky_publish:Second post — the payoff.|at://did:plc:abc/app.bsky.feed.post/3kxyz'
```
`REPLY_TO_AT_URI` chains the second post as a reply, forming a thread. Repeat for as many posts as needed.

**Monitor mentions and follow back recent likers:**
```bash
./supertool 'bluesky_status_since'
# review new likes, then for each liker handle:
./supertool 'bluesky_follow:liker.bsky.social'
```
`bluesky_status_since` uses the native notifications endpoint — one call, no fan-out. Auto-tracks last check in `~/.config/bluesky/last_check`.

**Search for mentions and reply:**
```bash
./supertool 'bluesky_search:claude-supertool|10'
# pick a post AT URI, read it for context:
./supertool 'bluesky_read:https://bsky.app/profile/someone.bsky.social/post/3kabc'
# reply:
./supertool 'bluesky_publish:Thanks for sharing!|at://did:plc:xyz/app.bsky.feed.post/3kabc'
```

## When the duplicate check cannot run

`bluesky_publish` (replies only), `bluesky_like` and `bluesky_follow` each look before they write — for an existing reply to the same thread root, an existing like, an existing follow. Since [#601](https://github.com/Digital-Process-Tools/claude-supertool/issues/601) that check has three outcomes, not two:

| Outcome | What the op does | stderr |
|---------|------------------|--------|
| Nothing found | Writes | — |
| Duplicate found | Refuses, names what it found | `ABORT — already liked at://… Use \|force to override.` |
| **The check could not be made** | Refuses, writes nothing | `ABORT — pre-flight lookup failed for at://… (cannot verify whether already liked). Use \|force to bypass and like anyway.` |

The third row is the new one. It used to be the first row: a failed `getActorLikes`, `getFollows` or `getAuthorFeed` returned the same `False` as a clean lookup, so a platform hiccup authorised the write it was meant to guard. Every one of these is recoverable in one keystroke — `|force`, already the bypass for a genuine duplicate:

```bash
./supertool 'bluesky_like:at://did:plc:abc/app.bsky.feed.post/xyz|force'
./supertool 'bluesky_follow:someone.bsky.social|force'
./supertool 'bluesky_publish:Thanks!|at://did:plc:abc/app.bsky.feed.post/xyz|force'
```

"Could not be made" covers an HTTP or network failure (the AT Protocol helper exits rather than raising, which is why these ops name `SystemExit` explicitly) and, for `bluesky_publish`, a reply target whose thread root cannot be resolved — a root that is unknown is not a root nobody replied to.

`bluesky_repost` has no pre-flight at all and is unaffected.

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `BLUESKY_HANDLE` | Yes | Your Bluesky handle (e.g. `max-ai-dev.bsky.social`) |
| `BLUESKY_APP_PASSWORD` | Yes | App password — not your login password |

Bluesky posts are capped at 300 characters. URLs in the post body are auto-detected and converted to clickable facets — no manual markup needed.

Both AT URIs (`at://did:plc:.../app.bsky.feed.post/...`) and web URLs (`https://bsky.app/profile/.../post/...`) are accepted by all ops that take a post reference.

## Authoring notes

Preset JSON: `presets/bluesky.json`. Helper scripts: `presets/bluesky/` — one Python file per op (`publish.py`, `list.py`, `read.py`, `search.py`, `like.py`, `follow.py`, `repost.py`, `status_since.py`). The `{path}` placeholder in `cmd` resolves to `presets/bluesky/` at runtime.

All XRPC calls — `createSession`, `refreshSession` and every authenticated procedure — go through `presets/_http.py`, so the access and refresh JWTs are never carried across a redirect that leaves the PDS. A redirect off-origin stops the op with `refused off-origin redirect: ...` naming the destination, including in `refresh_session`, which otherwise returns `None` and silently falls back to a fresh login. See [contributing.md](../contributing.md#http-requests-go-through-presets_httppy).
