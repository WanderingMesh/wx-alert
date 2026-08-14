"""Configuration loading and validation.

Validation failures must surface at startup. A container that refuses to boot
is far easier to diagnose than one that polls for a week and delivers nothing.
"""

from __future__ import annotations

import configparser
import textwrap
from pathlib import Path

import pytest

from wx_alert.config import (
    ConfigurationError,
    load_configuration,
    load_meshcore_configuration,
    normalize_server_url,
    read_config_file,
)

MINIMAL = """
[weather]
DEFAULT_LATITUDE = 39.5296
DEFAULT_LONGITUDE = -119.8138

[ntfy]
TOPIC = a-topic

[delivery]
"""


def parse(text: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(textwrap.dedent(text))
    return parser


class TestCoreConfiguration:
    def test_loads_a_minimal_file(self):
        config = load_configuration(parse(MINIMAL))
        assert config.latitude == 39.5296
        assert config.ntfy_topic == "a-topic"

    def test_applies_documented_defaults(self):
        config = load_configuration(parse(MINIMAL))
        assert config.ntfy_server == "https://ntfy.sh"
        assert config.ntfy_priority == "auto"
        assert config.state_file == Path("/data/notified-alerts.json")

    def test_a_blank_topic_becomes_none(self):
        # The tracked config.ini ships with a blank topic, and "" must not be
        # mistaken for a configured value.
        config = load_configuration(parse(MINIMAL.replace("a-topic", "")))
        assert config.ntfy_topic is None

    @pytest.mark.parametrize(
        ("section", "option", "value"),
        [
            ("weather", "DEFAULT_LATITUDE", "91"),
            ("weather", "DEFAULT_LATITUDE", "not-a-number"),
            ("weather", "DEFAULT_LONGITUDE", "-181"),
            ("ntfy", "PRIORITY", "loud"),
            ("ntfy", "SERVER", "ntfy.sh"),
            ("delivery", "DELAY_SECONDS", "0"),
            ("delivery", "DELAY_SECONDS", "61"),
            ("delivery", "CHECK_INTERVAL", "4"),
            ("delivery", "CHECK_INTERVAL", "3601"),
            ("delivery", "STARTUP_MAX_AGE_SECONDS", "-1"),
            ("delivery", "EXIT_AFTER_FAILED_CYCLES", "-1"),
        ],
    )
    def test_rejects_invalid_values(self, section, option, value):
        parser = parse(MINIMAL)
        if not parser.has_section(section):
            parser.add_section(section)
        parser.set(section, option, value)

        with pytest.raises(ConfigurationError):
            load_configuration(parser)

    @pytest.mark.parametrize("section", ["weather", "ntfy", "delivery"])
    def test_requires_core_sections(self, section):
        parser = parse(MINIMAL)
        parser.remove_section(section)
        with pytest.raises(ConfigurationError, match=section):
            load_configuration(parser)

    def test_state_section_is_optional(self):
        assert load_configuration(parse(MINIMAL)).state_retention_days == 14

    def test_rejects_an_out_of_range_retention(self):
        parser = parse(MINIMAL + "\n[state]\nRETENTION_DAYS = 0\n")
        with pytest.raises(ConfigurationError):
            load_configuration(parser)


class TestZonesAndRadius:
    def test_zones_are_optional(self):
        # The county containing the point is resolved at startup, so an
        # existing config file needs no edit to gain polygon warnings.
        assert load_configuration(parse(MINIMAL)).zones == ()

    def test_applies_the_default_radius(self):
        assert load_configuration(parse(MINIMAL)).alert_radius_km == 50.0

    def test_reads_a_zone_list(self):
        config = load_configuration(
            parse(MINIMAL.replace("[ntfy]", "ZONES = NVC031, nvc029\n\n[ntfy]"))
        )
        assert config.zones == ("NVC031", "NVC029")

    def test_rejects_a_malformed_zone(self):
        # Failing at startup beats querying a zone that returns nothing.
        with pytest.raises(ConfigurationError, match="UGC"):
            load_configuration(
                parse(MINIMAL.replace("[ntfy]", "ZONES = Washoe\n\n[ntfy]"))
            )

    def test_reads_a_radius(self):
        config = load_configuration(
            parse(MINIMAL.replace("[ntfy]", "ALERT_RADIUS_KM = 25.5\n\n[ntfy]"))
        )
        assert config.alert_radius_km == 25.5

    def test_zero_is_accepted_and_disables_the_test(self):
        config = load_configuration(
            parse(MINIMAL.replace("[ntfy]", "ALERT_RADIUS_KM = 0\n\n[ntfy]"))
        )
        assert config.alert_radius_km == 0

    @pytest.mark.parametrize("value", ["-1", "5000", "wide"])
    def test_rejects_an_impossible_radius(self, value):
        with pytest.raises(ConfigurationError):
            load_configuration(
                parse(
                    MINIMAL.replace("[ntfy]", f"ALERT_RADIUS_KM = {value}\n\n[ntfy]")
                )
            )


class TestServerUrl:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://ntfy.sh/", "https://ntfy.sh"),
            ("  http://local:8080  ", "http://local:8080"),
        ],
    )
    def test_normalizes(self, raw, expected):
        assert normalize_server_url(raw) == expected

    @pytest.mark.parametrize("raw", ["ntfy.sh", "ftp://ntfy.sh", "https://", ""])
    def test_rejects_malformed(self, raw):
        with pytest.raises(ConfigurationError):
            normalize_server_url(raw)


class TestMeshCoreConfiguration:
    def test_absent_section_disables_the_transport(self):
        assert not load_meshcore_configuration(parse(MINIMAL)).enabled

    def test_defaults_are_conservative(self):
        # Airtime is shared, so the out-of-box posture should carry only
        # products that are actually happening.
        mesh = load_meshcore_configuration(parse(MINIMAL))
        assert mesh.minimum_class == "warning"
        assert mesh.minimum_severity == "severe"
        assert mesh.max_per_hour > 0
        assert mesh.min_interval_seconds > 0

    def test_reads_a_full_section(self):
        mesh = load_meshcore_configuration(
            parse(
                MINIMAL
                + """
                [meshcore]
                ENABLED = true
                PORT = /dev/meshcore
                CHANNEL_INDEX = 2
                MIN_CLASS = watch
                MIN_SEVERITY = moderate
                MAX_SENDS_PER_HOUR = 6
                """
            )
        )
        assert mesh.enabled
        assert mesh.port == "/dev/meshcore"
        assert mesh.channel_index == 2
        assert mesh.minimum_class == "watch"
        assert mesh.max_per_hour == 6

    def test_repeats_each_message_by_default(self):
        # Channel messages are unacknowledged, so one copy can vanish with no
        # way to notice.
        mesh = load_meshcore_configuration(parse(MINIMAL))
        assert mesh.repeat_sends == 2
        assert mesh.repeat_min_delay >= 1
        assert mesh.repeat_max_delay >= mesh.repeat_min_delay

    def test_recovers_a_hung_radio_by_default(self):
        assert load_meshcore_configuration(parse(MINIMAL)).auto_reset

    def test_a_sub_second_repeat_gap_is_rejected(self):
        # Both copies would carry the same timestamp, hash identically, and
        # the mesh would discard the repeat as an already-forwarded packet.
        # Accepting this would make the setting silently do nothing.
        with pytest.raises(ConfigurationError, match="at least 1 second"):
            load_meshcore_configuration(
                parse(MINIMAL + "\n[meshcore]\nREPEAT_MIN_DELAY = 0\n")
            )

    def test_a_reversed_repeat_range_is_rejected(self):
        with pytest.raises(ConfigurationError, match="REPEAT_MAX_DELAY"):
            load_meshcore_configuration(
                parse(
                    MINIMAL
                    + "\n[meshcore]\nREPEAT_MIN_DELAY = 10\nREPEAT_MAX_DELAY = 2\n"
                )
            )

    @pytest.mark.parametrize("value", ["0", "6", "-1"])
    def test_an_out_of_range_repeat_count_is_rejected(self, value):
        with pytest.raises(ConfigurationError, match="REPEAT_SENDS"):
            load_meshcore_configuration(
                parse(MINIMAL + f"\n[meshcore]\nREPEAT_SENDS = {value}\n")
            )

    def test_repeating_can_be_turned_off(self):
        mesh = load_meshcore_configuration(
            parse(MINIMAL + "\n[meshcore]\nREPEAT_SENDS = 1\n")
        )
        assert mesh.repeat_sends == 1

    def test_scope_defaults_to_unset(self):
        # Absent means the radio's own scope state is left alone, which is
        # what every deployment predating this option expects.
        mesh = load_meshcore_configuration(parse(MINIMAL + "\n[meshcore]\n"))
        assert mesh.scope is None

    def test_a_blank_scope_is_unset(self):
        mesh = load_meshcore_configuration(
            parse(MINIMAL + "\n[meshcore]\nSCOPE =\n")
        )
        assert mesh.scope is None

    def test_a_scope_gains_its_marker(self):
        mesh = load_meshcore_configuration(
            parse(MINIMAL + "\n[meshcore]\nSCOPE = rno\n")
        )
        assert mesh.scope == "#rno"

    def test_scope_case_survives_the_config_file(self):
        mesh = load_meshcore_configuration(
            parse(MINIMAL + "\n[meshcore]\nSCOPE = NorthNV\n")
        )
        assert mesh.scope == "#NorthNV"

    @pytest.mark.parametrize(
        ("option", "value"),
        [
            ("MIN_CLASS", "bogus"),
            ("MIN_SEVERITY", "bogus"),
            ("CHANNEL_INDEX", "256"),
            ("CHANNEL_INDEX", "-1"),
            ("BAUD", "0"),
            ("MAX_SENDS_PER_HOUR", "-1"),
            ("MIN_SECONDS_BETWEEN_SENDS", "-1"),
            ("ENABLED", "maybe"),
            ("SCOPE", "northern nevada"),
            ("SCOPE", "0"),
        ],
    )
    def test_rejects_invalid_values(self, option, value):
        parser = parse(MINIMAL + "\n[meshcore]\n")
        parser.set("meshcore", option, value)
        with pytest.raises(ConfigurationError):
            load_meshcore_configuration(parser)


class TestReadConfigFile:
    def test_reports_a_missing_file(self, tmp_path):
        with pytest.raises(ConfigurationError, match="not found"):
            read_config_file(tmp_path / "absent.ini")

    def test_reports_malformed_ini(self, tmp_path):
        path = tmp_path / "bad.ini"
        path.write_text("this is not ini", encoding="utf-8")
        with pytest.raises(ConfigurationError):
            read_config_file(path)

    def test_the_shipped_config_is_valid(self):
        # The tracked config.ini is baked into the image, so a mistake in it
        # breaks every deployment.
        path = Path(__file__).resolve().parent.parent / "config.ini"
        config = load_configuration(read_config_file(path))
        assert config.ntfy_topic is None, "the template must not carry a real topic"
        assert config.ntfy_token is None, "the template must not carry a real token"


class TestMeshCoreStartupAgeOverride:
    """The radio's staleness limit has to be reachable from the command line.

    --startup-max-age governs ntfy only. Documenting it as the way to preview
    radio output produced a dry run that rendered nothing, because every alert
    currently active is normally older than the limit.
    """

    @pytest.fixture
    def config_file(self, tmp_path):
        path = tmp_path / "config.ini"
        path.write_text(
            textwrap.dedent(
                MINIMAL
                + """
                [meshcore]
                ENABLED = true
                PORT = /dev/meshcore
                STARTUP_MAX_AGE_SECONDS = 900
                """
            ),
            encoding="utf-8",
        )
        return path

    def build_mesh_policy(self, config_file, argv):
        from wx_alert.__main__ import build_transports
        from wx_alert.cli import parse_arguments

        args = parse_arguments(["--config", str(config_file), *argv])
        transports = build_transports(None, args)
        mesh = next(t for t in transports if t.name == "meshcore")
        return mesh._startup_policy

    def test_defaults_to_the_configured_limit(self, config_file):
        policy = self.build_mesh_policy(config_file, ["--meshcore"])
        assert policy.max_age_seconds == 900

    def test_the_flag_overrides_the_file(self, config_file):
        policy = self.build_mesh_policy(
            config_file, ["--meshcore", "--meshcore-startup-max-age", "0"]
        )
        assert not policy.enabled

    def test_the_remaining_life_threshold_tracks_the_limit(self, config_file):
        # One number for an operator to reason about: too old to replay past
        # the limit, worth the airtime while it has that long left to run.
        policy = self.build_mesh_policy(
            config_file, ["--meshcore", "--meshcore-startup-max-age", "1200"]
        )
        assert policy.warning_min_remaining_seconds == 1200
        assert not policy.always_notify_warnings

    def test_the_ntfy_flag_does_not_reach_the_radio(self, config_file):
        policy = self.build_mesh_policy(
            config_file, ["--meshcore", "--startup-max-age", "0"]
        )
        assert policy.max_age_seconds == 900
