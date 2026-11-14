"""Pitch shift: deeper or higher voice, in real time.

A granular delay-line shifter. Two read taps sweep through a short history of
the input at ``ratio`` times normal speed, each faded in and out with a
sin^2 window half a grain apart, so the windows always add up to 1 and the
jump when a tap wraps around happens while it is silent. The cost is a small
delay of about half a grain (~25 ms), far less than the ghost effect's swell.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

import numpy as np
from scipy import signal

from .ghost import soft_limit

HIGHCUT_OFF = 20000.0
"""Controls value meaning "no high cut"."""


@dataclass
class PitchSettings:
    semitones: float = -4.0
    """Pitch change; -12 = an octave down, +12 = an octave up."""
    shifted: float = 1.0
    """Volume of the pitch-shifted voice (1.0 = original loudness)."""
    original: float = 0.0
    """Volume of your unshifted voice mixed in (0 = none)."""
    grain: float = 0.05
    """Grain length in seconds: shorter is tighter but buzzier, longer is smoother but echoier."""
    lowcut: float | None = None
    """Thin out the shifted voice below this frequency (Hz). None to disable."""
    highcut: float | None = None
    """Darken the shifted voice above this frequency (Hz). None to disable."""


def pitch_delay(settings: PitchSettings) -> float:
    """Average delay of the shifted voice in seconds."""
    return settings.grain / 2


class _Voice:
    """One parameter set of the shifter, with its own sweep phase and filter state."""

    def __init__(self, sample_rate: int, s: PitchSettings, phase: float = 0.0):
        self.ratio = 2.0 ** (s.semitones / 12)
        self.window = int(np.clip(s.grain * sample_rate, 16, PitchFilter.MAX_GRAIN * sample_rate))
        self.inc = (1.0 - self.ratio) / self.window
        self.phase = phase
        self.shifted, self.original = max(s.shifted, 0.0), max(s.original, 0.0)
        nyq = sample_rate / 2
        sections = []
        if s.lowcut:
            sections.append(signal.butter(2, min(s.lowcut, nyq * 0.95), "highpass", fs=sample_rate, output="sos"))
        if s.highcut and s.highcut < nyq * 0.95:
            sections.append(signal.butter(2, s.highcut, "lowpass", fs=sample_rate, output="sos"))
        self.sos = np.vstack(sections) if sections else None
        self.zi = np.zeros((self.sos.shape[0], 2)) if sections else None

    def render(self, buf: np.ndarray, x: np.ndarray) -> np.ndarray:
        n = x.size
        k = np.arange(1, n + 1)
        phase_a = (self.phase + k * self.inc) % 1.0
        self.phase = float(phase_a[-1])
        now = buf.size - n + np.arange(n)  # buffer index of each new input sample
        out = np.zeros(n)
        for ph in (phase_a, (phase_a + 0.5) % 1.0):
            pos = now - 1.0 - ph * self.window  # at least one sample back, so i0 + 1 <= now
            i0 = np.floor(pos).astype(np.int64)
            frac = pos - i0
            tap = buf[i0] * (1.0 - frac) + buf[i0 + 1] * frac
            out += np.sin(np.pi * ph) ** 2 * tap  # the two windows sum to 1
        if self.sos is not None:
            out, self.zi = signal.sosfilt(self.sos, out, zi=self.zi)
        return self.shifted * out + self.original * x


class PitchFilter:
    """Real-time pitch shifter for a mono stream, same interface as :class:`GhostFilter`."""

    MAX_GRAIN = 0.2
    """Longest grain in seconds (sets the history kept)."""

    def __init__(self, sample_rate: int, settings: PitchSettings | None = None, block_size: int = 256,
                 crossfade: float = 0.03):
        self.sample_rate = sample_rate
        self.block_size = block_size
        self.settings = settings or PitchSettings()
        self._buf = np.zeros(int(self.MAX_GRAIN * sample_rate) + 4 * block_size + 4)
        self._voice = _Voice(sample_rate, self.settings)
        self._fade_from: _Voice | None = None
        self._fade_len = max(1, round(crossfade * sample_rate / block_size)) * block_size
        self._fade_pos = 0
        self._gen = itertools.count(1)
        self._pending: tuple | None = None
        self._installed = 0

    @property
    def latency_seconds(self) -> float:
        return pitch_delay(self.settings)

    @property
    def latency_samples(self) -> int:
        return int(self.latency_seconds * self.sample_rate)

    def update(self, settings: PitchSettings) -> None:
        """Change settings while running. Thread-safe; crossfaded at the next block."""
        self.settings = settings
        self._pending = (next(self._gen), settings)

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float64).ravel()
        pending = self._pending
        if pending is not None and pending[0] != self._installed:
            self._installed = pending[0]
            self._fade_from = self._voice
            self._voice = _Voice(self.sample_rate, pending[1], phase=self._voice.phase)
            self._fade_pos = 0
        out = np.empty_like(x)
        b = self.block_size
        for i in range(0, x.size, b):  # history must cover each chunk, so go block by block
            chunk = x[i : i + b]
            self._buf = np.concatenate((self._buf[chunk.size :], chunk))
            y = self._voice.render(self._buf, chunk)
            if self._fade_from is not None:
                old = self._fade_from.render(self._buf, chunk)
                ramp = np.minimum((self._fade_pos + np.arange(1, chunk.size + 1)) / self._fade_len, 1.0)
                y = old + (y - old) * ramp
                self._fade_pos += chunk.size
                if self._fade_pos >= self._fade_len:
                    self._fade_from = None
            out[i : i + chunk.size] = y
        return soft_limit(out)


def pitch_offline(x: np.ndarray, sample_rate: int, settings: PitchSettings) -> np.ndarray:
    """Run a whole recording through the live filter (used by the app's test playback)."""
    x = np.asarray(x, dtype=np.float64).ravel()
    f = PitchFilter(sample_rate, settings, 256)
    tail = int(settings.grain * sample_rate) + 256
    padded = np.concatenate((x, np.zeros(tail)))
    return f.process(padded)


PITCH_PRESETS: dict[str, PitchSettings] = {
    "deep": PitchSettings(semitones=-4),
    "demon": PitchSettings(semitones=-10, original=0.3, grain=0.06, highcut=8000.0),
    "giant": PitchSettings(semitones=-12, grain=0.07, highcut=6000.0),
    "twin": PitchSettings(semitones=-5, shifted=0.7, original=0.8),
    "chipmunk": PitchSettings(semitones=7, grain=0.04, lowcut=150.0),
    "helium": PitchSettings(semitones=12, grain=0.03, lowcut=200.0),
}

PITCH_NOTES: dict[str, str] = {
    "deep": "A deeper, bigger voice that still sounds like you.",
    "demon": "Very low with a trace of your real voice underneath. Menacing.",
    "giant": "A full octave down, dark and booming.",
    "twin": "Your voice plus a lower twin speaking along with you.",
    "chipmunk": "High and squeaky.",
    "helium": "A full octave up: the helium-balloon voice.",
}


def pitch_to_controls(s: PitchSettings) -> dict[str, float]:
    return {
        "pitch": s.semitones,
        "shifted": s.shifted * 100,
        "original": s.original * 100,
        "grain": s.grain * 1000,
        "lowcut": s.lowcut or 0.0,
        "highcut": s.highcut or HIGHCUT_OFF,
    }


def pitch_from_controls(c: dict[str, float]) -> PitchSettings:
    lo, hi = float(c["lowcut"]), float(c["highcut"])
    return PitchSettings(
        semitones=float(c["pitch"]),
        shifted=float(c["shifted"]) / 100,
        original=float(c["original"]) / 100,
        grain=float(c["grain"]) / 1000,
        lowcut=lo if lo > 0 else None,
        highcut=hi if hi < HIGHCUT_OFF else None,
    )
