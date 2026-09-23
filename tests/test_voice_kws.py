"""End-word spotting (ticket 79): model location, tail trimming, and how the
end words are absorbed by recognition. No model is loaded here; the spotter's
behaviour on speech is measured by ``training/voice_bench.py --mode kws``."""

from __future__ import annotations

from tcginventory.services import voice, voice_decode, voice_kws


def test_kws_model_needs_all_five_files(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_kws.resources, "files", lambda _pkg: tmp_path / "pkg")
    models = tmp_path / "voice" / "models"
    models.mkdir(parents=True)
    assert voice_kws.kws_model(tmp_path) is None
    for name in voice_kws._FILENAMES[:-1]:
        (models / name).write_bytes(b"x")
    assert voice_kws.kws_model(tmp_path) is None  # keywords file missing
    (models / voice_kws.KEYWORDS_FILENAME).write_text("▁F IN ISH\n", encoding="utf-8")
    found = voice_kws.kws_model(tmp_path)
    assert found is not None
    assert found.keywords == models / voice_kws.KEYWORDS_FILENAME


def test_the_bundled_keyword_list_spells_every_end_word():
    """The keyword file is the model's BPE of END_WORDS, generated at dev time;
    if someone adds an end word without regenerating it, the spotter would
    never fire on it."""
    model = voice_kws.kws_model()
    assert model is not None, "spotter files are package data"
    spelled = [
        line.replace("▁", "").replace(" ", "").lower()
        for line in model.keywords.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert sorted(spelled) == sorted(voice.END_WORDS)


def test_trim_tail_drops_the_end_word_and_never_splits_a_sample():
    one_second = b"\x01\x00" * 16_000
    assert len(voice_kws.trim_tail(one_second, 600)) == (16_000 - 9_600) * 2
    assert voice_kws.trim_tail(one_second, 0) == one_second
    assert voice_kws.trim_tail(b"\x01\x00" * 10, 5_000) == b""
    assert len(voice_kws.trim_tail(b"\x01\x00\x01", 0)) % 2 == 0


def test_trim_end_word_cuts_back_from_where_speech_stops():
    """1.0 s of speech, 0.3 s of quiet (the spotter's latency), then the fire.
    The word is removed from where speech *ends*, so the variable latency never
    decides how much of the name survives."""
    import numpy as np

    rate = 16_000
    speech = (np.ones(rate, dtype="<i2") * 8000).tobytes()
    quiet = b"\x00\x00" * int(rate * 0.3)
    clip = speech + quiet
    trimmed = voice_kws.trim_end_word(clip, word_ms=600, rate=rate)
    assert len(trimmed) == int(rate * 0.4) * 2  # 1.0 s − 0.6 s, quiet gone
    # Latency does not change the result.
    assert voice_kws.trim_end_word(speech + quiet * 3, 600, rate) == trimmed
    # Nothing loud → nothing to recognise.
    assert voice_kws.trim_end_word(quiet, 600, rate) == b""
    assert voice_kws.trim_end_word(b"", 600, rate) == b""
    # A clip shorter than the word → empty, not negative.
    assert voice_kws.trim_end_word(speech[: rate // 2], 600, rate) == b""


def test_end_words_are_legal_in_the_grammar_and_add_no_token():
    trie = voice_decode.build_grammar(["Scrafty"], ["Base Set"])
    states = trie.walk(trie.start(), b" scrafty finish")
    assert ("Scrafty", "") in voice_decode.PhraseTrie.finished(states)
    states = trie.walk(trie.start(), b" scrafty done")
    assert ("Scrafty", "") in voice_decode.PhraseTrie.finished(states)


def test_recognize_drops_the_end_word_from_both_paths(monkeypatch):
    monkeypatch.setattr(voice, "_recognize_constrained", lambda *a: ["Scrafty", ""])
    assert voice.recognize(b"\x00\x01" * 800, 16000, ["Scrafty"], ["Base Set"]) == (
        "scrafty"
    )
    # And the snap fallback treats it as filler.
    assert voice.parse_query("Scrafty finish", ["Scrafty"], []) == "scrafty"
    assert voice.parse_query("Scrafty done", ["Scrafty"], []) == "scrafty"
