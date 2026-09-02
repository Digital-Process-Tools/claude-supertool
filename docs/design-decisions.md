# Design decisions

Split out of `README.md`'s "Design decisions" section by [#2142](https://github.com/Digital-Process-Tools/claude-supertool/issues/2142). The two-files-one-shim bullet stayed on the front page because it is load-bearing there (`supertool.py`'s real line count, pinned by `tests/test_readme_one_file_claim_2143.py`); the rest of the list lives here.

- **Python 3.9+.** macOS ships 3.9 via CommandLineTools; we don't force upgrades.
- **Supertool isn't an MCP server.** For the ops supertool ships (run a script, return output), MCP would be ceremony — Bash-invoked binaries are simpler, faster, and plug into Claude Code's existing `--allowedTools`/`--disallowedTools` flow. But supertool *consumes* MCP servers when you want LSP-grade accuracy on `resolve`/`refs`/`workspace` — see [docs/mcp-integration.md](mcp-integration.md).
- **Trade Python work for LLM tokens.** LLM compute is expensive; local CPU is cheap. Any time the model would spend tokens computing, parsing, formatting, or finding — supertool should spend milliseconds instead. Richer op output (state hints, guards, semantic anchors, auto-formatting, syntax checks) is not feature creep — it's the whole thesis. Heavy Python is fine if it shaves tokens off the model side.
