# devto

Dev.to publishing, discovery, and engagement via REST API. Replaces manual `curl` calls to publish articles, browse tags, read comments, and react — one supertool call handles auth and formatting. `devto_status_since` fans out across your recent articles and returns a unified briefing: new comments, reactions, top performers, all since your last check.

## Requires

- `python3`
- `DEVTO_API_KEY` env var — from [dev.to/settings/extensions](https://dev.to/settings/extensions)
- `DEVTO_SESSION_COOKIE` env var — required for `devto_react` and `devto_comment` (the public API has no write endpoints for these; cookie auth fills the gap)

## Ops

| Op | Syntax | What it returns |
|----|--------|-----------------|
| `devto_publish` | `devto_publish:TITLE\|MD_FILE\|CANONICAL[|TAGS\|COVER\|PUBLISHED]` | Published article URL + ID |
| `devto_list` | `devto_list[:N]` or `devto_list:USER[:N]` | Article list: title, ID, date, reactions, comments |
| `devto_read` | `devto_read:ID_OR_SLUG_OR_URL` | Full article body + title, tags, date, reaction + comment counts |
| `devto_browse` | `devto_browse:TAG[|N][|SORT]` | Tag feed: titles, authors, IDs, reaction counts |
| `devto_comments` | `devto_comments:ARTICLE_ID[:N]` | Flat comment tree with IDs, authors, bodies |
| `devto_react` | `devto_react:ARTICLE_ID_OR_SLUG_OR_URL[|CATEGORY]` | Toggles like/unicorn/readinglist reaction (requires session cookie) |
| `devto_comment` | `devto_comment:ARTICLE_ID_OR_SLUG_OR_URL\|MESSAGE_OR_FILE[|PARENT_COMMENT_ID[|force]]` | Posts a comment or reply; accepts inline text, file path, or `file://PATH` |
| `devto_status_since` | `devto_status_since[:ISO_TIMESTAMP]` | Briefing: new comments + top articles since timestamp (auto-tracks last check) |

## Common workflows

**Cross-publish a post with canonical URL:**
```bash
./supertool 'devto_publish:My Post Title|/tmp/post.md|https://max-ai-dev.hashnode.dev/my-post|ai,tooling|https://example.com/og.png|true'
```
`PUBLISHED=true` publishes immediately; omit or set `false` to save as draft.

**Monitor engagement since last check:**
```bash
./supertool 'devto_status_since'
```
Auto-tracks last call in `~/.config/devto/last_check`. Fans out 1 call per recent article to collect comments — budget ~1 second per article.

**Reply to the top comment on an article:**
```bash
./supertool 'devto_comments:1234567:5'
# note the comment ID, then:
./supertool 'devto_comment:1234567|Thanks for the kind words!|comment-id-here'
```

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `DEVTO_API_KEY` | Yes | Personal API key from dev.to/settings/extensions |
| `DEVTO_SESSION_COOKIE` | For write ops | Session cookie value — required for `devto_react` and `devto_comment` |

`devto_react` categories: `like` (default), `unicorn`, `readinglist`. Reactions toggle — calling again removes the reaction.

`devto_comment` is marked EXPERIMENTAL — Dev.to's public API has no comment-write endpoint. The op uses the session cookie to post via the internal endpoint, which may break on API changes.

## When the duplicate check cannot run

`devto_comment` fetches the article's existing comments first and refuses to post if you already commented there. That check has three outcomes, not two:

| Outcome | What the op does | stderr |
|---------|------------------|--------|
| No comment of yours on the article | Posts | — |
| You already commented | Refuses, lists the comment ids | `ABORT — already commented 2× on article 1234567 (ids: …). Use \|force as 4th field to override.` |
| **The check could not be made** | Refuses, does **not** post | `ABORT — pre-flight lookup failed for article 1234567 (cannot verify whether already commented). Use \|force as 4th field to bypass and post anyway.` |

The third row is the one to know about. "Could not be made" covers a `/comments` call that errored or timed out, a response that is not a comment list (a rate-limit body, for instance), and a `DEVTO_API_KEY` that cannot be turned into a username — anything that leaves the op with no answer rather than a negative one.

Until [#562](https://github.com/Digital-Process-Tools/claude-supertool/issues/562) that case posted the comment anyway, because a failed lookup returned the same value as a clean article. Declining instead is recoverable — re-run with `|force` as the 4th pipe-separated field:

```bash
./supertool 'devto_comment:1234567|Thanks for the kind words!||force'
```

`|force` skips the pre-flight entirely, so use it when you already know the article is clean. `hashnode_comment` has always behaved this way; the two ops now agree.

A missing sibling module (`_auth`, `_rest`) is deliberately **not** in the table: it raises `ImportError` naming the module rather than degrading, because a broken checkout has no useful degraded mode and the traceback is the whole diagnosis.

`devto_publish` has the same three outcomes since [#601](https://github.com/Digital-Process-Tools/claude-supertool/issues/601). It checks `/articles/me` for an article carrying the same `canonical_url`:

| Outcome | What the op does | stderr |
|---------|------------------|--------|
| No article with that canonical url | Publishes | — |
| One already exists | Refuses, names the slug and url | `ABORT — already published with canonical_url='https://…' (slug=…, url=…). Use \|force as 7th field to override.` |
| **The check could not be made** | Refuses, does **not** publish | `ABORT — pre-flight lookup failed for canonical_url='https://…' (cannot verify whether already published). Use \|force as 7th field to bypass and publish anyway.` |

"Could not be made" covers a `/articles/me` call that errored or timed out and a response that is not an article list — a rate-limit body is an unanswered question, not an empty account. Until #601 that case published a second copy of the article, because a failed lookup returned the same `False` as a clean account. The bypass is the 7th pipe-separated field:

```bash
./supertool 'devto_publish:Title|drafts/post.md|https://max.dp.tools/p/x|||true|force'
```

`hashnode_publish` has always behaved this way; the two ops now agree.

## Authoring notes

Preset JSON: `presets/devto.json`. Helper scripts: `presets/devto/` — one Python file per op (`publish.py`, `list.py`, `read.py`, `browse.py`, `comments.py`, `react.py`, `comment.py`, `status_since.py`). The `{path}` placeholder in `cmd` resolves to `presets/devto/` at runtime.

Both the API-key calls and the opt-in session-cookie calls go through `presets/_http.py`, so neither the `api-key` header nor the full logged-in `Cookie` is carried across a redirect that leaves `dev.to`. A redirect off-origin stops the op with `refused off-origin redirect: ...` naming the destination. See [contributing.md](../contributing.md#http-requests-go-through-presets_httppy).

Redirects that stay on `dev.to` are still followed, and are now announced. This matters for the session-cookie path: once the cookie expires, `dev.to` answers `/settings` with a same-origin 302 to `/enter`, and the op used to report `authenticity_token not found in /settings HTML — Dev.to layout may have changed`. The HTML it had was `/enter`, the layout was fine, and the cookie was dead. A `NOTE: the request was redirected ...` line now precedes that message, so the login bounce is visible instead of being read as a dev.to redesign.
