---
title: "presets/gitlab/ — host check is depth-1 by design"
match: "presets/gitlab/"
---

# Host guard is depth-1, on purpose

`path_refusal` calls `decoded_host_naming_reason(urllib.parse.unquote(path))` — **one** `unquote`, not to a fixed point.

Two readers, two depths:
- depth 0 = glab's own endpoint parse → `host_naming_reason`
- depth 1 = a consumer that decodes once → `decoded_host_naming_reason`

**Do not "fix" this to decode-to-fixed-point.** That would refuse `%252F%252Fx` — the only valid encoding for a file literally named `%2F%2Fx`.

Call site: `presets/gitlab/api.py` in `path_refusal`, the `host_naming_reason(path) or decoded_host_naming_reason(unquote(path))` pair. Grep the symbol, not a line number — this file moves.

Corpora that must all stay green under any change here:
- `tests/test_gl_api_url_refusal_1035.py`
- `tests/test_gl_api_encoded_backslash_1043.py`
- `tests/test_gl_api_decode_depth_1194.py` — arrives with PR #1211; it is the only one that fails under a fixed-point rewrite. The other two pass under it, which is why prose alone was not enough.

# Why the guard exists at all

`gl-api` forwards its endpoint straight to `glab api`, which attaches the live `Private-Token`. No guard = arbitrary host gets the real token. That's the threat the depth-1 check defends against.

# glab is unauthenticated here

In agent sandboxes `glab` has no token — `gh` does (authenticated + networked). Expect GitLab-path work to come back "could not verify end to end." That's expected, not a bug:
- Say what you'd run to verify it for real (the `glab` command), don't fake a result.
- Use `gh` equivalents only when the target is actually GitHub.
