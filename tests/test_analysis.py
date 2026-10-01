import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import analysis  # noqa: E402

KEY_RE = re.compile(r"^[A-G][#b]?m?$")


def check_contract(a, duration=None):
    assert a is None or isinstance(a, dict)
    if not a:
        return
    assert set(a) <= {"tempo", "first_beat", "time_signature", "key", "sections",
                      "beats", "downbeats", "intro_free", "beats_confidence"}
    for k in ("tempo", "time_signature", "key"):
        if k in a and "confidence" in a[k]:
            assert 0 <= a[k]["confidence"] <= 1
    if "tempo" in a:
        assert 20 <= a["tempo"]["bpm"] <= 400
    if "first_beat" in a:
        assert 0 <= a["first_beat"] <= 600
    if "time_signature" in a:
        ts = a["time_signature"]
        assert 1 <= ts["numerator"] <= 16 and ts["denominator"] in (2, 4, 8, 16)
    if "key" in a:
        assert KEY_RE.match(a["key"]["name"])
    if "sections" in a:
        starts = [s["start"] for s in a["sections"]]
        assert starts == sorted(starts)
        for s in a["sections"]:
            assert s["label"] in analysis.SECTION_LABELS and s["label"] == s["label"].lower()
    # beats / downbeats: all or nothing, sorted, in range, consistent with first_beat
    assert ("beats" in a) == ("downbeats" in a)
    if "beats" in a:
        beats, downs = a["beats"], a["downbeats"]
        assert beats and downs
        assert all(b2 > b1 for b1, b2 in zip(beats, beats[1:]))
        assert all(d2 > d1 for d1, d2 in zip(downs, downs[1:]))
        assert beats[0] >= 0 and (duration is None or beats[-1] <= duration)
        assert set(downs) <= set(beats)
        assert a["first_beat"] == downs[0]
        assert all(round(t, 3) == t for t in beats)
    else:
        assert "beats_confidence" not in a
    if "beats_confidence" in a:
        assert 0 <= a["beats_confidence"] <= 1
    if "intro_free" in a:
        assert isinstance(a["intro_free"], bool)
    json.dumps(a)


def test_valid_block_passes_through():
    raw = {
        "tempo": {"bpm": 72.1, "confidence": 0.9},
        "first_beat": 0.42,
        "time_signature": {"numerator": 4, "denominator": 4, "confidence": 0.8},
        "key": {"name": "G", "confidence": 0.7},
        "sections": [{"start": 0.0, "label": "intro"}, {"start": 8.3, "label": "verse"},
                     {"start": 40.1, "label": "chorus"}],
    }
    assert analysis.sanitize_analysis(raw) == raw


def test_beats_and_downbeats_round_and_stay_consistent():
    out = analysis.sanitize_analysis({
        "first_beat": 9.9,
        "beats": [0.42, 1.25, 2.0834, 2.91, 3.74], "downbeats": [0.42, 3.74],
        "beats_confidence": 0.8, "intro_free": False,
    })
    assert out["beats"] == [0.42, 1.25, 2.083, 2.91, 3.74]
    assert out["downbeats"] == [0.42, 3.74]
    assert out["first_beat"] == 0.42          # follows the first downbeat
    assert out["beats_confidence"] == 0.8 and out["intro_free"] is False
    check_contract(out)


def test_invalid_beat_lists_are_dropped_whole():
    ok_beats = [0.5, 1.0, 1.5, 2.0]
    for bad in (
        {"beats": [1.0, 0.5], "downbeats": [1.0]},                  # not increasing
        {"beats": [1.0, 1.0, 2.0], "downbeats": [1.0]},             # duplicates
        {"beats": [-0.1, 1.0], "downbeats": [1.0]},                 # negative
        {"beats": ok_beats, "downbeats": [0.5, 9.0]},               # downbeat not a beat
        {"beats": [], "downbeats": []},                             # empty
        {"beats": ok_beats},                                        # downbeats missing
        {"downbeats": [0.5]},                                       # beats missing
        {"beats": [1.0, "x"], "downbeats": [1.0]},
        {"beats": [1.0, float("inf")], "downbeats": [1.0]},
    ):
        out = analysis.sanitize_analysis({**bad, "tempo": {"bpm": 100}, "beats_confidence": 0.9})
        assert out == {"tempo": {"bpm": 100.0}}, bad
    assert analysis.sanitize_analysis({"beats": ok_beats, "downbeats": [0.5]}, duration=1.9) is None
    assert analysis.sanitize_analysis({"beats": ok_beats, "downbeats": [0.5]}, duration=2.0) is not None


def test_intro_free_only_accepts_booleans():
    assert analysis.sanitize_analysis({"intro_free": True}) == {"intro_free": True}
    assert analysis.sanitize_analysis({"intro_free": "yes"}) is None
    assert analysis.sanitize_analysis({"intro_free": 1}) is None


def test_beats_confidence_needs_beats():
    assert analysis.sanitize_analysis({"beats_confidence": 0.9}) is None


def test_every_field_optional():
    assert analysis.sanitize_analysis({}) is None
    assert analysis.sanitize_analysis(None) is None
    assert analysis.sanitize_analysis({"key": {"name": "Bb"}}) == {"key": {"name": "Bb"}}
    assert analysis.sanitize_analysis({"tempo": {"bpm": 100}}) == {"tempo": {"bpm": 100.0}}


def test_out_of_range_values_dropped():
    raw = {
        "tempo": {"bpm": 5}, "first_beat": 601,
        "time_signature": {"numerator": 17, "denominator": 4},
        "key": {"name": "G major"}, "sections": [{"start": 1, "label": "weird"}],
    }
    assert analysis.sanitize_analysis(raw) is None
    assert analysis.sanitize_analysis({"time_signature": {"numerator": 4, "denominator": 3}}) is None
    assert analysis.sanitize_analysis({"tempo": {"bpm": float("nan")}}) is None
    assert analysis.sanitize_analysis({"first_beat": -0.1}) is None


def test_confidence_clamped_and_sections_sorted_lowercased():
    out = analysis.sanitize_analysis({
        "tempo": {"bpm": 120, "confidence": 1.7},
        "sections": [{"start": 9, "label": "Chorus"}, {"start": 1, "label": "INTRO"}, {"start": 3, "label": "x"}],
    })
    assert out["tempo"]["confidence"] == 1.0
    assert out["sections"] == [{"start": 1.0, "label": "intro"}, {"start": 9.0, "label": "chorus"}]
    check_contract(out)


@pytest.mark.parametrize("name", ["G", "Bb", "F#m", "Ebm", "A", "C#m"])
def test_valid_key_names(name):
    assert analysis.sanitize_analysis({"key": {"name": name}}) == {"key": {"name": name}}


@pytest.mark.parametrize("name", ["G major", "H", "g", "Gmaj", "", "Bbb", 5])
def test_invalid_key_names(name):
    assert analysis.sanitize_analysis({"key": {"name": name}}) is None


def test_key_name_always_parses():
    for pc in range(12):
        for minor in (False, True):
            assert KEY_RE.match(analysis.key_name(pc, minor))


def test_disabled_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(analysis, "ANALYSIS_ENABLED", False)
    assert analysis.analyze_job([], None, tmp_path) is None


def test_failure_never_raises(tmp_path):
    bad = tmp_path / "drums.wav"
    bad.write_bytes(b"not audio")
    assert analysis.analyze_job([bad], None, tmp_path) is None


def _song(tmp_path, **kw):
    pytest.importorskip("librosa")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import synth
    first, bar = synth.make_song(tmp_path, **kw)
    return first, bar, analysis.analyze_job(sorted(tmp_path.glob("*.wav")), None, tmp_path)


def test_end_to_end_on_synthetic_audio(tmp_path):
    _, _, out = _song(tmp_path, bpm=100, intro_bars=0, bars=16)
    check_contract(out)
    assert abs(out["tempo"]["bpm"] - 100) < 4
    assert out["key"]["name"] in ("C", "Am", "F", "G")
    assert out["time_signature"]["numerator"] in (3, 4)


def _check_beats(out, bpm, tmp_path):
    import soundfile as sf
    duration = sf.info(str(tmp_path / "drums.wav")).duration
    check_contract(out, duration)
    beats, downs = out["beats"], out["downbeats"]
    import statistics
    median_bpm = 60 / statistics.median(b - a for a, b in zip(beats, beats[1:]))
    assert abs(out["tempo"]["bpm"] - median_bpm) < 0.1      # tempo is the median beat interval
    assert out["first_beat"] == downs[0]
    assert len(beats) > 4 * len(downs) - 8 and len(downs) > 3  # about four beats per bar


@pytest.mark.parametrize("bpm", [80, 100, 120])
def test_beats_are_reported_and_consistent(tmp_path, bpm):
    _, _, out = _song(tmp_path, bpm=bpm, intro_bars=2, bars=12)
    _check_beats(out, bpm, tmp_path)
    steps = [b - a for a, b in zip(out["beats"], out["beats"][1:])]
    assert all(abs(x - 60 / bpm) < 0.1 * 60 / bpm for x in steps)   # beat-level, not half or double


def test_steady_pad_intro_is_not_intro_free(tmp_path):
    _, _, out = _song(tmp_path, bpm=100, intro_bars=4, bars=12)
    assert out.get("intro_free") is not True
    assert out["first_beat"] < 0.2


def test_free_intro_is_detected_and_first_beat_waits_for_the_beat(tmp_path):
    first, _, out = _song(tmp_path, bpm=100, free_intro=14.0, bars=14)
    assert out["intro_free"] is True
    assert abs(out["first_beat"] - first) < 0.15
    assert out["beats"][0] == out["first_beat"]          # nothing listed from the free intro
    _check_beats(out, 100, tmp_path)


def test_no_intro_means_not_intro_free(tmp_path):
    _, _, out = _song(tmp_path, bpm=100, intro_bars=0, bars=14)
    assert out.get("intro_free") in (False, None)


@pytest.mark.parametrize("bpm", [80, 100, 120])
def test_first_beat_is_bar_one_not_the_drum_entry(tmp_path, bpm):
    """A drumless pad intro: bar 1 starts with the music, not where the drums come in."""
    drums_in, bar, out = _song(tmp_path, bpm=bpm, intro_bars=4, bars=12, subdivide=2)
    assert drums_in > 4 * bar - 1e-6
    assert out["first_beat"] < 0.2 * 60 / bpm        # music starts at 0.0 in the fixture
    assert abs(out["tempo"]["bpm"] - bpm) < 0.04 * bpm


def test_first_beat_respects_leading_silence(tmp_path):
    _, _, out = _song(tmp_path, bpm=100, intro_bars=2, bars=12, lead=1.5)
    assert abs(out["first_beat"] - 1.5) < 0.2


def test_slow_song_is_not_reported_at_double_speed(tmp_path):
    """A 72 bpm song with busy hi-hats was reported as ~144; it must now be 72, or
    at least not claimed with confidence >= 0.5 when wrong."""
    _, _, out = _song(tmp_path, bpm=72, intro_bars=4, bars=12, subdivide=4)
    t = out["tempo"]
    assert abs(t["bpm"] - 72) < 3 or t.get("confidence", 1.0) < 0.5


@pytest.mark.parametrize("bpm", [60, 66, 140, 150, 160])
def test_tempos_outside_comfort_range_still_give_a_valid_block(tmp_path, bpm):
    """Outside ~70-130 bpm the octave is a guess (folded toward the prior); the block must still be valid."""
    _, _, out = _song(tmp_path, bpm=bpm, intro_bars=0, bars=12)
    check_contract(out)
