"""Reverb impulse responses: synthetic generation and loading from WAV files."""

from __future__ import annotations

import numpy as np
from scipy import signal


def synth_reverb_ir(
    sample_rate: int,
    length: float = 0.5,
    decay: float = 1.2,
    seed: int | None = 1234,
) -> np.ndarray:
    """Generate a plate/hall-like reverb tail from exponentially decaying noise.

    The tail is split into three bands with different decay rates (highs die
    out faster than lows, as in a real room), which keeps it from sounding like
    plain hiss.

    Args:
        sample_rate: Sample rate in Hz.
        length: Length of the impulse response in seconds. This is also the
            length of the "pre-swell" you hear before each word, and the
            real-time latency of the effect.
        decay: RT60 decay time in seconds (time to fall by 60 dB). Longer than
            ``length`` means the tail is truncated while still loud, which
            gives a fuller swell.
        seed: Random seed so the same settings always sound the same.
    """
    n = max(int(round(length * sample_rate)), 1)
    t = np.arange(n) / sample_rate
    rng = np.random.default_rng(seed)
    nyq = sample_rate / 2

    bands = [
        # (sos filter, decay multiplier)
        (signal.butter(2, min(500, nyq * 0.9), "lowpass", fs=sample_rate, output="sos"), 1.25),
        (signal.butter(2, [500, min(4000, nyq * 0.95)], "bandpass", fs=sample_rate, output="sos"), 1.0),
        (signal.butter(2, min(4000, nyq * 0.95), "highpass", fs=sample_rate, output="sos"), 0.55),
    ]
    ir = np.zeros(n)
    for sos, mult in bands:
        noise = signal.sosfilt(sos, rng.standard_normal(n))
        rt60 = max(decay * mult, 1e-3)
        ir += noise * np.exp(-6.907755 * t / rt60)  # ln(1000) -> -60 dB at rt60

    # Fade the very end out so the (later reversed) swell fades *in* smoothly
    # instead of starting with a click.
    fade = min(n, max(int(0.25 * n), 1))
    ir[n - fade :] *= 0.5 * (1 + np.cos(np.linspace(0, np.pi, fade)))
    return ir


def load_ir(path: str, sample_rate: int, max_length: float | None = None) -> np.ndarray:
    """Load an impulse response from an audio file, mixed to mono and resampled."""
    import soundfile as sf

    data, sr = sf.read(path, always_2d=True, dtype="float64")
    ir = data.mean(axis=1)
    if sr != sample_rate:
        ir = signal.resample_poly(ir, sample_rate, sr)
    # Drop leading silence so the direct sound sits at sample 0.
    peak = np.max(np.abs(ir)) if ir.size else 0.0
    if peak > 0:
        start = int(np.argmax(np.abs(ir) > peak * 0.01))
        ir = ir[start:]
    if max_length is not None:
        ir = ir[: max(int(max_length * sample_rate), 1)]
    return ir


def shape_ir(
    ir: np.ndarray,
    sample_rate: int,
    highpass: float | None = 250.0,
    lowpass: float | None = 9000.0,
) -> np.ndarray:
    """Tone-shape the reverb. Filtering the IR is the same as filtering the wet signal.

    A highpass keeps the ghostly wash thin and airy instead of boomy.
    """
    nyq = sample_rate / 2
    out = ir
    if highpass:
        sos = signal.butter(2, min(highpass, nyq * 0.95), "highpass", fs=sample_rate, output="sos")
        out = signal.sosfilt(sos, out)
    if lowpass and lowpass < nyq:
        sos = signal.butter(2, lowpass, "lowpass", fs=sample_rate, output="sos")
        out = signal.sosfilt(sos, out)
    return out


def normalize_energy(ir: np.ndarray) -> np.ndarray:
    """Scale so the IR has unit energy (wet level roughly matches dry level)."""
    energy = float(np.sqrt(np.sum(ir**2)))
    return ir / energy if energy > 0 else ir
