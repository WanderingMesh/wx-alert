"""Message rendering under a hard byte budget.

The invariant that matters most is simple and absolute: whatever is produced
must fit the budget and must be valid UTF-8. Everything else is a question of
spending the remaining bytes well.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from wx_alert.mesh_format import (
    MESH_MAX_TEXT_LEN,
    build_mesh_message,
    extract_detail,
    format_area,
    format_expiry,
    mesh_text_budget,
    normalize_ascii,
    truncate_bytes,
)


class TestBudget:
    def test_shrinks_as_the_node_name_grows(self):
        assert mesh_text_budget("WX") > mesh_text_budget("WX-Reno")
        assert mesh_text_budget("WX-Reno") > mesh_text_budget("X" * 32)

    def test_always_leaves_headroom_below_the_firmware_limit(self):
        # The firmware prepends the node name and appends framing, so the
        # usable text must be strictly less than MAX_TEXT_LEN.
        for name in ("", "A", "WX-Reno", "X" * 32):
            assert 0 < mesh_text_budget(name) < MESH_MAX_TEXT_LEN

    def test_never_negative_for_an_oversized_name(self):
        assert mesh_text_budget("X" * 500) == 0


class TestNormalizeAscii:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("90\u00b0F", "90degF"),
            ("a \u2013 b", "a - b"),
            ("\u201cquoted\u201d", '"quoted"'),
            ("wait\u2026", "wait..."),
            ("caf\u00e9", "cafe"),
            ("line\nwrapped\ntext", "line wrapped text"),
            ("  padded  ", "padded"),
        ],
    )
    def test_folds_to_ascii(self, raw, expected):
        assert normalize_ascii(raw) == expected

    def test_output_is_pure_ascii(self):
        result = normalize_ascii("\u00e9\u00e8\u00ea \u2014 90\u00b0 \u201cx\u201d")
        assert result.encode("ascii")  # raises if any byte is non-ASCII


class TestTruncateBytes:
    def test_leaves_short_text_untouched(self):
        assert truncate_bytes("short", 100) == "short"

    @pytest.mark.parametrize("budget", range(1, 40))
    def test_never_exceeds_budget_or_splits_a_character(self, budget):
        # Slicing encoded bytes can land inside a multi-byte sequence. The
        # result must always decode cleanly.
        text = "ABC\u00e9\u00e9\u00e9DEF ghij klmno"
        result = truncate_bytes(text, budget)
        encoded = result.encode("utf-8")
        assert len(encoded) <= budget
        encoded.decode("utf-8")

    def test_prefers_a_word_boundary(self):
        result = truncate_bytes("alpha beta gamma delta", 20)
        assert result.endswith("...")
        assert "beta" in result

    def test_does_not_collapse_a_single_long_token(self):
        # Honouring a word boundary here would throw away nearly everything.
        result = truncate_bytes("a supercalifragilistic", 15)
        assert len(result) > 5

    def test_zero_budget_yields_nothing(self):
        assert truncate_bytes("anything", 0) == ""


class TestSegments:
    def test_expiry_omits_the_weekday_when_it_ends_today(self):
        alert = {"ends": "2026-08-12T21:00:00-07:00"}
        now = datetime(2026, 8, 12, 11, 0, tzinfo=timezone(timedelta(hours=-7)))
        assert format_expiry(alert, now) == "til 21:00"

    def test_expiry_includes_the_weekday_on_a_later_day(self):
        alert = {"ends": "2026-08-13T21:00:00-07:00"}
        now = datetime(2026, 8, 12, 11, 0, tzinfo=timezone(timedelta(hours=-7)))
        assert format_expiry(alert, now) == "til Thu 21:00"

    def test_expiry_uses_the_alert_areas_local_time(self):
        # NWS supplies the offset of the affected area, which is exactly what
        # a reader needs. Converting to UTC would show 04:00 instead of 21:00.
        alert = {"ends": "2026-08-13T21:00:00-07:00"}
        now = datetime(2026, 8, 12, 18, 0, tzinfo=timezone.utc)
        assert "21:00" in format_expiry(alert, now)

    def test_expiry_falls_back_to_expires(self):
        assert format_expiry({"expires": "2026-08-13T11:15:00-07:00"})

    def test_expiry_is_empty_when_unknown(self):
        assert format_expiry({}) == ""

    def test_area_collapses_a_long_zone_list(self):
        alert = {"areaDesc": "Washoe County; Storey County; Lyon County"}
        assert format_area(alert) == "Washoe County +2"

    def test_area_keeps_a_single_zone_intact(self, flood_watch):
        assert format_area(flood_watch) == "Greater Reno-Carson City-Minden Area"

    def test_detail_prefers_instruction(self, fresh_warning):
        assert extract_detail(fresh_warning) == "Move to higher ground now."

    def test_detail_falls_back_to_the_what_section(self, flood_watch):
        # The captured alert has an empty instruction, which is common.
        assert not flood_watch.get("instruction")
        detail = extract_detail(flood_watch)
        assert detail.startswith("Flash flooding caused by excessive rainfall")
        # The WHAT section only, not the WHERE and WHEN that follow it.
        assert "WHERE" not in detail

    def test_detail_never_uses_the_headline(self, flood_watch):
        # The headline restates the event name and times already present in
        # the message, so spending 87 bytes on it would be waste.
        stripped = {
            k: v for k, v in flood_watch.items()
            if k not in ("instruction", "description")
        }
        assert extract_detail(stripped) == ""


class TestBuildMeshMessage:
    @pytest.mark.parametrize("budget", [0, 5, 12, 20, 40, 80, 116, 141, 200])
    def test_always_fits_the_budget(self, budget, flood_watch, now):
        message = build_mesh_message(flood_watch, budget, now)
        assert len(message.encode("utf-8")) <= budget

    @pytest.mark.parametrize("budget", [10, 30, 60, 90, 141])
    def test_output_is_always_ascii(self, budget, flood_watch, now):
        build_mesh_message(flood_watch, budget, now).encode("ascii")

    def test_includes_every_segment_when_there_is_room(self, fresh_warning, now):
        message = build_mesh_message(fresh_warning, 141, now)
        assert "Flash Flood Warning" in message
        assert "Washoe County" in message
        assert "til" in message
        assert "higher ground" in message

    def test_drops_area_before_expiry_when_space_runs_out(self, flood_watch, now):
        # The monitored point is fixed by configuration so the area is largely
        # implied, but "until when" never is.
        message = build_mesh_message(flood_watch, 40, now)
        assert "til" in message
        assert "Minden" not in message

    def test_keeps_the_event_name_above_all_else(self, flood_watch, now):
        assert build_mesh_message(flood_watch, 20, now) == "Flood Watch"

    def test_truncates_an_event_name_that_cannot_fit(self, now):
        alert = {"event": "Extremely Long Product Name That Cannot Possibly Fit"}
        message = build_mesh_message(alert, 15, now)
        assert len(message.encode("utf-8")) <= 15
        assert message.endswith("...")

    def test_omits_detail_rather_than_leaving_a_stub(self, flood_watch, now):
        # Below the minimum, a few trailing characters of prose are noise.
        message = build_mesh_message(flood_watch, 75, now)
        assert not message.rstrip().endswith(". ...")

    def test_handles_an_entirely_empty_alert(self, now):
        assert build_mesh_message({}, 100, now)
