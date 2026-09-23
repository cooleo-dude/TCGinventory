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


def test_snap_would_be_lost_without_the_threshold(monkeypatch):
    """A regression guard on the *mechanism*: with the spelling threshold at 1.0
    and the sound channel off, only an exact match may snap, so the misheard
    name comes back verbatim. Either channel alone recovers it."""
    monkeypatch.setattr(voice, "SNAP_THRESHOLD", 1.0)
    monkeypatch.setattr(voice, "PHONETIC_SNAP", False)
    assert parse("pikachoo") == "pikachoo"
    monkeypatch.setattr(voice, "PHONETIC_SNAP", True)
    assert parse("pikachoo") == "pikachu"  # by sound: PKX == PKX
    monkeypatch.setattr(voice, "PHONETIC_SNAP", False)
    monkeypatch.setattr(voice, "SNAP_THRESHOLD", 0.8)
    assert parse("pikachoo") == "pikachu"  # by spelling: ratio 0.8


# -- snapping by sound (ticket 77) -----------------------------------------------------


@pytest.mark.parametrize(
    "a, b",
    [
        ("Celebi", "Celiby"),
        ("Celebi", "sell a bee"),
        ("Abomasnow", "Obama's no"),
        ("Cheren", "Sharon"),
        ("Chansey", "Chancy"),
        ("Banette", "Bennett"),
        ("Pikachu", "Pikachoo"),
        ("Centiskorch", "Santa Scorch"),
    ],
)
def test_phonetic_key_hears_past_whisper_spelling(a, b):
    assert voice.phonetic_key(a) == voice.phonetic_key(b)


@pytest.mark.parametrize("a, b", [("Mew", "Mewtwo"), ("Base Set", "Base Set 2")])
def test_phonetic_key_keeps_different_names_apart(a, b):
    assert voice.phonetic_key(a) != voice.phonetic_key(b)


def test_misheard_names_snap_by_sound_across_word_boundaries():
    names = NAMES + ["Celebi", "Abomasnow", "Absol", "Centiskorch", "Cheren"]
    assert voice.parse_query("Celiby Triumphant", names, ["Triumphant"]) == (
        "celebi triumphant"
    )
    assert voice.parse_query("sell a bee", names, SETS) == "celebi"
    assert voice.parse_query("Obama's no.", names, SETS) == "abomasnow"
    assert voice.parse_query("Santa Scorch", names, SETS) == "centiskorch"
    assert voice.parse_query("of soul", names, SETS) == "absol"  # one key edit away


def test_sound_never_outranks_a_name_spelled_right():
    # "Mew" is exact; "Mewtwo" merely contains it. The exact spelling wins.
    assert parse("Mew") == "mew"
    assert parse("Mewtwo") == "mewtwo"


def test_short_words_and_filler_never_snap_to_a_card():
    sets = SETS + ["Neo Genesis"]
    names = NAMES + ["Entei", "Mr. Mime"]
    # "no" is two letters: never "Neo". "and" is filler: never "Entei".
    assert voice.parse_query("no", names, sets) == "no"
    assert voice.parse_query("Pikachu and Charizard", names, sets) == (
        "pikachu charizard"
    )
    # "Mr." is too short to stand as a prefix, so "more" stays "more".
    assert voice.parse_query("more", names, sets) == "more"


def test_spoken_suffixes_fold_to_the_catalogs_tokens():
    names = ["Charizard ex", "Charizard GX", "Charizard VMAX", "Charizard δ"]
    assert voice.parse_query("Charizard X", names) == "charizard ex"
    assert voice.parse_query("Charizard E X", names) == "charizard ex"
    assert voice.parse_query("Charizard V Max", names) == "charizard vmax"
    assert voice.parse_query("Charizard Delta", names) == "charizard δ"
    assert voice.parse_query("Charizard G X", names) == "charizard gx"


# -- what survives ------------------------------------------------------------------


def test_filler_is_dropped_but_unknown_words_are_kept():
    assert parse("um, show me Pikachu please") == "pikachu"
    # A card the user does not own filters to an honest empty grid.
    assert parse("Gengar") == "gengar"
    assert parse("blah blah") == "blah blah"


def test_empty_and_silent_transcripts_yield_nothing():
    assert parse("") == ""
    assert parse(" ... ") == ""


# -- recognize: constrained first, snap as the fallback (ticket 77) -------------------


def test_recognize_prefers_the_constrained_decoders_answer(monkeypatch):
    calls = []
    monkeypatch.setattr(
        voice,
        "_recognize_constrained",
        lambda pcm, rate, names, sets, numbers: (
            calls.append(names) or ["Celebi", "Triumphant"]
        ),
    )
    monkeypatch.setattr(voice, "transcribe", lambda *a: pytest.fail("free-form ran"))
    assert voice.recognize(b"\x00\x01" * 800, 16000, ["Celebi"], ["Triumphant"]) == (
        "celebi triumphant"
    )
    assert calls == [["Celebi"]]


def test_recognize_falls_back_to_snapping_when_the_grammar_hears_nothing(monkeypatch):
    monkeypatch.setattr(voice, "_recognize_constrained", lambda *a: [])
    monkeypatch.setattr(
        voice, "transcribe", lambda pcm, rate=16000: "Celiby Triumphant"
    )
    assert voice.recognize(b"\x00\x01" * 800, 16000, ["Celebi"], ["Triumphant"]) == (
        "celebi triumphant"
    )


def test_recognize_without_a_vocabulary_is_free_form(monkeypatch):
    monkeypatch.setattr(
        voice, "_recognize_constrained", lambda *a: pytest.fail("no grammar to use")
    )
    monkeypatch.setattr(
        voice, "transcribe", lambda pcm, rate=16000: "Pikachu 25 of 102"
    )
    assert voice.recognize(b"\x00\x01" * 800) == "pikachu 25/102"
    assert voice.recognize(b"") == ""


def test_constrained_answer_is_refused_when_the_grammar_forced_it(monkeypatch):
    """A clip that says something outside the vocabulary still decodes to *some*
    card under the grammar; the likelihood gap against the free transcript is
    what tells that case from a real name, and past REJECT_GAP the answer is
    dropped so the fallback runs instead of naming a wrong card."""
    from tcginventory.services import voice_decode

    class FakeDecoder:
        def __init__(self, gap):
            self.gap = gap

        def decode_scored(self, audio, trie):
            return voice_decode.Decoded(["Charizard"], -10.0, -10.0 + self.gap)

    monkeypatch.setattr(voice, "_grammar", lambda *a: object())
    monkeypatch.setattr(voice, "_decoder", FakeDecoder(voice.REJECT_GAP + 1))
    assert (
        voice._recognize_constrained(b"\x00\x01" * 800, 16000, ["Charizard"], [], [])
        == []
    )
    monkeypatch.setattr(voice, "_decoder", FakeDecoder(voice.REJECT_GAP - 1))
    assert voice._recognize_constrained(
        b"\x00\x01" * 800, 16000, ["Charizard"], [], []
    ) == ["Charizard"]


def test_grammar_is_rebuilt_only_when_the_vocabulary_changes(monkeypatch):
    from tcginventory.services import voice_decode

    builds = []
    monkeypatch.setattr(
        voice_decode, "build_grammar", lambda *a, **k: builds.append(a) or object()
    )
    monkeypatch.setattr(voice, "_grammar_cache", None)
    first = voice._grammar(["Celebi"], ["Triumphant"], [])
    assert voice._grammar(["Celebi"], ["Triumphant"], []) is first
    voice._grammar(["Celebi", "Mew"], ["Triumphant"], [])
    assert len(builds) == 2


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
