"""Message rendering under a hard byte budget.

The invariant that matters most is simple and absolute: whatever is produced
must fit the budget and must be valid UTF-8. Everything else is a question of
spending the remaining bytes well, which these tests pin down against the two
real captured products and a handful of synthetic ones.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from wx_alert.mesh_format import (
    MESH_MAX_TEXT_LEN,
    alert_segments,
    build_mesh_message,
    format_area,
    format_event,
    format_window,
    mesh_text_budget,
    normalize_ascii,
    truncate_bytes,
)

PACIFIC = timezone(timedelta(hours=-7))


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

    def test_prefers_a_sentence_boundary(self):
        # "...on the lowest floor of a sturdy..." is what the old word-level
        # cut produced. A cut at the sentence keeps a complete thought.
        text = "Move to higher ground. Avoid flooded roads. Do not drive."
        assert (
            truncate_bytes(text, 50) == "Move to higher ground. Avoid flooded roads..."
        )

    def test_falls_back_to_a_clause_then_a_word(self):
        assert (
            truncate_bytes("alpha, beta gamma, delta epsilon", 24)
            == "alpha, beta gamma..."
        )
        assert truncate_bytes("alpha beta gamma delta", 20) == "alpha beta gamma..."

    def test_does_not_collapse_a_single_long_token(self):
        # Honouring a boundary here would throw away nearly everything.
        result = truncate_bytes("a supercalifragilistic", 15)
        assert len(result) > 5

    def test_zero_budget_yields_nothing(self):
        assert truncate_bytes("anything", 0) == ""


class TestHeader:
    @pytest.mark.parametrize(
        ("event", "expected"),
        [
            ("Winter Storm Warning", "Winter Strm Wrn"),
            ("Severe Thunderstorm Warning", "Svr Tstorm Wrn"),
            ("Special Weather Statement", "Spcl Wx Stmt"),
            ("Excessive Heat Warning", "Excsv Heat Wrn"),
            ("Blowing Dust Advisory", "Blwng Dust Adv"),
            # Hazard words that carry the meaning are never shortened.
            ("Flood Watch", "Flood Wtch"),
            ("Fire Weather Watch", "Fire Wx Wtch"),
            ("Tornado Warning", "Tornado Wrn"),
        ],
    )
    def test_event_is_abbreviated_from_the_product_vocabulary(self, event, expected):
        assert format_event({"event": event}) == expected

    def test_area_abbreviates_county_and_drops_the_state(self):
        assert format_area({"areaDesc": "Washoe County, NV"}) == "Washoe Co"

    def test_area_joins_a_short_zone_list(self, tornado_warning):
        # A three-county storm warning names all three: "Lyon +2" said which
        # county happened to sort first, not where the storm was.
        assert format_area(tornado_warning) == "Lyon/Storey/Washoe"

    def test_area_collapses_a_long_zone_list(self):
        alert = {
            "areaDesc": "Santa Clarita Valley; "
            + "; ".join(f"Zone {i}" for i in range(11))
        }
        assert format_area(alert) == "Santa Clarita Valley +11"

    def test_area_drops_duplicate_zones(self):
        alert = {"areaDesc": "Greater Lake Tahoe Area; Greater Lake Tahoe Area"}
        assert format_area(alert) == "Greater Lake Tahoe Area"

    def test_area_keeps_a_single_zone_intact(self, flood_watch):
        assert format_area(flood_watch) == "Greater Reno-Carson City-Minden Area"


class TestWindow:
    def test_in_force_shows_end_only(self):
        alert = {
            "onset": "2026-08-12T08:00:00-07:00",
            "ends": "2026-08-12T21:00:00-07:00",
        }
        now = datetime(2026, 8, 12, 11, 0, tzinfo=PACIFIC)
        assert format_window(alert, now) == "til 21:00"

    def test_end_on_a_later_day_carries_its_date(self):
        alert = {
            "onset": "2026-08-12T08:00:00-07:00",
            "ends": "2026-08-13T21:00:00-07:00",
        }
        now = datetime(2026, 8, 12, 11, 0, tzinfo=PACIFIC)
        assert format_window(alert, now) == "til 21:00 08/13"

    def test_future_onset_shows_start_and_end(self, flood_watch):
        # Issued at 08:19 for 14:00 through the next evening. "til Thu 21:00"
        # hid that nothing was happening for six hours.
        now = datetime(2026, 8, 12, 8, 19, tzinfo=PACIFIC)
        assert format_window(flood_watch, now) == "14:00-21:00 08/13"

    def test_a_window_entirely_on_another_day_dates_the_start(self):
        alert = {
            "onset": "2026-09-01T11:00:00-07:00",
            "ends": "2026-09-01T23:00:00-07:00",
        }
        now = datetime(2026, 8, 30, 9, 19, tzinfo=PACIFIC)
        assert format_window(alert, now) == "09/01 11:00-23:00"

    def test_a_window_spanning_two_later_days_dates_both(self):
        alert = {
            "onset": "2026-09-02T12:00:00-07:00",
            "ends": "2026-09-03T21:00:00-07:00",
        }
        now = datetime(2026, 8, 31, 9, 0, tzinfo=PACIFIC)
        assert format_window(alert, now) == "09/02 12:00-09/03 21:00"

    def test_an_onset_minutes_away_counts_as_now(self):
        alert = {
            "onset": "2026-08-12T11:10:00-07:00",
            "ends": "2026-08-12T21:00:00-07:00",
        }
        now = datetime(2026, 8, 12, 11, 0, tzinfo=PACIFIC)
        assert format_window(alert, now) == "til 21:00"

    def test_uses_the_alert_areas_local_time(self):
        # NWS supplies the offset of the affected area, which is exactly what
        # a reader needs. Converting to UTC would show 04:00 instead of 21:00.
        alert = {"ends": "2026-08-12T21:00:00-07:00"}
        now = datetime(2026, 8, 12, 18, 0, tzinfo=UTC)
        assert format_window(alert, now) == "til 21:00"

    def test_falls_back_to_expires(self):
        assert format_window({"expires": "2026-08-13T11:15:00-07:00"})

    def test_is_empty_when_unknown(self):
        assert format_window({}) == ""


class TestSegments:
    def test_storm_warning_yields_location_motion_and_next_town(self, tornado_warning):
        segments = alert_segments(tornado_warning)
        # The imperative leads, then the storm, then where it is heading.
        assert segments[0] == "TAKE COVER NOW!"
        assert segments[1] == "Tornado 7 mi E of Lockwood, moving E at 15 mph."
        assert segments[2] == "Nr Derby Dam, 13:30."
        # The alternate reference point ("or 13 miles northeast of Virginia
        # City") is dropped: one fix is enough on a radio.
        assert not any("Virginia City" in s for s in segments)

    def test_hazard_line_names_the_threat_when_the_storm_description_is_long(self):
        alert = {
            "description": (
                "* At 1126 AM PDT /1126 AM MST/, a severe thunderstorm was located 12 "
                "miles south of Littlefield, or 10 miles southeast of Mesquite, moving "
                "northeast at 35 mph. HAZARD...60 mph wind gusts. SOURCE...Radar "
                "indicated. IMPACT...Expect damage to roofs, siding, and trees. "
                "Locations impacted include... Mesquite, Littlefield, Beaver Dam, "
                "Virgin River Gorge, Virgin River Campground and Bunkerville."
            ),
        }
        segments = alert_segments(alert)
        assert segments[0] == "Svr tstorm 12 mi S of Littlefield, moving NE at 35 mph."
        assert segments[1] == "60 mph gusts."
        assert (
            segments[2] == "Incl Mesquite, Littlefield, Beaver Dam, Virgin River Gorge."
        )

    def test_impact_is_dropped_when_it_repeats_hazard(self):
        alert = {
            "description": (
                "HAZARD...Life threatening flash flooding. Thunderstorms producing "
                "flash flooding. SOURCE...Radar indicated. IMPACT...Life threatening "
                "flash flooding of creeks and streams, urban areas, highways."
            ),
        }
        segments = alert_segments(alert)
        assert segments == ["Life threatening flash flooding."]

    def test_zone_product_yields_what_then_impacts(self, flood_watch):
        segments = alert_segments(flood_watch)
        assert segments[0] == "Flash flooding still possible."
        assert segments[1].startswith(
            "Excessive runoff may cause rock, mud, and debris flows"
        )
        # WHERE and WHEN are already in the header.
        assert not any("western Nevada" in s for s in segments)

    def test_fire_weather_bullets_keep_their_subject(self):
        alert = {
            "description": (
                "* Affected Area...Fire Weather Zone 270. * Winds...Southwest 15 to 25 "
                "mph with gusts 30 to 40 mph. * Humidity...As low as 10 to 15 percent. "
                "* Duration...3 to 6 hours. * Impacts...The combination of gusty winds "
                "and low humidity can cause fire to rapidly grow."
            ),
        }
        assert alert_segments(alert) == [
            "Winds SW 15-25 mph, gusts 30-40 mph.",
            "RH to 10-15%.",
        ]

    def test_spc_watch_banner_reduces_to_number_and_cities(self):
        alert = {
            "description": (
                "THE NATIONAL WEATHER SERVICE HAS ISSUED SEVERE THUNDERSTORM WATCH 629 "
                "IN EFFECT UNTIL 11 PM MDT THIS EVENING FOR THE FOLLOWING AREAS IN "
                "COLORADO THIS WATCH INCLUDES 2 COUNTIES IN EAST CENTRAL COLORADO "
                "CHEYENNE KIT CARSON THIS INCLUDES THE CITIES OF ARAPAHOE, BURLINGTON, "
                "AND CHEYENNE WELLS."
            ),
        }
        assert alert_segments(alert) == [
            "SPC Wtch 629.",
            "Incl Arapahoe, Burlington, Cheyenne Wells.",
        ]

    def test_cancellation_is_one_word(self):
        alert = {
            "description": (
                "The dust storm which prompted the warning has weakened. Therefore, "
                "the Dust Storm Warning has been allowed to expire."
            ),
        }
        assert alert_segments(alert) == ["Cancelled (weakened)."]

    def test_long_instruction_trails_the_facts(self, fresh_warning):
        fresh_warning["instruction"] = (
            "Turn around, don't drown when encountering flooded roads. Most flood "
            "deaths occur in vehicles."
        )
        segments = alert_segments(fresh_warning)
        assert segments[0] == "Flash flooding is occurring."
        assert segments[1].startswith("Turn around, don't drown")
        # Generic safety statistics are surplus.
        assert len(segments) == 2

    def test_unabbreviated_form_keeps_full_words(self, tornado_warning):
        segments = alert_segments(tornado_warning, abbreviate=False)
        assert segments[1] == "Tornado 7 miles east of Lockwood, moving east at 15 mph."

    def test_headline_is_never_used(self, flood_watch):
        # The headline restates the event name and times already present in
        # the message, so spending 87 bytes on it would be waste.
        stripped = {
            k: v
            for k, v in flood_watch.items()
            if k not in ("instruction", "description")
        }
        assert alert_segments(stripped) == []


class TestBuildMeshMessage:
    @pytest.mark.parametrize("budget", [0, 5, 12, 20, 40, 80, 116, 141, 200])
    def test_always_fits_the_budget(self, budget, flood_watch, tornado_warning, now):
        for alert in (flood_watch, tornado_warning):
            message = build_mesh_message(alert, budget, now)
            assert len(message.encode("utf-8")) <= budget

    @pytest.mark.parametrize("budget", [10, 30, 60, 90, 141])
    def test_output_is_always_ascii(self, budget, flood_watch, now):
        build_mesh_message(flood_watch, budget, now).encode("ascii")

    def test_tornado_warning_at_the_reno_budget(self, tornado_warning):
        now = datetime(2026, 8, 13, 13, 25, tzinfo=PACIFIC)
        assert build_mesh_message(tornado_warning, 137, now) == (
            "Tornado Wrn: Lyon/Storey/Washoe til 14:15. TAKE COVER NOW! "
            "Tornado 7 mi E of Lockwood, moving E at 15 mph. Nr Derby Dam, 13:30."
        )

    def test_flood_watch_at_the_reno_budget(self, flood_watch):
        now = datetime(2026, 8, 12, 8, 19, tzinfo=PACIFIC)
        message = build_mesh_message(flood_watch, 137, now)
        assert message.startswith(
            "Flood Wtch: Greater Reno-Carson City-Minden Area 14:00-21:00 08/13. "
            "Flash flooding still possible. Excessive runoff may cause"
        )

    def test_includes_every_header_segment_when_there_is_room(self, fresh_warning, now):
        message = build_mesh_message(fresh_warning, 141, now)
        assert message.startswith("Flash Flood Wrn: Washoe Co til ")
        assert "Move to higher ground now." in message

    def test_drops_area_before_window_when_space_runs_out(self, flood_watch, now):
        # The monitored point is fixed by configuration so the area is largely
        # implied, but "until when" never is.
        message = build_mesh_message(flood_watch, 40, now)
        assert "til" in message or "-" in message
        assert "Minden" not in message

    def test_keeps_the_event_name_above_all_else(self, flood_watch, now):
        assert build_mesh_message(flood_watch, 12, now) == "Flood Wtch"

    def test_truncates_an_event_name_that_cannot_fit(self, now):
        alert = {"event": "Extremely Long Product Name That Cannot Possibly Fit"}
        message = build_mesh_message(alert, 15, now)
        assert len(message.encode("utf-8")) <= 15
        assert message.endswith("...")

    @pytest.mark.parametrize("budget", range(24, 145, 7))
    def test_never_ends_mid_word(self, budget, tornado_warning, flood_watch, now):
        # Either the message ends on a complete segment, or it ends with an
        # ellipsis placed at a sentence, clause, or word boundary. It never
        # ends with a partial word.
        for alert in (tornado_warning, flood_watch):
            message = build_mesh_message(alert, budget, now)
            if message.endswith("..."):
                body = message[:-3]
                assert body == body.rstrip(" ,;.:-")
            else:
                assert message.endswith((".", "!", "?")) or ". " not in message

    def test_omits_a_segment_rather_than_leaving_a_stub(self, flood_watch, now):
        # Below the minimum fill, a few trailing characters of prose are noise.
        for budget in range(60, 90):
            message = build_mesh_message(flood_watch, budget, now)
            assert not message.rstrip().endswith(". ...")

    def test_handles_an_entirely_empty_alert(self, now):
        assert build_mesh_message({}, 100, now)
