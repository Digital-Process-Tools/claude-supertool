# Security Policy

## Trust model

**Opening a project and running an op executes configuration from that
project.** `.supertool.json` is not an inert settings file — an
`ops.<name>.cmd` entry runs a shell command the moment that op is invoked,
and a `presets` entry loads and runs a preset's own scripts. Cloning a
repository and running supertool against it already runs that repository's
code, exactly as running `npm install` or `make` in an unfamiliar checkout
does. `.supertool.json` is not a new attack surface on top of that — it is
the same one, made explicit.

Two limits keep that trust contained to the project you actually opened,
rather than to anything sharing a filesystem with it (#695):

- the search for `.supertool.json` walks up from the current directory but
  stops at the first ancestor containing `.git` — it will not reach past
  your repository into a shared parent directory (a `/tmp` extraction, a
  CI runner's checkout root) that you did not choose to trust;
- a config file that is group- or world-writable, or not owned by the user
  running supertool (root excepted), is refused rather than loaded — the
  same rule `ssh` applies to `~/.ssh/config` and git applies to a
  repository via `safe.directory`, because a config another local account
  can rewrite is not really yours to trust.

Neither limit changes what a config you DO trust is allowed to do — a
project's own `.supertool.json`, owned by you, still runs whatever it
declares. See `_load_config()` and `_config_trust_violation()` in
`_supertool.py` for the mechanics.

## Supported Versions

Only the latest minor version receives security updates.

| Version | Supported          |
| ------- | ------------------ |
| 0.11.x  | :white_check_mark: |
| < 0.11  | :x:                |

## Reporting a Vulnerability

**Do not open public GitHub issues for security vulnerabilities.**

Email **<fdavid@digitalprocesstools.com>** with:

- A description of the issue
- Steps to reproduce
- Affected version (`./supertool version`)
- Impact assessment if known

You can expect an acknowledgment within 7 days. We will work with you to
understand and resolve the issue, and credit you in the release notes if
desired.
