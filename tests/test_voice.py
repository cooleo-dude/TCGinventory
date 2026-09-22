"""Voice search (ticket 76): transcript → query parsing and model location.

Headless and Qt-free. Nothing here loads the speech model — ``transcribe`` is
exercised by hand against a microphone; what a unit test can pin down is that
the words Whisper returns become the text the filter bar needs.
"""

from __future__ import annotations

import pytest

from tcginventory.services import voice

NAMES = ["Pikachu", "Charizard", "Umbreon VMAX", "Umbreon V", "Mewtwo"]
SETS = ["Base Set", "Base Set 2", "151", "Evolving Skies", "Obsidian Flames"]


def parse(text: str) -> str:
    return voice.parse_query(text, NAMES, SETS)


# -- the three spoken shapes --------------------------------------------------


def test_a_bare_name_passes_through_lowercased():
    assert parse("Pikachu") == "pikachu"
    assert parse("Pikachu.") == "pikachu"  # Whisper likes a full stop


def test_name_then_set_snaps_both():
    assert parse("Pikachu base set") == "pikachu base set"
    assert parse("Charizard, Obsidian Flames.") == "charizard obsidian flames"


@pytest.mark.parametrize(
    "spoken",
    [
        "Pikachu 25 out of 102",
        "Pikachu 25/102",
        "Pikachu 25 / 102",
        "Pikachu twenty five out of one oh two",
        "Pikachu twenty five of one hundred and two",
        "Pikachu twenty five over one hundred two",
        "Pikachu twenty five slash one oh two",
    ],
)
def test_name_then_number_becomes_the_catalog_shape(spoken):
    assert parse(spoken) == "pikachu 25/102"


# -- number reading -------------------------------------------------------------


@pytest.mark.parametrize(
    "spoken, digits",
    [
        ("one oh two", "102"),
        ("one zero two", "102"),
        ("one hundred and two", "102"),
        ("one hundred two", "102"),
        ("twenty five", "25"),
        ("one sixty five", "165"),
        ("one fifteen", "115"),
        ("two hundred", "200"),
        ("seven", "7"),
        ("198", "198"),
    ],
)
def test_spoken_numbers_read_as_digits(spoken, digits):
    assert voice.parse_query(spoken) == digits


def test_two_numbers_in_a_row_stay_two_numbers():
    # "twenty five" is complete once its units are spoken; "six" starts anew.
    assert voice.parse_query("twenty five six") == "25 6"


def test_a_set_that_is_a_number_snaps_as_a_set():
    assert parse("Umbreon 151") == "umbreon 151"


# -- snapping ---------------------------------------------------------------------


def test_a_misheard_name_snaps_to_the_owned_card():
    assert parse("pikachoo twenty five out of one oh two") == "pikachu 25/102"
    assert parse("Charizard") == "charizard"
    assert parse("Charizard base set two") == "charizard base set 2"


def test_a_name_that_is_the_start_of_several_cards_yields_the_shared_prefix():
    # "Umbreon" against Umbreon VMAX and Umbreon V → the token both rows carry.
    assert parse("evolving skies umbreon") == "evolving skies umbreon"
    assert parse("Umbreon VMAX") == "umbreon vmax"


def test_snapping_is_off_when_nothing_resembles_the_words():
    # Not a snap: "mew" alone is too far from "Mewtwo" to be the same word.
    assert parse("mew") == "mew"


def test_snap_would_be_lost_without_the_threshold():
    """A regression guard on the *mechanism*: with the threshold at 1.0 only an
    exact match may snap, so the misheard name comes back verbatim."""
    original = voice.SNAP_THRESHOLD
    try:
        voice.SNAP_THRESHOLD = 1.0
        assert parse("pikachoo") == "pikachoo"
    finally:
        voice.SNAP_THRESHOLD = original
    assert parse("pikachoo") == "pikachu"


# -- what survives ------------------------------------------------------------------


def test_filler_is_dropped_but_unknown_words_are_kept():
    assert parse("um, show me Pikachu please") == "pikachu"
    # A card the user does not own filters to an honest empty grid.
    assert parse("Gengar") == "gengar"
    assert parse("blah blah") == "blah blah"


def test_empty_and_silent_transcripts_yield_nothing():
    assert parse("") == ""
    assert parse(" ... ") == ""


# -- model location -------------------------------------------------------------------


def test_voice_model_needs_every_file(tmp_path, monkeypatch):
    # Point the bundled lookup at an empty package dir so only the data dir counts.
    monkeypatch.setattr(voice.resources, "files", lambda _pkg: tmp_path / "pkg")
    models = tmp_path / "voice" / "models"
    models.mkdir(parents=True)
    assert voice.voice_model(tmp_path) is None
    (models / voice.ENCODER_FILENAME).write_bytes(b"x")
    (models / voice.DECODER_FILENAME).write_bytes(b"x")
    assert voice.voice_model(tmp_path) is None  # two of three is not a model
    (models / voice.TOKENS_FILENAME).write_text("a 0\n")
    found = voice.voice_model(tmp_path)
    assert found is not None
    assert found.tokens == models / voice.TOKENS_FILENAME


def test_transcribe_of_silence_is_empty_without_touching_the_model(monkeypatch):
    calls = []
    monkeypatch.setattr(voice, "_load_recognizer", lambda: calls.append(1))
    assert voice.transcribe(b"") == ""
    assert calls == []
