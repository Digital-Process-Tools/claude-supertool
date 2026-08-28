# slack

A deliberate post to a Slack channel or thread (`chat.postMessage`), for the case the [notifiers](../notifiers.md) example cannot cover.

## Requires

- `python3`
- `SLACK_BOT_TOKEN` env var (or `~/.config/slack/bot_token`, or `.slack-bot-token` in cwd) — a bot token from [api.slack.com/apps](https://api.slack.com/apps) (OAuth & Permissions -> Bot User OAuth Token, `xoxb-...`). Needs `chat:write` to publish, and `channels:history` (or `groups:history` for private channels) if the paired [`slack` watch source](watch.md) is also in use.

## Ops

| Op | Syntax | What it returns |
|----|--------|-----------------|
| `slack_publish` | `slack_publish:CHANNEL_ID\|TEXT_OR_FILE_OR_file://PATH[\|THREAD_TS[\|force]]` | Posted message `ts` and, when the lookup succeeds, a permalink |

`CHANNEL_ID`, not a channel name: a name is resolved server-side and can be re-pointed by someone else out from under a saved call, while an id cannot. Find it in the Slack UI under the channel's details.

`THREAD_TS` posts as a reply inside that thread's own `ts` — cheap to pass from the start, and the field the paired watch source's `slack_message` events already carry back to you (`payload.ts`) if you are replying to something the poller just delivered.

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

Reply in the same thread once you have the `ts` (from the output above, or from a `slack_message` event's `payload.ts`):

```bash
./supertool 'slack_publish:C0123456|Following up on this thread.|1700000000.000200'
```

## See also

- [watch.md](watch.md#slack) — the paired `slack` watch source (inbound half).
- [../notifiers.md](../notifiers.md) — the fire-and-forget Slack webhook example.
