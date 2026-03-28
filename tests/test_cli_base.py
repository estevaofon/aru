"""Tests for cli base functions like format_duration."""

import pytest
from aru.cli import format_duration


class TestFormatDuration:
    """Test cases for format_duration function."""

    # Millisecond cases (< 1 second)
    def test_milliseconds_zero(self):
        assert format_duration(0) == "0ms"

    def test_milliseconds_half_second(self):
        assert format_duration(0.5) == "500ms"

    def test_milliseconds_fractional(self):
        assert format_duration(0.25) == "250ms"

    def test_milliseconds_smallest(self):
        assert format_duration(0.001) == "1ms"

    def test_milliseconds_nine_tenths(self):
        assert format_duration(0.9) == "900ms"

    # Second-only cases
    def test_seconds_single(self):
        assert format_duration(1) == "1s"

    def test_seconds_single_float(self):
        assert format_duration(1.0) == "1s"

    def test_seconds_multiple(self):
        assert format_duration(59) == "59s"

    # Minute + second cases
    def test_minutes_and_seconds(self):
        assert format_duration(90) == "1m 30s"

    def test_minutes_only(self):
        assert format_duration(60) == "1m 0s"

    def test_minutes_no_seconds(self):
        assert format_duration(120) == "2m 0s"

    def test_minutes_with_partial_seconds(self):
        assert format_duration(90.5) == "1m 30s"

    def test_many_minutes(self):
        assert format_duration(3599) == "59m 59s"

    # Hour + minute + second cases
    def test_hours_minutes_seconds(self):
        assert format_duration(3661) == "1h 1m 1s"

    def test_hours_only(self):
        assert format_duration(3600) == "1h 0m 0s"

    def test_hours_with_minutes(self):
        assert format_duration(3660) == "1h 1m 0s"

    def test_hours_with_seconds(self):
        assert format_duration(3601) == "1h 0m 1s"

    def test_hours_no_minutes_or_seconds(self):
        assert format_duration(7200) == "2h 0m 0s"

    def test_many_hours(self):
        assert format_duration(86399) == "23h 59m 59s"

    # Edge cases
    def test_exactly_one_second(self):
        assert format_duration(1.0) == "1s"

    def test_just_under_one_second(self):
        assert format_duration(0.999) == "999ms"

    def test_just_over_one_second(self):
        assert format_duration(1.001) == "1s"

    def test_whole_day(self):
        assert format_duration(86400) == "24h 0m 0s"

    def test_fractional_hours(self):
        # 5400 seconds = 1.5 hours
        assert format_duration(5400) == "1h 30m 0s"

    def test_with_microseconds_truncated(self):
        # Only integer part is used for display
        assert format_duration(1.9) == "1s"

    def test_negative_value(self):
        # Negative values are returned as-is (e.g., -1.0 -> "-1000ms")
        assert format_duration(-1) == "-1000ms"