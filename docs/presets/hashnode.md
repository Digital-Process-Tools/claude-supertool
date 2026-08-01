# hashnode

Hashnode publishing, discovery, and engagement via GraphQL. Replaces manual API calls to publish posts, monitor comments, react, and reply — all in one supertool call. Cross-publication support means you can comment or like posts on any Hashnode blog, not just your own. `hashnode_status_since` eliminates the polling loop: one call gives you follower count, new comments, and top posts since your last check.

## Requires

- `python3`
- `HASHNODE_TOKEN` env var — personal access token from [hashnode.com/settings/developer](https://hashnode.com/settings/developer)
- `HASHNODE_PUBLICATION_ID` env var — your publication's UUID (visible in publication settings)
- Optional: `HASHNODE_USERNAME` for `hashnode_list` to browse your own posts by username

## Ops

| Op | Syntax | What it returns |
|----|--------|-----------------|
| `hashnode_publish` | `hashnode_publish:TITLE\|MD_FILE\|CANONICAL[|TAGS\|COVER]` | Published post URL + slug |
| `hashnode_list` | `hashnode_list[:N]` or `hashnode_list:USER[:N]` | Post list: title, slug, date, reaction count |
| `hashnode_read` | `hashnode_read:SLUG_OR_URL` | Full post body + title, tags, date, reaction + comment counts |
| `hashnode_browse` | `hashnode_browse:TAG[|N][|SORT]` | Cross-publication tag feed: titles, authors, slugs, reaction counts |
| `hashnode_comments` | `hashnode_comments:SLUG_OR_URL[:N]` | Flat comment list with IDs, authors, bodies (for your publication) |
| `hashnode_comment` | `hashnode_comment:POST_ID_OR_URL\|MESSAGE_OR_FILE[|force]` | Posts a comment; accepts inline text, file path, or `file://PATH` |
| `hashnode_react` | `hashnode_react:POST_ID_OR_URL[|force]` | Likes a post; requires `\|force` (no per-user state available via API) |
| `hashnode_reply` | `hashnode_reply:COMMENT_ID\|MESSAGE_OR_FILE` | Replies to a comment; accepts inline text, file path, or `file://PATH` |
| `hashnode_search` | `hashnode_search:QUERY[:N]` | Text search within your publication: matching post titles and slugs |
| `hashnode_status_since` | `hashnode_status_since[:ISO_TIMESTAMP]` | Briefing: new comments, follower count, top posts since timestamp (auto-tracks last check) |

## Common workflows

**Cross-publish a blog post from Dev.to or a local markdown file:**
```bash
./supertool 'hashnode_publish:My Post Title|/tmp/post.md|https://dev.to/me/my-post|ai,tooling|https://example.com/og.png'
```
The canonical URL ensures search engines attribute the original correctly.

**Monitor engagement since last check:**
```bash
./supertool 'hashnode_status_since'
```
No timestamp needed — the script auto-tracks the last call in `~/.config/hashnode/last_check`. Returns new comments + follower count + top posts in one response.

**Read a post then reply to its top comment:**
```bash
./supertool 'hashnode_comments:my-post-slug:5'
# get comment ID from output, then:
./supertool 'hashnode_reply:comm-abc123|Great point — the tradeoff you describe is exactly what motivated the design.'
```

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `HASHNODE_TOKEN` | Yes | Personal access token from hashnode.com/settings/developer |
| `HASHNODE_PUBLICATION_ID` | Yes | Publication UUID from your Hashnode publication settings |
| `HASHNODE_USERNAME` | No | Your Hashnode username (used by `hashnode_list` to default to your own posts) |

`hashnode_react` requires `|force` on every call — Hashnode's GraphQL API does not expose per-user reaction state, so the preset can't detect duplicates. To skip the flag globally, add to `.supertool.json`:

```json
{
  "ops": {
    "hashnode_react": { "auto_force": true }
  }
}
```

## When the duplicate check cannot run

`hashnode_publish`, `hashnode_comment` and `hashnode_react` each look before they write. The check has three outcomes, not two:

| Outcome | What the op does | stderr |
|---------|------------------|--------|
| Nothing found | Writes | — |
| Duplicate found | Refuses, names what it found | `ABORT — already published with canonical_url='https://…' (slug=…, url=…). Use |force as 6th field to override.` |
| **The check could not be made** | Refuses, writes nothing | `ABORT — pre-flight lookup failed for canonical_url='https://…' (cannot verify whether already published). Use |force as 6th field to bypass and publish anyway.` |

Hashnode has behaved this way since these ops were written — it is the preset [#599](https://github.com/Digital-Process-Tools/claude-supertool/issues/599) and [#601](https://github.com/Digital-Process-Tools/claude-supertool/issues/601) copied the fail-closed shape *from*. Their module docstrings claimed the opposite (`a warning is printed and publish proceeds`) for the whole life of the files; [#603](https://github.com/Digital-Process-Tools/claude-supertool/issues/603) corrected the prose, not the behaviour. Every refusal is recoverable with `|force`:

```bash
./supertool 'hashnode_publish:Title|drafts/post.md|https://max.dp.tools/p/x||||force'
./supertool 'hashnode_comment:POST_ID|Thanks!|force'
./supertool 'hashnode_react:POST_ID|force'
```

`hashnode_react` lands on the third row on *every* call — Hashnode's GraphQL exposes no per-user reaction field, so `preflight_react` makes no API call and always returns "unknown". See the `auto_force` opt-in above.

## Authoring notes

Preset JSON: `presets/hashnode.json`. Helper scripts: `presets/hashnode/` — one Python file per op (`publish.py`, `list.py`, `read.py`, `browse.py`, `comments.py`, `comment.py`, `react.py`, `reply.py`, `search.py`, `status_since.py`). The `{path}` placeholder in `cmd` resolves to `presets/hashnode/` at runtime.

All GraphQL calls go through `presets/_http.py`, so the `Authorization` header is never carried across a redirect that leaves `gql.hashnode.com`. If the endpoint ever answers a redirect pointing elsewhere, the op stops with `refused off-origin redirect: ...` naming the destination — including in `gql_safe`, which otherwise degrades to `None`. See [contributing.md](../contributing.md#http-requests-go-through-presets_httppy).
