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

## Authoring notes

Preset JSON: `presets/devto.json`. Helper scripts: `presets/devto/` — one Python file per op (`publish.py`, `list.py`, `read.py`, `browse.py`, `comments.py`, `react.py`, `comment.py`, `status_since.py`). The `{path}` placeholder in `cmd` resolves to `presets/devto/` at runtime.
