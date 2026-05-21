"""Direct unit tests for _validator_render_diff covering metric-delta and
unchanged-with-primary branches."""
from __future__ import annotations

import supertool


def test_render_diff_unchanged_with_metric_delta() -> None:
    """Same count + ok, but a metric changed → emit metric delta row."""
    before = {"tool": "phpunit", "count": 0, "ok": True, "metrics": {"tests_total": 7}}
    after = {"tool": "phpunit", "count": 0, "ok": True, "metrics": {"tests_total": 10}}
    rows = supertool._validator_render_diff(before, after)
    joined = "".join(rows)
    assert "tests_total" in joined
    assert "7" in joined and "10" in joined


def test_render_diff_unchanged_falls_through_to_primary_metric() -> None:
    """No metric delta but absolute metric exists → 'unchanged' row with primary."""
    before = {"tool": "phpunit", "count": 0, "ok": True, "metrics": {"tests_total": 7}}
    after = {"tool": "phpunit", "count": 0, "ok": True, "metrics": {"tests_total": 7}}
    rows = supertool._validator_render_diff(before, after)
    joined = "".join(rows)
    assert "unchanged" in joined
    assert "tests_total" in joined


def test_render_diff_unchanged_no_metrics() -> None:
    """No metrics, same ok → simple unchanged row."""
    before = {"tool": "phpstan", "count": 0, "ok": True}
    after = {"tool": "phpstan", "count": 0, "ok": True}
    rows = supertool._validator_render_diff(before, after)
    joined = "".join(rows)
    assert "unchanged" in joined


def test_render_diff_metric_with_non_numeric_skipped() -> None:
    """Non-numeric metric values are ignored (not formatted)."""
    before = {"tool": "x", "count": 0, "ok": True, "metrics": {"label": "alpha", "count_n": 1}}
    after = {"tool": "x", "count": 0, "ok": True, "metrics": {"label": "beta", "count_n": 5}}
    rows = supertool._validator_render_diff(before, after)
    joined = "".join(rows)
    # Only numeric metric appears in the diff.
    assert "count_n" in joined and "5" in joined
    assert "label" not in joined


def test_render_diff_metric_bv_non_numeric_coerced_to_zero() -> None:
    """When before-value is non-numeric, it's treated as 0."""
    before = {"tool": "x", "count": 0, "ok": True, "metrics": {"k": "string"}}
    after = {"tool": "x", "count": 0, "ok": True, "metrics": {"k": 5}}
    rows = supertool._validator_render_diff(before, after)
    joined = "".join(rows)
    assert "k" in joined and "5" in joined
