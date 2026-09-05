"""#2291 -- `_expand_env` spliced a second, nested single-quote pair into a
`$VAR` reference that already sat inside the template author's OWN single
quotes (the `docs/notifiers.md` `bash -c '...$SLACK_WEBHOOK'` pattern), once
#2290 wrapped every substituted value in `shlex.quote()`. `shlex.split` has
no notion of nested quoting, so a value with a shell-special character (a
space, here) came back split into two argv tokens instead of one.

Fix: `_expand_env` now tracks whether the match position sits inside a
single-quote span the TEMPLATE itself opened (`_in_template_single_quotes`).
Inside such a span nothing but a literal `'` is special, so the value is
spliced in directly with only its own `'` characters broken out
(`'"'"'`) -- no extra wrapping quote pair. Outside any such span, the
existing `shlex.quote()` behaviour (#2290, the Windows-backslash fix) is
unchanged.

Every "must not add a nested quote" case here is paired with a "must still
protect an unquoted reference" case in the same fixture -- the #2290 fix
this must not regress.

Would this test fail if the code did nothing? Yes -- at 05de660a,
`test_a_value_inside_the_templates_own_quotes_stays_one_token` fails: the
space in the value splits it into two argv tokens.
"""
from __future__ import annotations

import shlex

import supertool


def test_a_value_inside_the_templates_own_quotes_stays_one_token():
    template = "bash -c '/usr/bin/curl -d $SLACK_WEBHOOK'"
    env = {"SLACK_WEBHOOK": "https://hooks.example/a b"}

    expanded = supertool._expand_env(template, env)
    tokens = shlex.split(expanded)

    assert tokens == ["bash", "-c", "/usr/bin/curl -d https://hooks.example/a b"]


def test_an_embedded_single_quote_inside_the_templates_quotes_is_broken_out():
    template = "bash -c '/usr/bin/curl -d $SLACK_WEBHOOK'"
    env = {"SLACK_WEBHOOK": "it's-a-value"}

    expanded = supertool._expand_env(template, env)
    tokens = shlex.split(expanded)

    assert tokens == ["bash", "-c", "/usr/bin/curl -d it's-a-value"]


def test_a_plain_value_inside_the_templates_quotes_still_round_trips():
    """Must-fire's companion in miniature: nothing regresses for the common
    shape either, a value with no shell-special characters at all."""
    template = "bash -c '/usr/bin/curl -d $SLACK_WEBHOOK'"
    env = {"SLACK_WEBHOOK": "https://hooks.example/plain"}

    expanded = supertool._expand_env(template, env)
    tokens = shlex.split(expanded)

    assert tokens == ["bash", "-c", "/usr/bin/curl -d https://hooks.example/plain"]


def test_an_unquoted_reference_is_still_shlex_quoted_2290():
    """The #2290 fix this must not regress: a `$VAR` reference OUTSIDE any
    of the template's own quotes still gets wrapped so a value with a
    shell-special character survives as one argv token, and a Windows path's
    backslashes are not eaten by `shlex.split`."""
    template = "echo $WATCH_PATH"
    env = {"WATCH_PATH": r"C:\Users\runneradmin\AppData\Local\Temp\watch-sources"}

    expanded = supertool._expand_env(template, env)
    tokens = shlex.split(expanded)

    assert tokens == ["echo", r"C:\Users\runneradmin\AppData\Local\Temp\watch-sources"]


def test_a_reference_inside_double_quotes_is_unchanged_pre_existing_behaviour():
    """Double-quote context is explicitly out of scope for this fix (#2291's
    own text: the shipped, documented pattern is single-quoted). This pins
    today's (imperfect, pre-existing) behaviour so a future change to it is
    a deliberate decision, not an accidental side effect of this fix."""
    template = 'bash -c "echo $VAR"'
    env = {"VAR": "plain"}

    expanded = supertool._expand_env(template, env)

    assert expanded == 'bash -c "echo plain"'
