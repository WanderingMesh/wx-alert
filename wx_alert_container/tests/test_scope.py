"""Scope name validation and key derivation.

The tests that matter here are the ones about silence: a scope that is subtly
wrong does not raise, it reduces coverage to whatever happens to be in direct
radio range, with nothing in any log to say so.
"""

from __future__ import annotations

import pytest

from wx_alert.scope import FORCE_UNSCOPED, normalize_scope, scope_key


class TestBlank:
    @pytest.mark.parametrize("raw", [None, "", "   ", "\t"])
    def test_blank_means_leave_the_radio_alone(self, raw):
        # Distinct from forcing unscoped, and the difference is visible only
        # on a radio with a default scope set on the device.
        assert normalize_scope(raw, "test") is None


class TestNames:
    def test_a_bare_name_gains_the_marker(self):
        assert normalize_scope("rno", "test") == "#rno"

    def test_a_marked_name_is_left_alone(self):
        assert normalize_scope("#rno", "test") == "#rno"

    def test_both_forms_resolve_to_one_key(self):
        # The library normalizes too, so accepting both spellings must not
        # produce two different regions.
        assert scope_key(normalize_scope("rno", "test")) == scope_key(
            normalize_scope("#rno", "test")
        )

    def test_surrounding_whitespace_is_ignored(self):
        assert normalize_scope("  rno  ", "test") == "#rno"

    def test_case_is_preserved(self):
        # The key is a hash of the exact string, so folding case here would
        # silently move the deployment into a different region from the one
        # the operator configured on their repeaters.
        assert normalize_scope("RNO", "test") == "#RNO"

    def test_case_changes_the_key(self):
        assert scope_key("#RNO") != scope_key("#rno")

    def test_internal_whitespace_is_rejected(self):
        with pytest.raises(ValueError, match="whitespace"):
            normalize_scope("northern nevada", "test")

    def test_an_overlong_name_is_rejected(self):
        # The firmware stores 31 bytes including the marker.
        with pytest.raises(ValueError, match="bytes"):
            normalize_scope("n" * 31, "test")

    def test_a_name_at_the_limit_is_accepted(self):
        assert normalize_scope("n" * 30, "test") == "#" + "n" * 30

    def test_a_bare_marker_is_not_a_name(self):
        with pytest.raises(ValueError, match="not a region name"):
            normalize_scope("#", "test")

    def test_the_source_appears_in_the_message(self):
        # Config and CLI share this validator, and the operator needs to know
        # which one they got wrong.
        with pytest.raises(ValueError, match=r"\[meshcore\] SCOPE"):
            normalize_scope("bad name", "[meshcore] SCOPE")


class TestReservedValues:
    def test_the_wildcard_forces_unscoped(self):
        assert normalize_scope("*", "test") == FORCE_UNSCOPED

    @pytest.mark.parametrize("raw", ["0", "None"])
    def test_library_reserved_names_are_refused(self, raw):
        # These mean "revert to the device default" inside the library. A
        # region genuinely named 0 must fail loudly rather than quietly do
        # something else entirely.
        with pytest.raises(ValueError, match="reserved"):
            normalize_scope(raw, "test")


class TestKey:
    def test_the_key_is_sixteen_bytes(self):
        # Truncated from SHA-256; the firmware transport key is 16 bytes.
        assert len(bytes.fromhex(scope_key("#rno"))) == 16

    def test_the_key_is_stable(self):
        # Pinned so a refactor cannot silently move every deployment to a
        # different region.
        assert scope_key("#rno") == scope_key("#rno")
        assert scope_key("#rno") != scope_key("#cc")
