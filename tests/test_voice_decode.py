"""Constrained Whisper decoding (ticket 77): the grammar and the front end.

No model is loaded here. The decoder itself is measured by
``training/voice_bench.py`` against synthetic speech; what a unit test can pin
is that the trie admits exactly the vocabulary and that the numpy front end has
Whisper's shape and normalisation.
"""

from __future__ import annotations

import numpy as np
import pytest

from tcginventory.services import voice_decode as vd

PHRASES = ["Celebi", "Celebi V", "Base Set", "Base Set 2", "Mr. Mime"]


def walk(trie, text: str):
    """Feed ``text`` from the start state; the surviving states."""
    return trie.walk(trie.start(), text.encode("utf-8"))


def finished(states):
    """The phrase lists that could end here (EOT allowed)."""
    return sorted(vd.PhraseTrie.finished(states))


def test_trie_admits_a_phrase_and_reports_it_complete_at_eot():
    trie = vd.PhraseTrie(PHRASES)
    states = walk(trie, " Celebi")
    assert finished(states) == [("Celebi",)]


def test_trie_folds_ascii_case():
    trie = vd.PhraseTrie(PHRASES)
    assert finished(walk(trie, " celebi")) == [("Celebi",)]
    assert finished(walk(trie, " CELEBI")) == [("Celebi",)]


def test_trie_rejects_bytes_that_leave_the_vocabulary():
    trie = vd.PhraseTrie(PHRASES)
    assert walk(trie, " Celiby") == []  # the misspelling Whisper likes
    assert walk(trie, "Celebi") == []  # Whisper tokens carry a leading space
    assert walk(trie, " Celebi X") == []


def test_trie_continues_into_a_second_phrase_after_a_space():
    trie = vd.PhraseTrie(PHRASES)
    assert finished(walk(trie, " Celebi Base Set")) == [("Celebi", "Base Set")]
    # "Celebi V" is one phrase, yet "Celebi" + a new phrase starting with "V"
    # would also be legal — the walk keeps both readings alive until the bytes
    # decide, and EOT here can only mean the one-phrase reading.
    assert finished(walk(trie, " Celebi V")) == [("Celebi V",)]
    assert finished(walk(trie, " Base Set 2")) == [("Base Set 2",)]
    assert finished(walk(trie, " Base Set")) == [("Base Set",)]


def test_trie_caps_the_number_of_phrases():
    trie = vd.PhraseTrie(PHRASES, max_phrases=1)
    assert walk(trie, " Celebi Base Set") == []
    assert finished(walk(trie, " Celebi")) == [("Celebi",)]


def test_trie_keeps_punctuation_the_catalog_writes():
    trie = vd.PhraseTrie(PHRASES)
    assert finished(walk(trie, " Mr. Mime")) == [("Mr. Mime",)]
    assert walk(trie, " Mr Mime") == []


def test_trie_answers_the_canonical_phrase_for_a_spoken_alias():
    trie = vd.PhraseTrie([("V Max", "VMAX"), ("Charizard V Max", "Charizard VMAX")])
    assert finished(walk(trie, " charizard v max")) == [("Charizard VMAX",)]


@pytest.mark.parametrize(
    "phrase, spoken",
    [
        ("Charizard VMAX", "Charizard V Max"),
        ("Arceus VSTAR", "Arceus V Star"),
        ("Charizard-EX", "Charizard X"),
        ("Charizard ex", "Charizard E X"),
        ("Charizard δ", "Charizard Delta"),
        ("Arceus LV.X", "Arceus Level X"),
        ("Celebi & Venusaur GX", "Celebi and Venusaur G X"),
    ],
)
def test_spoken_variants_cover_how_suffixes_are_said(phrase, spoken):
    variants = vd.spoken_variants(phrase)
    assert variants[0] == phrase  # the catalog spelling is always heard too
    assert spoken in variants


@pytest.mark.parametrize(
    "number, spoken, canonical",
    [
        ("025/102", "25 out of 102", "25/102"),
        ("025/102", "25 of 102", "25/102"),
        ("025/102", "25/102", "25/102"),
        ("4/102", "4 slash 102", "4/102"),
        ("TG12/TG30", "TG12 out of TG30", "TG12/TG30"),
        ("151", "151", "151"),
    ],
)
def test_number_phrases_complete_the_search_token(number, spoken, canonical):
    assert (spoken, canonical) in vd.number_phrases(number)


def test_build_grammar_hears_aliases_and_numbers_and_answers_canonically():
    trie = vd.build_grammar(["Charizard VMAX"], ["Base Set"], ["004/102"])
    assert finished(walk(trie, " charizard v max base set")) == [
        ("Charizard VMAX", "Base Set")
    ]
    assert finished(walk(trie, " charizard vmax 4 out of 102")) == [
        ("Charizard VMAX", "4/102")
    ]


def test_log_mel_has_whispers_shape_and_range():
    audio = np.zeros(vd.SR * 2, dtype=np.float32)
    audio[:: vd.SR // 440] = 0.5  # a buzzing 440 Hz pulse train
    mel = vd.log_mel(audio)
    assert mel.shape == (1, vd.N_MELS, vd.N_FRAMES)
    assert mel.dtype == np.float32
    # Normalised to (log10 + 4) / 4 after a floor 8 under the peak, so the
    # whole spectrogram spans at most 2.0.
    assert mel.min() >= mel.max() - 2.0 - 1e-5
    assert mel.max() < 3.0


def test_mel_filters_cover_the_band_once():
    f = vd.mel_filters()
    assert f.shape == (vd.N_MELS, 1 + vd.N_FFT // 2)
    assert (f >= 0).all()
    assert (f.sum(axis=1) > 0).all()  # every band hears something


@pytest.mark.parametrize("rate, seconds", [(22050, 1.0), (8000, 0.5), (16000, 0.25)])
def test_resample_lands_on_16k(rate, seconds):
    audio = (
        np.random.default_rng(0).standard_normal(int(rate * seconds)).astype(np.float32)
    )
    out = vd.resample(audio, rate)
    assert abs(len(out) - int(vd.SR * seconds)) <= 1
    assert out.dtype == np.float32
    if rate == vd.SR:
        assert out is audio
