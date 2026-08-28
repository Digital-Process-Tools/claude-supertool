# slack

A deliberate post to a Slack channel or thread (`chat.postMessage`), for the case the [notifiers](../notifiers.md) example cannot cover.

## Requires

- `python3`
- `SLACK_BOT_TOKEN` env var (or `~/.config/slack/bot_token`, or `.slack-bot-token` in cwd) — a token from [api.slack.com/apps](https://api.slack.com/apps). Needs `chat:write` to publish, and `channels:history` (or `groups:history` for private channels) if the paired [`slack` watch source](watch.md) is also in use.

### Minting the token (#2041)

This lives at api.slack.com in a browser -- **not** the Slack desktop app's own Preferences, which has nothing related.

1. At [api.slack.com/apps](https://api.slack.com/apps), create the app **in the same workspace the target channel lives in** -- a token minted against the wrong workspace authenticates fine and then fails `chat.postMessage` with `channel_not_found`, which reads as a wrong channel ID rather than a wrong workspace.
2. *From a manifest* is the fast path -- it sets the scope for you, skipping the step below:

   ```yaml
   display_information:
     name: supertool
   oauth_config:
     scopes:
       bot:
         - chat:write
   ```

   Otherwise, build the app by hand and open **OAuth & Permissions**. `chat:write` exists under *both* **Bot Token Scopes** and **User Token Scopes** on that page -- add it to the wrong table and the token fails at the API, not at setup.
3. **Install to Workspace.** In a workspace that restricts app installation this raises an admin request instead of minting a token; a reader who does not know that reads the absence of a token as their own mistake and retries.
4. Copy the token OAuth & Permissions hands back (`xoxb-...` for a bot scope). A bot token also needs `/invite @your-app-name` in the target channel before `chat.postMessage` stops answering `not_in_channel`.

A **user token** (`xoxp-...`, User Token Scopes -- `scopes: user: [chat:write]` in the manifest above) works too and is not documented above the fold on purpose: nothing in `_auth.py` inspects the prefix, so it authenticates the same way, and it skips the `/invite` step because it posts as the human who authorized it. The tradeoff: with a user token, `author_is_viewer` (the field the paired watch source computes to tell the operator's own messages from a stranger's) resolves to the human rather than the bot, which weakens that signal. Prefer a bot token when the watch source is also in use.

## Ops

| Op | Syntax | What it returns |
|----|--------|-----------------|
| `slack_publish` | `slack_publish:CHANNEL_ID\|TEXT_OR_file://PATH[\|THREAD_TS[\|force]]` | Posted message `ts` and, when the lookup succeeds, a permalink |

`CHANNEL_ID`, not a channel name: a name is resolved server-side and can be re-pointed by someone else out from under a saved call, while an id cannot. Find it in the Slack UI under the channel's details.

`THREAD_TS` posts as a reply inside that thread's own `ts` — cheap to pass from the start, and the field the paired watch source's `slack_message` events already carry back to you (`payload.message_ts`, or `payload.thread_ts` when the message you are replying to was itself a thread reply) if you are replying to something the poller just delivered. Not named `payload.ts`: the channel bridge reserves that name for its own routing field, so the poller's own message-ts payload key was renamed away from it ([#2052](https://github.com/Digital-Process-Tools/claude-supertool/issues/2052)).

**Breaking change (#2039): the bare-path arm is withdrawn.** TEXT is read from a file only with the explicit `file://` prefix now -- a call that used to work as `slack_publish:C0123|.max/draft.md` (no prefix, relying on the path existing) now posts the literal string `.max/draft.md` instead of the file's contents, and needs `file://` added to keep reading the file. Unlike the other four publish/comment presets, which keep the bare-path convenience because their TEXT is always maintainer-typed, `slack_publish`'s TEXT can be stranger-authored -- the paired watch source hands back message text written by anyone in the workspace -- so a bare path that happened to exist inside the safety allowlist (`.max/` among them) let untrusted text choose a private file to disclose, with no marker in the call that a file was ever involved. If you have an existing call relying on the bare-path form, add `file://`.

## Not the same job as the notifier

`docs/notifiers.md` already documents a Slack webhook example ("Slack ping when files in `src/auth/` change") — a `curl` fired at `$SLACK_WEBHOOK`, matched on a glob, hooked into `edit`/`replace`/`paste`/`vim`. That is fire-and-forget: it never blocks the op, has no return path, and cannot hand back a `ts`. `slack_publish` is the other half — call it on purpose, block for the response, and get back what a later thread reply or edit needs. Use the notifier for "ping Slack whenever X happens on the op stream"; use `slack_publish` for "post this sentence now".

## Safety class: `!`

`slack_publish` is `"safety": "acts"` — marked `!` in `ops:roster` because it changes something outside this tree (a message becomes visible to a Slack workspace, which this tool cannot undo). It runs under `presets/_publish_safety.py`'s confirmation gate the same way `bluesky_publish` and `devto_publish` do: a bare call without `|force`, `SUPERTOOL_NO_PUBLISH_CONFIRM=1`, or `"no_publish_confirm": true` in `.supertool.json` refuses rather than posts.

## Example

```bash
./supertool 'slack_publish:C0123456|Deploy finished, all green.'
# (published channel=C0123456 ts=1700000000.000200)
# URL: https://yourteam.slack.com/archives/C0123456/p1700000000000200
```

Reply in the same thread once you have the `ts` (from the output above, or from a `slack_message` event's `payload.message_ts`):

```bash
./supertool 'slack_publish:C0123456|Following up on this thread.|1700000000.000200'
```

## See also

- [watch.md](watch.md#slack) — the paired `slack` watch source (inbound half).
- [../notifiers.md](../notifiers.md) — the fire-and-forget Slack webhook example.
