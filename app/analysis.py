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

TEMPO_PRIOR_CENTER = 92.0    # bpm; log-normal prior over the "counted" tempo
TEMPO_PRIOR_SIGMA = 0.5      # octaves
TEMPO_RANGE = (40.0, 220.0)


def _tempo_prior(bpm: float) -> float:
    import math
    return math.exp(-0.5 * (math.log2(bpm / TEMPO_PRIOR_CENTER) / TEMPO_PRIOR_SIGMA) ** 2)


def _norm_env(env):
    import numpy as np
    scale = np.percentile(env, 95) if env.size else 0.0
    return env / scale if scale > 0 else env


def _pick_tempo(env, sr: int, hop: int) -> tuple[float, float] | None:
    """Choose the tempo with a harmonic comb over the onset-envelope autocorrelation.

    A tempo is scored by the autocorrelation at 1, 2, 3 and 4 beat lags, so a true beat
    period (whose multiples are all accents) beats 3:2 look-alikes. Double/half-time is
    genuinely ambiguous in audio, so the choice rests on a tempo prior and the confidence reflects how contested it is.
    """
    import librosa
    import numpy as np

    fps = sr / hop
    max_lag = int(fps * 60.0 / TEMPO_RANGE[0] * 4) + 2
    if env.size < max_lag * 2:
        return None
    x = env - env.mean()
    ac = librosa.autocorrelate(x, max_size=max_lag + 1)
    if ac[0] <= 0:
        return None
    ac = ac / ac[0]
    lags = np.arange(len(ac))

    def comb(b: float) -> float:
        lag = 60.0 / b * fps
        vals = [float(np.interp(k * lag, lags, ac)) for k in (1, 2, 3, 4) if k * lag <= len(ac) - 1]
        return float(np.mean(vals)) if len(vals) == 4 else 0.0

    grid = np.arange(TEMPO_RANGE[0], TEMPO_RANGE[1], 0.5)
    scores = np.array([max(comb(b), 0.0) for b in grid])
    weighted = scores * np.array([_tempo_prior(b) for b in grid])
    if weighted.max() <= 0:
        return None
    best = float(grid[int(np.argmax(weighted))])

    def local(b: float) -> tuple[float, float]:
        """Best (score, bpm) within +-3% of b."""
        sel = (grid >= b * 0.97) & (grid <= b * 1.03)
        if not sel.any():
            return 0.0, b
        i = int(np.argmax(scores[sel]))
        return float(scores[sel][i]), float(grid[sel][i])

    s_best, best = local(best)
    lower = None
    if best / 2 >= 30.0:
        s_half, b_half = local(best / 2)
        lower = (s_half, b_half)
    higher = local(best * 2) if best * 2 <= TEMPO_RANGE[1] else None

    # Double/half time cannot be told apart from periodicity alone (all octaves score within a
    # few percent), so the prior decides. Near an octave boundary the call is a coin flip:
    # report that as a low confidence rather than a confident wrong answer.
    ratio = 0.0
    for alt in (lower, higher):
        if alt and alt[0] >= 0.75 * s_best:
            ratio = max(ratio, _tempo_prior(alt[1]) / _tempo_prior(best))
    conf = min(1.0, max(0.0, s_best / 0.35))
    if ratio >= 0.7:
        conf = min(conf, 0.45)   # coin flip between octaves: below 0.5 clients ignore it
    elif ratio >= 0.4:
        conf = min(conf, 0.6)    # prior-driven choice, plausible alternative exists
    return best, conf


def _drum_flux(y_drums, sr: int, hop: int):
    """Kick (30-150 Hz) and snare-body (150-2500 Hz) onset flux; hi-hats barely register."""
    import librosa
    import numpy as np

    S = np.abs(librosa.stft(y_drums, n_fft=2048, hop_length=hop)) ** 2
    freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)

    def flux(lo: float, hi: float):
        band = np.log1p(S[(freqs >= lo) & (freqs < hi)].sum(axis=0) * 1e3)
        return np.maximum(0.0, np.diff(band, prepend=band[0]))

    return flux(30, 150), flux(150, 2500)


def _harmonic_novelty(y, sr: int, hop: int, beat_frames):
    """z-scored chroma change across each beat (current beat vs the previous one)."""
    import librosa
    import numpy as np

    chroma = librosa.feature.chroma_stft(y=y, sr=sr, n_fft=4096, hop_length=hop)
    out = np.zeros(len(beat_frames))
    for i in range(1, len(beat_frames) - 1):
        a, b, c = beat_frames[i - 1], beat_frames[i], beat_frames[i + 1]
        if b <= a or c <= b or c > chroma.shape[1]:
            continue
        prev, cur = chroma[:, a:b].mean(axis=1), chroma[:, b:c].mean(axis=1)
        out[i] = float(np.linalg.norm(cur - prev))
    if out.std() < 1e-9:
        return None
    return (out - out.mean()) / out.std()


def _music_onset(y, sr: int) -> float:
    """Time at which the audio first becomes clearly audible (skips leading silence)."""
    import librosa
    import numpy as np
    rms = librosa.feature.rms(y=y, hop_length=512)[0]
    if rms.size == 0 or rms.max() <= 0:
        return 0.0
    idx = np.nonzero(rms > 0.03 * rms.max())[0]
    return float(librosa.frames_to_time(idx[0], sr=sr, hop_length=512)) if idx.size else 0.0


def _rhythm(y_full, y_rhythm, sr: int, y_drums=None, y_harm=None) -> dict:
    """Tempo, first downbeat and meter.

    Beats are tracked on the full mix plus the drums/bass, so a drumless intro still gets a
    beat grid; the downbeat phase comes from low-frequency accents (kick/bass), and the grid
    is extended back over the intro so `first_beat` is where bar 1 starts, not where the drums enter.
    """
    import librosa
    import numpy as np

    hop = 512
    env_full = _norm_env(librosa.onset.onset_strength(y=y_full, sr=sr, hop_length=hop))
    env_rhy = _norm_env(librosa.onset.onset_strength(y=y_rhythm, sr=sr, hop_length=hop))
    n = min(len(env_full), len(env_rhy))
    env = env_full[:n] + env_rhy[:n]
    if n < 8 or not np.any(env > 0):
        return {}
    # Hi-hats and other busy high-frequency detail make the broadband envelope look like
    # double time. The tempo is chosen on a band below 1.5 kHz (kick, snare body, bass, chords).
    env_lo = (_norm_env(librosa.onset.onset_strength(y=y_full, sr=sr, hop_length=hop, fmax=1500))[:n]
              + _norm_env(librosa.onset.onset_strength(y=y_rhythm, sr=sr, hop_length=hop, fmax=1500))[:n])

    tempo_env = env_lo
    if y_drums is not None:
        low, mid = _drum_flux(y_drums, sr, hop)
        d_env = _norm_env(low)[:n] + _norm_env(mid)[:n]
        if len(d_env) == n and np.percentile(d_env, 95) > 0 and (d_env > 0.5).mean() > 0.02:
            tempo_env = d_env  # drum accents are the cleanest counting cue when there are drums

    picked = _pick_tempo(tempo_env, sr, hop)
    if picked is None:
        return {}
    bpm, tempo_conf = picked
    result: dict[str, Any] = {"tempo": {"bpm": bpm, "confidence": tempo_conf}}

    # Track beats on drum accents plus the low band of the mix, so a drumless intro is covered too.
    beat_env = env_lo + (tempo_env if tempo_env is not env_lo else 0.0)
    _, beat_frames = librosa.beat.beat_track(onset_envelope=beat_env, sr=sr, hop_length=hop, bpm=bpm, tightness=120, trim=False)
    beat_frames = np.asarray(beat_frames, dtype=int)
    if len(beat_frames) < 4:
        return result

    # Meter / downbeat phase from low-frequency (kick/bass) accents per beat.
    low = librosa.onset.onset_strength(y=y_rhythm, sr=sr, hop_length=hop, fmax=300)
    idx = beat_frames[beat_frames < len(low)]
    strength = low[idx] + 0.5 * env_rhy[idx]
    if len(strength) < 8:
        return result
    strength = (strength - strength.mean()) / (strength.std() + 1e-9)
    # Chord changes usually fall on bar lines: add the harmonic novelty at each beat. It also
    # works in a drumless intro, where the kick cue is silent.
    novelty = _harmonic_novelty(y_harm if y_harm is not None else y_full, sr, hop, idx)
    if novelty is not None:
        strength = strength + 1.2 * novelty

    scores: dict[int, tuple[float, int]] = {}
    for m in (3, 4):
        phase_means = [float(strength[p::m].mean()) for p in range(m)]
        best = int(np.argmax(phase_means))
        others = [v for i, v in enumerate(phase_means) if i != best]
        scores[m] = (phase_means[best] - float(np.mean(others)), best)
    # Prior toward 4/4: only call 3/4 when its accent pattern is clearly stronger.
    meter = 3 if scores[3][0] > scores[4][0] * 1.35 + 0.1 else 4
    contrast, phase = scores[meter]
    result["time_signature"] = {
        "numerator": meter, "denominator": 4, "confidence": min(1.0, max(0.0, contrast / 1.5)),
    }

    times = librosa.frames_to_time(idx, sr=sr, hop_length=hop)
    beat_len = 60.0 / bpm
    bar = beat_len * meter
    if phase < len(times):
        # Median-fit the downbeat grid to all bar starts to cancel per-beat jitter, then
        # walk it back to the first bar line at or after where the music starts.
        bar_times = times[phase::meter]
        k = np.arange(len(bar_times))
        t0 = float(np.median(bar_times - k * bar))
        onset = _music_onset(y_full, sr)
        first = t0 - np.floor((t0 - (onset - 0.35 * beat_len)) / bar) * bar
        result["first_beat"] = max(0.0, float(first))
    result["_downbeat_period"] = bar
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
        harm_parts = [p for p in (bass, other) if p] or ([no_vocals] if no_vocals else [])
        rhythm = _rhythm(full_mix(), y, sr, _load(drums, sr) if drums else None,
                         _mix(harm_parts, sr) if harm_parts else None)
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
