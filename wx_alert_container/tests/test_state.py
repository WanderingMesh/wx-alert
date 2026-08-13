"""Persistent delivery history and the startup staleness policy."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from wx_alert.policy import StartupPolicy, should_suppress_on_startup
from wx_alert.state import (
    STATE_VERSION,
    AlertState,
    StateError,
    load_state,
    prune_state,
    save_state,
)


class TestSettlement:
    def test_records_and_recognizes_a_delivery(self, flood_watch):
        state = AlertState()
        assert not state.is_settled(flood_watch, "ntfy")
        state.record(flood_watch, "ntfy", "delivered")
        assert state.is_settled(flood_watch, "ntfy")

    def test_transports_settle_independently(self, flood_watch):
        # An ntfy success paired with a radio failure has to be
        # representable, or the next cycle either duplicates the
        # notification or permanently skips the broadcast.
        state = AlertState()
        state.record(flood_watch, "ntfy", "delivered")
        assert state.is_settled(flood_watch, "ntfy")
        assert not state.is_settled(flood_watch, "meshcore")

    def test_a_content_update_reopens_the_alert(self, flood_watch):
        state = AlertState()
        state.record(flood_watch, "ntfy", "delivered")
        updated = dict(flood_watch, description="the situation has changed")
        assert not state.is_settled(updated, "ntfy")

    def test_has_seen_survives_a_content_update(self, flood_watch):
        state = AlertState()
        state.record(flood_watch, "ntfy", "delivered")
        updated = dict(flood_watch, description="changed")
        assert state.has_seen(updated)

    def test_a_skip_settles_the_alert(self, flood_watch):
        # A skip is a decision, not an error. Recording it prevents the alert
        # from being reconsidered every cycle forever.
        state = AlertState()
        state.record(flood_watch, "meshcore", "skipped:filtered")
        assert state.is_settled(flood_watch, "meshcore")

    def test_has_record_survives_a_reissue(self, flood_watch):
        # is_settled goes false again when NWS reissues the product, which is
        # correct for delivery but useless for asking "has this transport had a
        # turn at this alert?" — the question the staleness policy needs.
        state = AlertState()
        state.record(flood_watch, "meshcore", "delivered")
        updated = dict(flood_watch, description="the situation has changed")

        assert not state.is_settled(updated, "meshcore")
        assert state.has_record(updated, "meshcore")

    def test_has_record_is_per_transport(self, flood_watch):
        # An alert the radio has never recorded is new to the radio even if
        # ntfy pushed it an hour ago. This is also what keeps a migrated
        # single-transport state file from replaying its backlog on air.
        state = AlertState()
        state.record(flood_watch, "ntfy", "delivered")

        assert state.has_record(flood_watch, "ntfy")
        assert not state.has_record(flood_watch, "meshcore")

    def test_has_record_is_false_for_an_unknown_alert(self, flood_watch):
        assert not AlertState().has_record(flood_watch, "meshcore")


class TestPersistence:
    def test_round_trips(self, tmp_path, flood_watch):
        path = tmp_path / "state.json"
        state = AlertState()
        state.record(flood_watch, "ntfy", "delivered")
        save_state(path, state)

        assert load_state(path).is_settled(flood_watch, "ntfy")

    def test_missing_file_starts_empty(self, tmp_path):
        assert load_state(tmp_path / "absent.json").alerts == {}

    def test_creates_missing_parent_directories(self, tmp_path):
        path = tmp_path / "a" / "b" / "state.json"
        save_state(path, AlertState())
        assert path.is_file()

    def test_write_is_atomic_and_leaves_no_debris(self, tmp_path, flood_watch):
        # os.replace within a directory is atomic, so a crash mid-write leaves
        # the previous good file rather than a truncated one.
        path = tmp_path / "state.json"
        state = AlertState()
        state.record(flood_watch, "ntfy", "delivered")
        save_state(path, state)
        save_state(path, state)
        assert [p.name for p in tmp_path.iterdir()] == ["state.json"]

    @pytest.mark.parametrize(
        "content",
        ['{"version": 99}', "{not json", "[]", '"a string"'],
    )
    def test_refuses_unusable_files(self, tmp_path, content):
        # Starting empty on a bad file would silently re-deliver every
        # currently active alert, so refusing to start is the safer failure.
        path = tmp_path / "state.json"
        path.write_text(content, encoding="utf-8")
        with pytest.raises(StateError):
            load_state(path)


class TestMigration:
    def test_upgrades_a_version_1_file(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "alerts": {
                        "urn:oid:old": {
                            "fingerprint": "abc123",
                            "status": "delivered",
                            "event": "Winter Storm Warning",
                            "nws_sent": "2026-08-01T00:00:00+00:00",
                            "recorded_at": "2026-08-01T00:05:00+00:00",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        state = load_state(path)
        record = state.alerts["urn:oid:old"]["transports"]

        # Version 1 only ever tracked ntfy, so its single status belongs there.
        assert record["ntfy"]["fingerprint"] == "abc123"
        assert "meshcore" not in record

    def test_migration_is_persisted_immediately(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text(
            json.dumps({"version": 1, "alerts": {}}),
            encoding="utf-8",
        )
        load_state(path)
        assert json.loads(path.read_text())["version"] == STATE_VERSION

    def test_a_migrated_alert_is_unsettled_for_new_transports(
        self, tmp_path, flood_watch
    ):
        # Leaving the radio absent is deliberate and meaningful: it marks the
        # transport as never attempted, which subjects the alert to the
        # startup staleness policy rather than replaying a backlog on air.
        path = tmp_path / "state.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "alerts": {
                        flood_watch["id"]: {
                            "fingerprint": "whatever",
                            "status": "delivered",
                            "recorded_at": "2026-08-01T00:05:00+00:00",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        assert not load_state(path).is_settled(flood_watch, "meshcore")


class TestPruning:
    def test_drops_records_past_the_retention_window(self, flood_watch):
        state = AlertState()
        state.record(flood_watch, "ntfy", "delivered")
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        state.alerts[flood_watch["id"]]["recorded_at"] = old

        assert prune_state(state, retention_days=14) == 1
        assert state.alerts == {}

    def test_keeps_recent_records(self, flood_watch):
        state = AlertState()
        state.record(flood_watch, "ntfy", "delivered")
        assert prune_state(state, retention_days=14) == 0

    def test_keeps_records_with_an_unreadable_timestamp(self, flood_watch):
        # Deleting one risks a duplicate notification, so keep it.
        state = AlertState()
        state.record(flood_watch, "ntfy", "delivered")
        state.alerts[flood_watch["id"]]["recorded_at"] = "corrupt"
        assert prune_state(state, retention_days=1) == 0


class TestStartupPolicy:
    @pytest.fixture
    def policy(self):
        return StartupPolicy(max_age_seconds=900, always_notify_warnings=True)

    def test_suppresses_a_stale_watch_on_the_first_cycle(
        self, policy, flood_watch, now
    ):
        # The captured alert was issued about three hours before `now`.
        suppress, reason = should_suppress_on_startup(
            flood_watch, policy, first_cycle=True, now=now
        )
        assert suppress
        assert "exceeds startup limit" in reason

    def test_allows_a_stale_warning_through_when_configured(self, policy, now):
        # Silently swallowing an active warning is the one failure mode worth
        # accepting duplicates to avoid.
        old = (now - timedelta(hours=3)).isoformat()
        warning = {"event": "Tornado Warning", "sent": old}
        assert not should_suppress_on_startup(warning, policy, True, now)[0]

    def test_suppresses_a_stale_warning_when_the_bypass_is_off(self, now):
        # The radio uses this: replaying hours-old warnings onto a shared
        # channel after every restart costs everyone airtime.
        strict = StartupPolicy(max_age_seconds=900, always_notify_warnings=False)
        old = (now - timedelta(hours=3)).isoformat()
        warning = {"event": "Tornado Warning", "sent": old}
        assert should_suppress_on_startup(warning, strict, True, now)[0]

    def test_does_not_apply_after_the_first_cycle(self, policy, flood_watch, now):
        assert not should_suppress_on_startup(
            flood_watch, policy, first_cycle=False, now=now
        )[0]

    def test_zero_disables_the_policy(self, flood_watch, now):
        disabled = StartupPolicy(max_age_seconds=0, always_notify_warnings=False)
        assert not should_suppress_on_startup(flood_watch, disabled, True, now)[0]

    def test_allows_a_fresh_alert(self, policy, fresh_warning, now):
        assert not should_suppress_on_startup(fresh_warning, policy, True, now)[0]

    def test_suppresses_an_alert_with_no_issue_time(self, policy, now):
        # Unknown age on the first cycle is more likely stale than fresh.
        suppress, reason = should_suppress_on_startup(
            {"event": "Flood Watch"}, policy, True, now
        )
        assert suppress
        assert "unavailable" in reason

    def test_an_alert_this_transport_already_handled_is_not_a_backlog(
        self, policy, flood_watch, now
    ):
        # Persistent state proves this transport has had a turn at this alert,
        # so it did not arrive with the cold-start backlog. Either NWS reissued
        # it, or the last attempt failed and this is the retry — and a radio
        # that was unreachable throughout a warning must not have that warning
        # written off the moment it comes back.
        assert not should_suppress_on_startup(
            flood_watch, policy, True, now, previously_handled=True
        )[0]


class TestStartupPolicyRemainingLife:
    """The radio's alternative to an unconditional warning bypass.

    How old a warning is says nothing about whether it still matters. How long
    it has left to run says exactly that, which lets the radio carry a warning
    that is still in force without replaying an expired backlog.
    """

    @pytest.fixture
    def policy(self):
        return StartupPolicy(
            max_age_seconds=900,
            always_notify_warnings=False,
            warning_min_remaining_seconds=900,
        )

    def warning(self, now, issued_hours_ago, expires_in_minutes):
        return {
            "event": "Tornado Warning",
            "sent": (now - timedelta(hours=issued_hours_ago)).isoformat(),
            "expires": (now + timedelta(minutes=expires_in_minutes)).isoformat(),
        }

    def test_an_old_warning_still_in_force_is_allowed(self, policy, now):
        alert = self.warning(now, issued_hours_ago=4, expires_in_minutes=45)
        assert not should_suppress_on_startup(alert, policy, True, now)[0]

    def test_an_old_warning_about_to_expire_is_suppressed(self, policy, now):
        # Two minutes of validity left is not worth a flood-routed broadcast.
        alert = self.warning(now, issued_hours_ago=4, expires_in_minutes=2)
        suppress, reason = should_suppress_on_startup(alert, policy, True, now)
        assert suppress
        assert "exceeds startup limit" in reason

    def test_an_expired_warning_is_suppressed(self, policy, now):
        alert = self.warning(now, issued_hours_ago=4, expires_in_minutes=-30)
        assert should_suppress_on_startup(alert, policy, True, now)[0]

    def test_the_bypass_does_not_extend_to_lesser_products(self, policy, now):
        # A watch running for another six hours is exactly the backlog this
        # policy exists to keep off a shared channel.
        watch = {
            "event": "Flood Watch",
            "sent": (now - timedelta(hours=4)).isoformat(),
            "expires": (now + timedelta(hours=6)).isoformat(),
        }
        assert should_suppress_on_startup(watch, policy, True, now)[0]

    def test_a_warning_with_no_expiry_falls_back_to_age(self, policy, now):
        alert = {
            "event": "Tornado Warning",
            "sent": (now - timedelta(hours=4)).isoformat(),
        }
        assert should_suppress_on_startup(alert, policy, True, now)[0]

    def test_zero_disables_the_remaining_life_test(self, now):
        without = StartupPolicy(
            max_age_seconds=900,
            always_notify_warnings=False,
            warning_min_remaining_seconds=0,
        )
        alert = self.warning(now, issued_hours_ago=4, expires_in_minutes=45)
        assert should_suppress_on_startup(alert, without, True, now)[0]
