"""Music analysis (tempo, first downbeat, meter, key, sections) for finished fast passes.

Everything here is best effort: every field of the result is optional and a
failure of any step only leaves its fields out. `analyze_job` never raises.

Default backend: librosa (ISC licence, no trained model files) plus small
heuristics. Optional backend: All-In-One (`ANALYSIS_BACKEND=allin1`), see README.
"""
from __future__ import annotations

import logging
import math
import os
import re
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("demucs-api.analysis")

ANALYSIS_ENABLED = os.getenv("ANALYSIS_ENABLED", "true").lower() == "true"
ANALYSIS_BACKEND = os.getenv("ANALYSIS_BACKEND", "librosa").lower()
ANALYSIS_TIMEOUT = float(os.getenv("ANALYSIS_TIMEOUT_SECONDS", "60"))

BPM_RANGE = (20.0, 400.0)
FIRST_BEAT_RANGE = (0.0, 600.0)
DENOMINATORS = {2, 4, 8, 16}
SECTION_LABELS = {
    "intro", "verse", "pre-chorus", "chorus", "bridge", "inst", "instrumental",
    "solo", "break", "interlude", "outro", "tag",
}
KEY_RE = re.compile(r"^[A-G][#b]?m?$")

SHARP_TO_FLAT_TONIC = ["C", "Db", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
MINOR_TONIC = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "G#", "A", "Bb", "B"]


# --------------------------------------------------------------------------- #
# Output normalisation: the contract Songverse relies on lives here.
# --------------------------------------------------------------------------- #

def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _confidence(value: Any) -> float | None:
    v = _num(value)
    return None if v is None else round(min(1.0, max(0.0, v)), 3)


def _with_conf(out: dict, raw: dict) -> dict:
    conf = _confidence(raw.get("confidence"))
    if conf is not None:
        out["confidence"] = conf
    return out


def sanitize_analysis(raw: Any) -> dict | None:
    """Return a contract-conformant analysis dict, or None when nothing valid is left."""
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}

    tempo = raw.get("tempo")
    if isinstance(tempo, dict):
        bpm = _num(tempo.get("bpm"))
        if bpm is not None and BPM_RANGE[0] <= bpm <= BPM_RANGE[1]:
            out["tempo"] = _with_conf({"bpm": round(bpm, 1)}, tempo)

    first_beat = _num(raw.get("first_beat"))
    if first_beat is not None and FIRST_BEAT_RANGE[0] <= first_beat <= FIRST_BEAT_RANGE[1]:
        out["first_beat"] = round(first_beat, 3)

    ts = raw.get("time_signature")
    if isinstance(ts, dict):
        n, d = ts.get("numerator"), ts.get("denominator")
        if (isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= 16
                and isinstance(d, int) and not isinstance(d, bool) and d in DENOMINATORS):
            out["time_signature"] = _with_conf({"numerator": n, "denominator": d}, ts)

    key = raw.get("key")
    if isinstance(key, dict):
        name = key.get("name")
        if isinstance(name, str) and KEY_RE.match(name):
            out["key"] = _with_conf({"name": name}, key)

    sections = raw.get("sections")
    if isinstance(sections, list):
        clean = []
        for s in sections:
            if not isinstance(s, dict):
                continue
            start = _num(s.get("start"))
            label = s.get("label")
            if start is None or start < 0 or not isinstance(label, str):
                continue
            label = label.strip().lower()
            if label in SECTION_LABELS:
                clean.append({"start": round(start, 3), "label": label})
        clean.sort(key=lambda s: s["start"])
        if clean:
            out["sections"] = clean

    return out or None


def key_name(tonic_pc: int, minor: bool) -> str:
    return (MINOR_TONIC[tonic_pc] + "m") if minor else SHARP_TO_FLAT_TONIC[tonic_pc]


# --------------------------------------------------------------------------- #
# Audio helpers
# --------------------------------------------------------------------------- #

class _Budget:
    def __init__(self, seconds: float):
        self.deadline = time.monotonic() + seconds

    def ok(self) -> bool:
        return time.monotonic() < self.deadline


def _find_stem(files: list[Path], name: str) -> Path | None:
    for p in files:
        if p.stem == name:
            return p
    return None


def _load(path: Path, sr: int):
    import librosa
    y, _ = librosa.load(str(path), sr=sr, mono=True)
    return y


def _mix(paths: list[Path], sr: int):
    import numpy as np
    ys = [_load(p, sr) for p in paths]
    n = min(len(y) for y in ys)
    return np.sum([y[:n] for y in ys], axis=0)


# --------------------------------------------------------------------------- #
# librosa backend
# --------------------------------------------------------------------------- #

def _rhythm(y_rhythm, sr: int) -> dict:
    """Tempo, first downbeat and meter from drums(+bass) audio."""
    import librosa
    import numpy as np

    hop = 512
    env = librosa.onset.onset_strength(y=y_rhythm, sr=sr, hop_length=hop)
    if env.size < 8 or not np.any(env > 0):
        return {}
    tempo, beat_frames = librosa.beat.beat_track(onset_envelope=env, sr=sr, hop_length=hop)
    bpm = float(np.atleast_1d(tempo)[0])
    beat_frames = np.asarray(beat_frames, dtype=int)
    result: dict[str, Any] = {}
    if bpm <= 0 or len(beat_frames) < 4:
        return result

    # Confidence: how periodic the onset envelope is at the beat period.
    lag = max(1, int(round(60.0 / bpm * sr / hop)))
    ac = librosa.autocorrelate(env - env.mean(), max_size=lag * 2 + 2)
    if ac[0] > 0:
        lo, hi = max(1, lag - 2), min(len(ac) - 1, lag + 2)
        periodic = float(np.max(ac[lo:hi + 1]) / ac[0])
        result["tempo"] = {"bpm": bpm, "confidence": min(1.0, max(0.0, periodic * 1.6))}
    else:
        result["tempo"] = {"bpm": bpm}

    # Meter / downbeat phase from low-frequency (kick/bass) accents per beat.
    low = librosa.onset.onset_strength(y=y_rhythm, sr=sr, hop_length=hop, fmax=300)
    n_frames = min(len(low), len(env))
    idx = beat_frames[beat_frames < n_frames]
    strength = low[idx] + 0.5 * env[idx]
    if len(strength) < 8:
        return result
    strength = (strength - strength.mean()) / (strength.std() + 1e-9)

    scores: dict[int, tuple[float, int]] = {}
    for m in (3, 4):
        phase_means = [float(strength[p::m].mean()) for p in range(m)]
        best = int(np.argmax(phase_means))
        others = [v for i, v in enumerate(phase_means) if i != best]
        scores[m] = (phase_means[best] - float(np.mean(others)), best)
    # Prior toward 4/4: only call 3/4 when its accent pattern is clearly stronger.
    meter = 3 if scores[3][0] > scores[4][0] * 1.35 + 0.1 else 4
    contrast, phase = scores[meter]
    meter_conf = min(1.0, max(0.0, contrast / 1.5))
    result["time_signature"] = {"numerator": meter, "denominator": 4, "confidence": meter_conf}

    times = librosa.frames_to_time(idx, sr=sr, hop_length=hop)
    if phase < len(times):
        result["first_beat"] = float(times[phase])
    result["_downbeat_period"] = 60.0 / bpm * meter
    result["_first_beat_raw"] = result.get("first_beat")
    return result


_KS_MAJOR = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
_KS_MINOR = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]


def _key(y_harm, sr: int) -> dict:
    """Krumhansl-Schmuckler key estimate on a chroma average."""
    import librosa
    import numpy as np

    tuning = librosa.estimate_tuning(y=y_harm, sr=sr)
    chroma = librosa.feature.chroma_stft(y=y_harm, sr=sr, n_fft=4096, hop_length=2048, tuning=tuning)
    profile = chroma.mean(axis=1)
    if not np.any(profile > 0):
        return {}
    candidates = []
    for minor, ref in ((False, _KS_MAJOR), (True, _KS_MINOR)):
        ref = np.asarray(ref)
        for tonic in range(12):
            r = float(np.corrcoef(profile, np.roll(ref, tonic))[0, 1])
            candidates.append((r, tonic, minor))
    candidates.sort(reverse=True)
    (r1, tonic, minor), (r2, _, _) = candidates[0], candidates[1]
    # Margin to the runner-up (often the relative major/minor) plus absolute fit.
    conf = 0.6 * min(1.0, (r1 - r2) / 0.08) + 0.4 * min(1.0, max(0.0, (r1 - 0.4) / 0.4))
    return {"name": key_name(tonic, minor), "confidence": min(1.0, max(0.0, conf))}


def _sections(y_mix, y_vocals, sr: int, first_downbeat: float | None, bar_len: float | None) -> list[dict]:
    """Heuristic structure: segment on beat-less chroma+MFCC, cluster, label by energy/repetition."""
    import librosa
    import numpy as np

    hop = 2048
    duration = len(y_mix) / sr
    if duration < 30:
        return []
    chroma = librosa.feature.chroma_stft(y=y_mix, sr=sr, n_fft=4096, hop_length=hop)
    mfcc = librosa.feature.mfcc(y=y_mix, sr=sr, n_mfcc=13, hop_length=hop)
    rms = librosa.feature.rms(y=y_mix, hop_length=hop)[0]
    # Smooth over ~1.5s so boundaries follow sections rather than notes.
    win = max(1, int(1.5 * sr / hop))
    kernel = np.ones(win) / win

    def smooth(m):
        return np.apply_along_axis(lambda r: np.convolve(r, kernel, mode="same"), 1, m)

    def z(m):
        return (m - m.mean(axis=1, keepdims=True)) / (m.std(axis=1, keepdims=True) + 1e-9)

    feats = np.vstack([z(smooth(chroma)), z(smooth(mfcc))])
    n = feats.shape[1]
    k = int(min(12, max(3, round(duration / 22))))
    k = min(k, n // 4)
    if k < 2:
        return []
    bounds = librosa.segment.agglomerative(feats, k)
    bounds = sorted({int(b) for b in bounds} | {0})
    starts = librosa.frames_to_time(bounds, sr=sr, hop_length=hop)

    # Snap boundaries to the bar grid when we have one.
    if first_downbeat is not None and bar_len and bar_len > 0.5:
        starts = np.array([first_downbeat + round((t - first_downbeat) / bar_len) * bar_len for t in starts])
        starts = np.clip(starts, 0.0, duration - 1.0)
    starts[0] = 0.0
    keep = [0]
    for i in range(1, len(starts)):
        if starts[i] - starts[keep[-1]] >= 4.0:
            keep.append(i)
    starts = starts[keep]
    ends = np.append(starts[1:], duration)

    seg_vec, seg_rms, seg_voc = [], [], []
    voc_rms = librosa.feature.rms(y=y_vocals, hop_length=hop)[0] if y_vocals is not None else None
    for s, e in zip(starts, ends):
        a, b = int(s * sr / hop), max(int(s * sr / hop) + 1, int(e * sr / hop))
        seg_vec.append(feats[:, a:b].mean(axis=1))
        seg_rms.append(float(rms[a:b].mean()))
        if voc_rms is not None:
            seg_voc.append(float(voc_rms[a:b].mean()))
    seg_vec = np.asarray(seg_vec)
    m = len(seg_vec)

    # Group similar segments (cosine similarity) greedily.
    norm = seg_vec / (np.linalg.norm(seg_vec, axis=1, keepdims=True) + 1e-9)
    group = [-1] * m
    g = 0
    for i in range(m):
        if group[i] >= 0:
            continue
        group[i] = g
        for j in range(i + 1, m):
            if group[j] < 0 and float(norm[i] @ norm[j]) > 0.6:
                group[j] = g
        g += 1
    counts = {gi: group.count(gi) for gi in set(group)}
    mean_rms = {gi: float(np.mean([seg_rms[i] for i in range(m) if group[i] == gi])) for gi in counts}
    repeated = [gi for gi, c in counts.items() if c >= 2]
    chorus_g = max(repeated, key=lambda gi: (mean_rms[gi], counts[gi])) if repeated else None
    verse_candidates = [gi for gi in repeated if gi != chorus_g]
    verse_g = max(verse_candidates, key=lambda gi: (counts[gi], -mean_rms[gi])) if verse_candidates else None

    voc_floor = (0.25 * max(seg_voc)) if seg_voc else 0.0
    labels = []
    for i in range(m):
        gi = group[i]
        if i == 0 and (m > 2) and seg_rms[0] < 0.8 * np.mean(seg_rms) and gi not in (chorus_g,):
            label = "intro"
        elif i == m - 1 and m > 2 and (counts[gi] == 1 or seg_rms[i] < 0.8 * np.mean(seg_rms)):
            label = "outro"
        elif gi == chorus_g:
            label = "chorus"
        elif gi == verse_g:
            label = "verse"
        elif counts[gi] == 1:
            label = "bridge" if 0 < i < m - 1 else "verse"
        else:
            label = "verse"
        if seg_voc and seg_voc[i] < voc_floor and label in {"verse", "bridge"}:
            label = "inst"
        labels.append(label)

    out, prev = [], None
    for s, lab in zip(starts, labels):
        if lab == prev:
            continue
        out.append({"start": float(s), "label": lab})
        prev = lab
    return out


def _analyze_librosa(files: list[Path], source: Path | None, budget: _Budget) -> dict:
    sr = 22050
    drums, bass = _find_stem(files, "drums"), _find_stem(files, "bass")
    other, vocals = _find_stem(files, "other"), _find_stem(files, "vocals")
    no_vocals = _find_stem(files, "no_vocals")
    result: dict[str, Any] = {}
    mix_cache: list = []

    def full_mix():
        if not mix_cache:
            stems = [p for p in (drums, bass, other, vocals) if p] if sum(map(bool, (drums, bass, other, vocals))) >= 3 \
                else ([no_vocals, vocals] if no_vocals and vocals else None)
            if stems:
                mix_cache.append(_mix([p for p in stems if p], sr))
            elif source:
                mix_cache.append(_load(source, sr))
            else:
                raise RuntimeError("no audio available")
        return mix_cache[0]

    rhythm: dict = {}
    try:
        rp = [p for p in (drums, bass) if p]
        y = _mix(rp, sr) if rp else full_mix()
        rhythm = _rhythm(y, sr)
        for k in ("tempo", "time_signature", "first_beat"):
            if k in rhythm:
                result[k] = rhythm[k]
    except Exception:
        log.exception("analysis: rhythm step failed")

    if budget.ok():
        try:
            parts = [p for p in (bass, other) if p] or ([no_vocals] if no_vocals else [])
            y = _mix(parts, sr) if parts else full_mix()
            k = _key(y, sr)
            if k:
                result["key"] = k
        except Exception:
            log.exception("analysis: key step failed")
    else:
        log.warning("analysis: time budget exhausted before key")

    if budget.ok():
        try:
            y_voc = _load(vocals, sr) if vocals else None
            secs = _sections(full_mix(), y_voc, sr, rhythm.get("first_beat"), rhythm.get("_downbeat_period"))
            if secs:
                result["sections"] = secs
        except Exception:
            log.exception("analysis: sections step failed")
    else:
        log.warning("analysis: time budget exhausted before sections")
    return result


# --------------------------------------------------------------------------- #
# Optional All-In-One backend (opt-in; see README for licence caveats)
# --------------------------------------------------------------------------- #

_AIO_LABELS = {"start": None, "end": None, "intro": "intro", "verse": "verse", "chorus": "chorus",
               "bridge": "bridge", "inst": "inst", "solo": "solo", "break": "break", "outro": "outro"}


def _analyze_allin1(files: list[Path], source: Path | None, work_dir: Path, device: str, budget: _Budget) -> dict:
    import shutil

    import allin1  # type: ignore  # optional dependency

    stems = {p.stem: p for p in files if p.stem in {"bass", "drums", "other", "vocals"}}
    if source is None:
        raise RuntimeError("no source file")
    # All-In-One reuses Demucs stems when they sit in <demix_dir>/htdemucs/<track>/<stem>.wav.
    demix = work_dir / "allin1-demix"
    track_dir = demix / "htdemucs" / source.stem
    if len(stems) == 4 and all(p.suffix == ".wav" for p in stems.values()):
        track_dir.mkdir(parents=True, exist_ok=True)
        for name, p in stems.items():
            shutil.copy2(p, track_dir / f"{name}.wav")
    res = allin1.analyze(
        str(source), device=device if device in {"cpu", "cuda"} else "cpu",
        demix_dir=str(demix), spec_dir=str(work_dir / "allin1-spec"),
        out_dir=None, keep_byproducts=False,
    )
    res = res[0] if isinstance(res, list) else res
    out: dict[str, Any] = {}
    if res.bpm:
        out["tempo"] = {"bpm": float(res.bpm)}
    downbeats = list(res.downbeats or [])
    if downbeats:
        out["first_beat"] = float(downbeats[0])
    positions = list(res.beat_positions or [])
    if positions:
        out["time_signature"] = {"numerator": int(max(positions)), "denominator": 4}
    secs = []
    for seg in res.segments or []:
        label = _AIO_LABELS.get(str(seg.label).lower())
        if label:
            secs.append({"start": float(seg.start), "label": label})
    if secs:
        secs[0]["start"] = 0.0
        out["sections"] = secs
    shutil.rmtree(work_dir / "allin1-demix", ignore_errors=True)
    shutil.rmtree(work_dir / "allin1-spec", ignore_errors=True)
    return out


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def analyze_job(files: list[Path], source: Path | None, work_dir: Path, device: str = "cpu") -> dict | None:
    """Analyse a job after its fast pass. Never raises; returns a sanitised dict or None."""
    if not ANALYSIS_ENABLED:
        return None
    budget = _Budget(ANALYSIS_TIMEOUT)
    started = time.monotonic()
    raw: dict[str, Any] = {}
    try:
        if ANALYSIS_BACKEND == "allin1":
            try:
                raw = _analyze_allin1(files, source, work_dir, device, budget)
            except Exception:
                log.exception("analysis: allin1 backend failed; falling back to librosa")
        fallback = _analyze_librosa(files, source, budget)
        # Keep All-In-One's values where it had them; fill the rest from librosa.
        for k, v in fallback.items():
            if k not in raw:
                raw[k] = v
    except Exception:
        log.exception("analysis failed")
    result = sanitize_analysis(raw)
    log.info("analysis done in %.1fs: fields=%s", time.monotonic() - started, sorted(result or {}))
    return result
