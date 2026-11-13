"""Built-in presets, and conversion between settings and the app's simple controls."""

from __future__ import annotations

from .ghost import GhostSettings, levels, with_levels


def _mild(ghost: float, **kw) -> GhostSettings:
    """Voice at full volume with the ghost wash at ``ghost`` x the voice level."""
    return with_levels(GhostSettings(**kw), voice=1.0, ghost=ghost)


PRESETS: dict[str, GhostSettings] = {
    # Mild: the voice stays at full volume, so every word is easy to understand.
    "mist": _mild(0.32),
    "breath": _mild(0.40, tail=0.35, decay=1.0, highpass=1200.0, lowpass=10000.0),
    "faint": _mild(0.32, tail=0.3, decay=0.9, highpass=500.0, lowpass=7000.0),
    "veil": _mild(0.35, tail=0.4, decay=1.4, highpass=500.0, lowpass=4500.0),
    # Strong: the voice sinks into the wash.
    "ghost": GhostSettings(),
    "whisper": GhostSettings(tail=0.3, decay=0.8, mix=0.6, highpass=400.0),
    "phantom": GhostSettings(tail=0.7, decay=2.0, mix=0.8, highpass=200.0, lowpass=7000.0),
    "banshee": GhostSettings(tail=0.6, decay=1.5, mix=0.9, highpass=900.0, lowpass=12000.0),
}

NOTES: dict[str, str] = {
    "mist": "The classic ghost swell, turned down so every word stays clear.",
    "breath": "Airy breath around a clear voice. The easiest to understand.",
    "faint": "A short, light swell with the least delay.",
    "veil": "A dark, soft cloud behind the voice.",
    "ghost": "Full effect: the voice sinks into the wash.",
    "whisper": "Short and thin, voice partly buried.",
    "phantom": "Long, heavy swell (most delay). Hard to understand.",
    "banshee": "Thin, shrieking wash with a very quiet voice.",
}

DEFAULT_PRESET = "mist"

LOWPASS_OFF = 20000.0
"""Controls value meaning "no high cut"."""


def to_controls(s: GhostSettings) -> dict[str, float]:
    """Settings -> app controls (voice/ghost in %, filters with 'off' values)."""
    voice, ghost = levels(s)
    return {
        "voice": voice * 100,
        "ghost": ghost * 100,
        "tail": s.tail,
        "decay": s.decay,
        "highpass": s.highpass or 0.0,
        "lowpass": s.lowpass or LOWPASS_OFF,
    }


def from_controls(c: dict[str, float], base: GhostSettings | None = None) -> GhostSettings:
    """App controls -> settings. ``base`` supplies anything the controls don't cover (e.g. ir_path)."""
    s = base or GhostSettings()
    hp, lp = float(c["highpass"]), float(c["lowpass"])
    s = GhostSettings(
        tail=float(c["tail"]),
        decay=float(c["decay"]),
        highpass=hp if hp > 0 else None,
        lowpass=lp if lp < LOWPASS_OFF else None,
        ir_path=s.ir_path,
        seed=s.seed,
    )
    return with_levels(s, float(c["voice"]) / 100, float(c["ghost"]) / 100)
