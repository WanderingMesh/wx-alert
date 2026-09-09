"""The vocabulary and rules that compress NWS prose.

These pin down the behaviour that makes the abbreviations safe to read: case
is preserved, hazard words are never touched, and the two dictionaries stay in
their own lanes.
"""

from __future__ import annotations

import re

import pytest

from wx_alert.abbrev import (
    NAME_TERMS,
    PROSE_TERMS,
    abbreviate_name,
    abbreviate_prose,
    collapse_acronyms,
    compress_sentences,
    convert_clock,
    polish,
)

# Words whose meaning is the alert. If any of these ever appears in a
# dictionary, a stranger on the channel stops being able to read the message.
HAZARD_WORDS = ("flood", "fire", "tornado", "heat", "wind", "freeze", "frost", "fog")


class TestDictionaries:
    @pytest.mark.parametrize("word", HAZARD_WORDS)
    def test_no_dictionary_entry_matches_a_hazard_word_on_its_own(self, word):
        # "wind gusts" -> "gusts" is allowed; a pattern that would rewrite the
        # bare word "wind" is not.
        for pattern, _ in NAME_TERMS + PROSE_TERMS:
            assert re.fullmatch(pattern, word, re.IGNORECASE) is None

    @pytest.mark.parametrize("word", HAZARD_WORDS)
    def test_hazard_words_pass_through_untouched(self, word):
        assert abbreviate_name(word.title()) == word.title()
        assert abbreviate_prose(word) == word

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("Winter Storm Warning", "Winter Strm Wrn"),
            ("Severe Thunderstorm Watch", "Svr Tstorm Wtch"),
            ("Excessive Heat Warning", "Excsv Heat Wrn"),
            ("Northern Washoe County", "N Washoe Co"),
            ("Eastern Sierra Counties", "E Sierra Cos"),
        ],
    )
    def test_product_names(self, source, expected):
        assert abbreviate_name(source) == expected

    def test_product_vocabulary_does_not_leak_into_prose(self):
        # "Excsv rainfall" and "Sub-Frzng temps" read as typos. The prose
        # dictionary leaves those words alone.
        assert (
            abbreviate_prose("caused by excessive rainfall")
            == "caused by excessive rainfall"
        )
        assert abbreviate_prose("Sub-freezing temperatures") == "Sub-freezing temps"

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("The National Weather Service in Reno", "The NWS in Reno"),
            ("Doppler radar indicated thunderstorms", "Radar indicated tstorms"),
            ("the Doppler radar indicated", "the radar indicated"),
            ("7 miles east of Lockwood", "7 mi E of Lockwood"),
            ("moving northeast at 35 mph", "moving NE at 35 mph"),
            (
                "Southwest winds 15 to 25 mph with gusts up to 40 mph",
                "SW winds 15-25 mph, gusts to 40 mph",
            ),
            ("temperatures between 98 and 108 degrees", "temps 98-108 deg"),
            ("As low as 10 to 15 percent", "To 10-15%"),
            ("Visibility less than three miles", "Vis under 3 mi"),
            ("half inch hail", '1/2" hail'),
            ("valid through Saturday", "valid thru Sat"),
        ],
    )
    def test_prose(self, source, expected):
        assert abbreviate_prose(source) == expected

    def test_case_follows_the_source_word(self):
        assert (
            abbreviate_prose("SEVERE THUNDERSTORM WATCH 629") == "SVR TSTORM WTCH 629"
        )
        assert abbreviate_prose("Thunderstorm") == "Tstorm"
        assert abbreviate_prose("thunderstorm") == "tstorm"

    def test_function_words_do_not_inherit_a_capital_mid_sentence(self):
        # "as low as" -> "to" must not become "To" in the middle of a bullet,
        # but does take the capital at the start of one.
        assert abbreviate_prose("RH As low as 10 percent") == "RH to 10%"
        assert abbreviate_prose("As low as 10 percent") == "To 10%"

    def test_plurals_are_preserved(self):
        assert abbreviate_prose("thunderstorms") == "tstorms"
        assert abbreviate_prose("temperatures") == "temps"


class TestRules:
    def test_agency_name_collapses_to_its_own_acronym(self):
        text = "The Arizona Department of Environmental Quality (ADEQ) has issued"
        assert collapse_acronyms(text) == "ADEQ has issued"

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("At 130 PM PDT", "At 13:30"),
            ("At 1126 AM PDT /1126 AM MST/, a storm", "At 11:26, a storm"),
            ("until 8 PM MST this evening", "until 20:00 this evening"),
            ("around 1200 AM", "around 00:00"),
            ("1215 PM", "12:15"),
        ],
    )
    def test_clock_stamps_become_24_hour(self, source, expected):
        assert convert_clock(source) == expected

    @pytest.mark.parametrize(
        "sentence",
        [
            "Blowing dust can be hazardous.",
            "Remain aware of the weather.",
            "Monitor the latest forecasts and warnings for updates.",
            "The National Weather Service in Reno has issued a Dust Advisory.",
            "Most flood deaths occur in vehicles.",
            "The Dense Fog Advisory has been cancelled and is no longer in effect.",
        ],
    )
    def test_surplus_sentences_are_dropped(self, sentence):
        assert compress_sentences(sentence) == []

    def test_non_nws_issuer_keeps_the_product_and_credits_the_agency(self):
        text = "ADEQ has issued a PM-10 High Pollution Adv for Pinal Co thru Sat."
        assert compress_sentences(text) == ["PM-10 High Pollution Adv thru Sat (ADEQ)."]

    def test_header_restatements_are_stripped_from_within_a_sentence(self):
        text = "For the High Wind Warning on Wednesday, southwest winds 35 to 45 mph."
        assert polish(text) == ["SW winds 35-45 mph."]

    def test_every_sentence_ends_with_punctuation_and_a_capital(self):
        assert polish("flash flooding is occurring") == ["Flash flooding is occurring."]

    def test_polish_can_run_without_the_dictionary(self):
        assert polish("Winds southwest 15 to 25 mph.", abbreviate=False) == [
            "Winds southwest 15 to 25 mph."
        ]
