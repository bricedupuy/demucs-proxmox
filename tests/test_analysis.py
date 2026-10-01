import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import analysis  # noqa: E402

KEY_RE = re.compile(r"^[A-G][#b]?m?$")


def check_contract(a):
    assert a is None or isinstance(a, dict)
    if not a:
        return
    assert set(a) <= {"tempo", "first_beat", "time_signature", "key", "sections"}
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


def _click_track(path, bpm=100, bars=24, sr=22050):
    """4/4 drum-ish track: strong kick on beat 1, weaker clicks elsewhere; plus a C-major-ish chord bed."""
    np = pytest.importorskip("numpy")
    sf = pytest.importorskip("soundfile")
    beat = 60 / bpm
    n = int(bars * 4 * beat * sr)
    y = np.zeros(n)
    t = np.arange(int(0.12 * sr)) / sr
    kick = np.sin(2 * np.pi * 55 * t) * np.exp(-t * 30)
    hat = np.random.default_rng(0).standard_normal(len(t)) * np.exp(-t * 60) * 0.3
    for i in range(bars * 4):
        s = int((0.5 + i * beat) * sr)
        if s + len(t) > n:
            break
        y[s:s + len(t)] += (kick if i % 4 == 0 else hat * 0.8 + kick * 0.25)
    sf.write(path, y.astype("float32"), sr)
    tt = np.arange(n) / sr
    chord = sum(np.sin(2 * np.pi * f * tt) for f in (261.63, 329.63, 392.0, 130.81)) * 0.1
    sf.write(path.with_name("other.wav"), chord.astype("float32"), sr)


def test_end_to_end_on_synthetic_audio(tmp_path):
    pytest.importorskip("librosa")
    drums = tmp_path / "drums.wav"
    _click_track(drums)
    files = [drums, tmp_path / "other.wav"]
    out = analysis.analyze_job(files, None, tmp_path)
    check_contract(out)
    assert out is not None
    assert abs(out["tempo"]["bpm"] - 100) < 3 or abs(out["tempo"]["bpm"] - 200) < 6
    assert out["key"]["name"] in ("C", "Am")
    assert out["time_signature"]["numerator"] in (3, 4)
    assert out["first_beat"] < 3
