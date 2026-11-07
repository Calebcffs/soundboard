"""Measure real round-trip latency: play clicks out, find them in what comes back in.

The clicks go out and the input is captured inside the *same* audio callbacks the
recorder uses, so the measured delay is exactly the shift a recorded take needs.
"""
from __future__ import annotations

import numpy as np

from .project import SR

# irregular gaps so a click can't be mistaken for its neighbour
CLICK_TIMES = [0.6, 1.1, 1.75, 2.2, 2.9, 3.35, 4.0, 4.55]
LENGTH_S = 5.4
MAX_LAG_S = 0.4


def _burst() -> np.ndarray:
    n = int(0.004 * SR)
    rng = np.random.default_rng(7)
    return (rng.uniform(-1, 1, n) * np.hanning(n)).astype(np.float32)


def click_train(level: float = 0.5) -> np.ndarray:
    sig = np.zeros(int(LENGTH_S * SR), np.float32)
    b = _burst() * level
    for t in CLICK_TIMES:
        i = int(t * SR)
        sig[i:i + len(b)] += b
    return sig


def _xcorr(rec: np.ndarray, ref: np.ndarray) -> np.ndarray:
    """corr[lag] = sum ref[i] * rec[i + lag] for lag >= 0."""
    n = len(rec) + len(ref)
    nfft = 1 << (n - 1).bit_length()
    c = np.fft.irfft(np.fft.rfft(rec, nfft) * np.conj(np.fft.rfft(ref, nfft)), nfft)
    return c[: len(rec)]


def measure(rec: np.ndarray, level: float = 0.5) -> tuple[float | None, str]:
    """Returns (round-trip ms or None, human message)."""
    ref = click_train(level)
    if len(rec) < len(ref) * 0.9:
        return None, "Recording was cut short, try again."
    peak_in = float(np.abs(rec).max())
    if peak_in < 0.003:
        return None, "Didn't hear anything on that input. Check the cable/mic and the gain knob."
    max_lag = int(MAX_LAG_S * SR)
    corr = np.abs(_xcorr(rec, ref)[:max_lag])
    lag = int(np.argmax(corr))
    noise = float(np.median(corr)) + 1e-9
    if corr[lag] / noise < 8:
        return None, "Couldn't pick the clicks out of the noise. Turn the headphones up a bit or get the mic closer."

    # check every click individually lands at the same delay
    b = _burst()
    lags = []
    for t in CLICK_TIMES:
        i = int(t * SR) + lag
        lo, hi = i - 480, i + len(b) + 480
        if lo < 0 or hi > len(rec):
            continue
        c = np.abs(np.correlate(rec[lo:hi], b, mode="valid"))
        lags.append(lo + int(np.argmax(c)) - int(t * SR))
    if len(lags) < 5:
        return None, "Only heard some of the clicks, try again."
    lags = np.array(lags)
    spread_ms = (lags.max() - lags.min()) / SR * 1000
    if spread_ms > 2.0:
        return None, f"The clicks came back inconsistently ({spread_ms:.1f} ms spread). Try again somewhere quieter."
    ms = float(np.median(lags)) / SR * 1000
    return ms, f"Measured round trip: {ms:.1f} ms (all {len(lags)} clicks agree within {spread_ms:.2f} ms)."
