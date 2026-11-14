"""The app's effects ("settings"): each has its own sliders, presets and filter.

Everything the GUI and the live engine need to know about an effect lives in
an :class:`Effect`. Presets are stored as slider values ("controls"), the same
form the app saves custom presets in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from .ghost import GhostFilter, GhostSettings, reverse_reverb_offline, voice_delay
from .pitch import (HIGHCUT_OFF, PITCH_NOTES, PITCH_PRESETS, PitchFilter, PitchSettings, pitch_delay,
                    pitch_from_controls, pitch_offline, pitch_to_controls)
from .presets import DEFAULT_PRESET, LOWPASS_OFF, NOTES, PRESETS, from_controls, to_controls


@dataclass(frozen=True)
class Slider:
    key: str
    label: str
    lo: float
    hi: float
    step: float
    fmt: Callable[[float], str]

    def quantize(self, v: float) -> float:
        return float(min(max(round((v - self.lo) / self.step) * self.step + self.lo, self.lo), self.hi))


@dataclass(frozen=True)
class Effect:
    key: str
    label: str
    sliders: tuple[Slider, ...]
    presets: dict[str, dict[str, float]]
    """Built-in presets as slider values."""
    notes: dict[str, str]
    default: str
    settings: Callable[[dict[str, float]], Any]
    """Slider values -> the filter's settings object."""
    extra: dict = field(default_factory=dict)

    def slider(self, key: str) -> Slider:
        return next(s for s in self.sliders if s.key == key)

    def clean(self, controls: dict) -> dict[str, float]:
        """Complete and quantize slider values (fills gaps from the default preset)."""
        base = self.presets[self.default]
        out = {}
        for s in self.sliders:
            try:
                out[s.key] = s.quantize(float(controls.get(s.key, base[s.key])))
            except (TypeError, ValueError):
                out[s.key] = s.quantize(base[s.key])
        return out


def _pct(v: float) -> str:
    return f"{v:.0f}%"


GHOST = Effect(
    key="ghost",
    label="Ghost voice",
    sliders=(
        Slider("voice", "Voice volume", 0, 150, 1, _pct),
        Slider("ghost", "Ghost volume", 0, 150, 1, _pct),
        Slider("tail", "Swell length (= delay)", 0.1, 1.0, 0.05, lambda v: f"{v:.2f} s"),
        Slider("decay", "Reverb decay", 0.3, 4.0, 0.1, lambda v: f"{v:.1f} s"),
        Slider("highpass", "Ghost low cut", 0, 2000, 50, lambda v: "off" if v <= 0 else f"{v:.0f} Hz"),
        Slider("lowpass", "Ghost high cut", 2000, LOWPASS_OFF, 500,
               lambda v: "off" if v >= LOWPASS_OFF else f"{v / 1000:.1f} kHz"),
    ),
    presets={name: to_controls(s) for name, s in PRESETS.items()},
    notes=NOTES,
    default=DEFAULT_PRESET,
    settings=from_controls,
)

PITCH = Effect(
    key="pitch",
    label="Pitch shift",
    sliders=(
        Slider("pitch", "Pitch", -12, 12, 0.5, lambda v: f"{v:+.1f} st"),
        Slider("shifted", "Shifted voice", 0, 150, 1, _pct),
        Slider("original", "Original voice", 0, 150, 1, _pct),
        Slider("grain", "Grain size", 20, 120, 5, lambda v: f"{v:.0f} ms"),
        Slider("lowcut", "Low cut", 0, 1000, 25, lambda v: "off" if v <= 0 else f"{v:.0f} Hz"),
        Slider("highcut", "High cut", 2000, HIGHCUT_OFF, 500,
               lambda v: "off" if v >= HIGHCUT_OFF else f"{v / 1000:.1f} kHz"),
    ),
    presets={name: pitch_to_controls(s) for name, s in PITCH_PRESETS.items()},
    notes=PITCH_NOTES,
    default="deep",
    settings=pitch_from_controls,
)

EFFECTS: dict[str, Effect] = {e.key: e for e in (GHOST, PITCH)}
DEFAULT_EFFECT = "ghost"


def make_filter(sample_rate: int, settings, block_size: int):
    """A real-time filter for any effect's settings."""
    if isinstance(settings, PitchSettings):
        return PitchFilter(sample_rate, settings, block_size)
    return GhostFilter(sample_rate, settings, block_size)


def delay_seconds(settings) -> float:
    """How long the live voice is delayed by an effect's settings."""
    if isinstance(settings, PitchSettings):
        return pitch_delay(settings)
    return voice_delay(settings)


def render(x: np.ndarray, sample_rate: int, settings) -> np.ndarray:
    """A whole recording with the effect applied (for test playback)."""
    if isinstance(settings, PitchSettings):
        return pitch_offline(x, sample_rate, settings)
    if isinstance(settings, GhostSettings):
        return reverse_reverb_offline(x, sample_rate, settings)
    raise TypeError(f"unknown settings {settings!r}")
