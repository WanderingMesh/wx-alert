"""Relevance filtering and airtime rate limiting."""

from __future__ import annotations

from datetime import timedelta

import pytest

from wx_alert.ratelimit import RateLimiter, RelevanceFilter


class TestRelevanceFilter:
    def test_default_posture_carries_warnings_only(self, fresh_warning, flood_watch):
        strict = RelevanceFilter("warning", "severe")
        assert strict.accepts(fresh_warning)[0]

        accepted, reason = strict.accepts(flood_watch)
        assert not accepted
        assert "below minimum" in reason

    def test_lowering_the_class_admits_watches(self, flood_watch):
        assert RelevanceFilter("watch", "severe").accepts(flood_watch)[0]

    def test_severity_is_also_required(self):
        # A Severe watch and a Minor watch are not the same thing.
        minor = {"event": "Flood Watch", "severity": "Minor"}
        assert not RelevanceFilter("watch", "severe").accepts(minor)[0]

    def test_class_is_checked_before_severity(self, flood_watch):
        # A Severe watch fails on class, and the reason should say so rather
        # than blaming severity, which it actually satisfies.
        accepted, reason = RelevanceFilter("warning", "severe").accepts(flood_watch)
        assert not accepted
        assert "product class" in reason

    def test_lowest_thresholds_accept_everything(self):
        permissive = RelevanceFilter("other", "unknown")
        assert permissive.accepts({"event": "Test Message"})[0]
        assert permissive.accepts({})[0]

    @pytest.mark.parametrize(
        ("klass", "severity"),
        [("bogus", "severe"), ("warning", "bogus")],
    )
    def test_rejects_invalid_thresholds(self, klass, severity):
        with pytest.raises(ValueError):
            RelevanceFilter(klass, severity)


class TestRateLimiter:
    def test_allows_the_first_transmission(self, now):
        assert RateLimiter(30, 12).check(now)[0]

    def test_enforces_minimum_spacing(self, now):
        limiter = RateLimiter(min_interval_seconds=30, max_per_hour=0)
        limiter.record(now)

        allowed, reason = limiter.check(now + timedelta(seconds=10))
        assert not allowed
        assert "minimum spacing" in reason

        assert limiter.check(now + timedelta(seconds=31))[0]

    def test_enforces_the_hourly_cap(self, now):
        limiter = RateLimiter(min_interval_seconds=0, max_per_hour=3)
        for index in range(3):
            limiter.record(now + timedelta(seconds=index))

        allowed, reason = limiter.check(now + timedelta(seconds=10))
        assert not allowed
        assert "hourly cap" in reason

    def test_the_cap_window_rolls_forward(self, now):
        limiter = RateLimiter(min_interval_seconds=0, max_per_hour=2)
        limiter.record(now)
        limiter.record(now + timedelta(minutes=1))

        assert not limiter.check(now + timedelta(minutes=2))[0]
        # Once the oldest transmission ages out, capacity returns.
        assert limiter.check(now + timedelta(minutes=61))[0]

    def test_zero_disables_each_limit_independently(self, now):
        unlimited = RateLimiter(min_interval_seconds=0, max_per_hour=0)
        for index in range(100):
            unlimited.record(now + timedelta(seconds=index))
        assert unlimited.check(now + timedelta(seconds=100))[0]

    def test_check_does_not_consume_capacity(self, now):
        limiter = RateLimiter(min_interval_seconds=0, max_per_hour=1)
        assert limiter.check(now)[0]
        assert limiter.check(now)[0]
        limiter.record(now)
        assert not limiter.check(now)[0]

    def test_reports_when_capacity_returns(self, now):
        limiter = RateLimiter(min_interval_seconds=60, max_per_hour=0)
        limiter.record(now)
        assert limiter.seconds_until_ready(now + timedelta(seconds=20)) == 40
        assert limiter.seconds_until_ready(now + timedelta(seconds=90)) == 0
