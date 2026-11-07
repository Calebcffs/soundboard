"""Effects chains (via Spotify's pedalboard) and offline rendering/mixing."""
from __future__ import annotations

import numpy as np
from pedalboard import (Pedalboard, NoiseGate, HighpassFilter, LowShelfFilter, PeakFilter,
                        HighShelfFilter, Compressor, Gain, Reverb, Limiter)

from .project import SR, FxSettings, Project, Track

# name -> (room_size, damping, width)
REVERB_TYPES = {
    "Small room": (0.30, 0.60, 0.8),
    "Room": (0.50, 0.50, 1.0),
    "Hall": (0.80, 0.40, 1.0),
    "Plate": (0.65, 0.12, 1.0),
    "Cathedral": (0.95, 0.30, 1.0),
}


def _reverb(fx: FxSettings) -> Reverb:
    size, damp, width = REVERB_TYPES.get(fx.reverb_type, REVERB_TYPES["Room"])
    mix = float(np.clip(fx.reverb_mix, 0, 1))
    return Reverb(room_size=size, damping=damp, width=width, wet_level=mix * 0.6, dry_level=1 - mix * 0.5)


def build_track_chain(fx: FxSettings) -> Pedalboard:
    p = []
    if fx.gate_on:
        p.append(NoiseGate(threshold_db=fx.gate_threshold, ratio=8, attack_ms=1.0, release_ms=120))
    if fx.eq_on:
        if fx.low_cut > 21:
            p.append(HighpassFilter(cutoff_frequency_hz=fx.low_cut))
        if abs(fx.low_gain) > 0.05:
            p.append(LowShelfFilter(cutoff_frequency_hz=150, gain_db=fx.low_gain, q=0.707))
        if abs(fx.mid_gain) > 0.05:
            p.append(PeakFilter(cutoff_frequency_hz=fx.mid_freq, gain_db=fx.mid_gain, q=1.0))
        if abs(fx.high_gain) > 0.05:
            p.append(HighShelfFilter(cutoff_frequency_hz=5000, gain_db=fx.high_gain, q=0.707))
    if fx.comp_on:
        p.append(Compressor(threshold_db=fx.comp_threshold, ratio=max(1.0, fx.comp_ratio),
                            attack_ms=8, release_ms=120))
        if abs(fx.comp_makeup) > 0.05:
            p.append(Gain(gain_db=fx.comp_makeup))
    if fx.reverb_on:
        p.append(_reverb(fx))
    return Pedalboard(p)


def build_master_chain(fx: FxSettings) -> Pedalboard:
    p = []
    if fx.comp_on:
        p.append(Compressor(threshold_db=fx.comp_threshold, ratio=max(1.0, fx.comp_ratio),
                            attack_ms=15, release_ms=150))
        if abs(fx.comp_makeup) > 0.05:
            p.append(Gain(gain_db=fx.comp_makeup))
    if fx.reverb_on:
        p.append(_reverb(fx))
    if fx.limiter_on:
        p.append(Limiter(threshold_db=fx.limiter_threshold, release_ms=100))
    return Pedalboard(p)


def render_track(t: Track, length: int) -> np.ndarray | None:
    """Raw track audio -> processed stereo (length, 2) with effects and offset applied."""
    a = t.audio
    if a is None:
        return None
    if a.shape[1] == 1:
        a = np.repeat(a, 2, axis=1)       # mono -> stereo before reverb so it can spread
    chain = build_track_chain(t.fx)
    if len(chain):
        out = chain(np.ascontiguousarray(a.T), SR).T
    else:
        out = a
    out = np.asarray(out, dtype=np.float32)
    off = int(round(t.offset_ms * SR / 1000))
    if off:
        shifted = np.zeros_like(out)
        if off > 0:
            shifted[off:] = out[:-off]
        else:
            shifted[:off] = out[-off:]
        out = shifted
    if len(out) < length:
        out = np.pad(out, ((0, length - len(out)), (0, 0)))
    return np.ascontiguousarray(out[:length])


def track_gains(t: Track) -> tuple[float, float]:
    g = 10 ** (t.volume_db / 20)
    return g * min(1.0, 1.0 - t.pan), g * min(1.0, 1.0 + t.pan)


def audible(project: Project) -> list[Track]:
    soloed = any(t.solo for t in project.tracks)
    return [t for t in project.tracks if not t.mute and (t.solo or not soloed)]


def render_mix(project: Project, start: int, end: int, normalize: bool, progress=None) -> np.ndarray:
    """Full offline mixdown of [start, end) for export. Re-renders every track fresh."""
    tracks = audible(project)
    mix = np.zeros((end - start, 2), np.float32)
    for i, t in enumerate(tracks):
        if progress:
            progress(f"Rendering {t.name}...", i / max(1, len(tracks)))
        buf = render_track(t, project.length)
        if buf is None:
            continue
        gl, gr = track_gains(t)
        mix[:, 0] += buf[start:end, 0] * gl
        mix[:, 1] += buf[start:end, 1] * gr
    if progress:
        progress("Master effects...", 0.95)
    mix *= 10 ** (project.master_volume_db / 20)
    chain = build_master_chain(project.master_fx)
    if len(chain):
        mix = np.asarray(chain(np.ascontiguousarray(mix.T), SR).T, dtype=np.float32)
    peak = float(np.abs(mix).max()) if len(mix) else 0.0
    if normalize and peak > 1e-5:
        mix *= (10 ** (-1 / 20)) / peak    # peak-normalise to -1 dBFS
    elif peak > 1.0:
        mix /= peak                         # never export clipped audio
    return mix
