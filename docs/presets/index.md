# Presets

A preset is a declarative bundle of ops (and optionally aliases and validators) for a specific tool or platform. Enable one by adding its name to `"presets"` in your `.supertool.json`:

```json
{ "presets": ["gitlab", "git"] }
```

Supertool merges preset ops at startup — project-level ops always override on name conflict. Presets are resolved from three locations in order: `./presets/{name}.json` (project), `~/.config/supertool/presets/{name}.json` (user), then the supertool install directory (shipped presets).

## Shipped presets

| Preset | Description | Page | Requires |
|--------|-------------|------|----------|
| `git` | Git investigation + workflow ops | [git.md](git.md) | `git` |
| `github` | GitHub issues, PRs, Actions, social | [github.md](github.md) | `gh` CLI |
| `gitlab` | GitLab issues, MRs, pipelines | [gitlab.md](gitlab.md) | `glab` CLI |
| `claude-log` | Inspect Claude Code session transcripts | [claude-log.md](claude-log.md) | none |
| `hashnode` | Publish, read, and engage on Hashnode | [hashnode.md](hashnode.md) | `python3`, `HASHNODE_TOKEN` |
| `devto` | Publish, read, and engage on Dev.to | [devto.md](devto.md) | `python3`, `DEVTO_API_KEY` |
| `bluesky` | Post, read, and engage on Bluesky | [bluesky.md](bluesky.md) | `python3`, `BLUESKY_HANDLE` + `BLUESKY_APP_PASSWORD` |
| `xml` | Read-only XPath queries over XML files | [xml.md](xml.md) | `python3` |
| `watch` | Background pollers + async wake on external events (PRs, MRs, pipelines) | [watch.md](watch.md) | `gh` and/or `glab` per source |

## Writing your own preset

**File layout:** create `presets/NAME.json` in your project (or `~/.config/supertool/presets/NAME.json` for personal use). Place helper scripts in a sibling folder `presets/NAME/`. The `{path}` placeholder in `cmd` resolves to the preset JSON's directory with a trailing `/`, so scripts stay co-located:

```json
{
  "description": "My team's deploy tools",
  "requires": "kubectl",
  "ops": {
    "deploy-status": {
      "cmd": "python3 {path}mytools/status.py {arg}",
      "timeout": 15,
      "description": "Check deployment status for a service.",
      "syntax": "deploy-status:SERVICE"
    }
  }
}
```

The `requires` field is documentation only — supertool does not enforce it at runtime. See [contributing.md](../contributing.md) for full authoring guidelines.

## Preset vs. custom op

Use a **preset** when:
- The ops are reusable across multiple projects
- They depend on a specific external CLI or service
- You want to share them with the team or publish them

Use a **custom op** (directly in `.supertool.json`) when:
- The op is specific to this project (e.g., `pytest:tests/`, `mypy:src/`)
- It's a one-liner that doesn't need a helper script
- You don't need the `{path}` placeholder

Both support the same op schema — presets are just a packaging convention.
