"""The ghost voice effect: reverse -> reverb -> reverse.

The classic studio trick: flip a recording backwards, add reverb, then flip it
forwards again. The reverb tail now *precedes* each sound, so every word
swells up out of a ghostly wash before it's spoken.

Doing that literally needs the whole recording up front, which is impossible
on a live mic. But time-reversal commutes with convolution:

    reverse(reverse(x) * h) == x * reverse(h)

so the whole reverse-reverb-reverse chain is identical to a single convolution
with the *time-reversed* impulse response. That filter is causal, so it runs in
real time; the only cost is a delay equal to the reverb length (the swell has
to play before the word it leads into). ``reverse_reverb_offline`` implements
the literal three-step version, and the tests check both agree sample for
sample.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, replace

import numpy as np
from scipy import signal

from .convolver import PartitionedConvolver
from .ir import load_ir, normalize_energy, shape_ir, synth_reverb_ir


@dataclass
class GhostSettings:
    tail: float = 0.45
    """Length of the reverse-reverb swell in seconds. Also the live latency."""
    decay: float = 1.2
    """RT60 of the synthetic reverb in seconds (ignored with ``ir_path``)."""
    mix: float = 0.7
    """0 = dry voice only, 1 = ghostly wash only."""
    highpass: float | None = 250.0
    """Cut lows from the wash below this frequency (Hz). None to disable."""
    lowpass: float | None = 9000.0
    """Cut highs from the wash above this frequency (Hz). None to disable."""
    gain_db: float = 0.0
    """Output gain in dB, applied before the soft limiter."""
    ir_path: str | None = None
    """Use a reverb impulse response from a WAV file instead of the synthetic one."""
    seed: int | None = 1234


def levels(settings: GhostSettings) -> tuple[float, float]:
    """Output levels ``(voice, ghost)`` as linear gains; 1.0 = original loudness."""
    mix = float(np.clip(settings.mix, 0.0, 1.0))
    g = 10 ** (settings.gain_db / 20)
    return (1.0 - mix) * g, mix * g


def with_levels(settings: GhostSettings, voice: float, ghost: float) -> GhostSettings:
    """Set the voice and ghost-wash levels independently (inverse of :func:`levels`)."""
    voice, ghost = max(float(voice), 0.0), max(float(ghost), 0.0)
    total = voice + ghost
    if total <= 1e-9:
        return replace(settings, mix=0.0, gain_db=-200.0)
    return replace(settings, mix=ghost / total, gain_db=float(20 * np.log10(total)))


def build_reverb_ir(sample_rate: int, settings: GhostSettings) -> np.ndarray:
    """The (forward) reverb impulse response, tone-shaped and energy-normalised."""
    if settings.ir_path:
        ir = load_ir(settings.ir_path, sample_rate, max_length=settings.tail)
    else:
        ir = synth_reverb_ir(sample_rate, settings.tail, settings.decay, settings.seed)
    ir = shape_ir(ir, sample_rate, settings.highpass, settings.lowpass)
    return normalize_energy(ir)


def build_kernel(sample_rate: int, settings: GhostSettings) -> np.ndarray:
    """Single FIR kernel equivalent to reverse-reverb-reverse plus dry mix.

    The wet part is the reversed reverb. The dry voice is a unit impulse at the
    *end* of the kernel, i.e. delayed so it lands right where the swell peaks.
    """
    ir = build_reverb_ir(sample_rate, settings)
    mix = float(np.clip(settings.mix, 0.0, 1.0))
    kernel = mix * ir[::-1]
    kernel[-1] += 1.0 - mix
    return kernel * 10 ** (settings.gain_db / 20)


def soft_limit(x: np.ndarray, threshold: float = 0.8) -> np.ndarray:
    """Transparent below ``threshold``, smoothly saturates towards +/-1 above it."""
    a = np.abs(x)
    over = a > threshold
    if not np.any(over):
        return x
    y = x.copy()
    head = 1.0 - threshold
    y[over] = np.sign(x[over]) * (threshold + head * np.tanh((a[over] - threshold) / head))
    return y


class GhostFilter:
    """Real-time ghost voice filter for a mono stream.

    Feed it blocks of ``block_size`` samples (multiples are fine) and it returns
    the same number of processed samples.

    :meth:`update` may be called from another thread (e.g. a GUI) while the
    audio thread calls :meth:`process`; the new sound is crossfaded in over
    ``crossfade`` seconds so slider moves don't click.
    """

    MAX_TAIL = 2.0
    """Input history kept for live changes of ``tail`` (seconds)."""

    def __init__(
        self,
        sample_rate: int,
        settings: GhostSettings | None = None,
        block_size: int = 256,
        crossfade: float = 0.03,
    ):
        self.sample_rate = sample_rate
        self.block_size = block_size
        self.settings = settings or GhostSettings()
        self._conv = PartitionedConvolver(
            build_kernel(sample_rate, self.settings), block_size, history=int(self.MAX_TAIL * sample_rate)
        )
        self._kernel_length = self._conv.kernel_length
        self._fade_blocks = max(1, round(crossfade * sample_rate / block_size))
        self._gen = itertools.count(1)
        self._pending: tuple | None = None  # (generation, H, length), replaced atomically
        self._installed = 0

    @property
    def latency_samples(self) -> int:
        """Delay from input to the dry voice in the output (for the latest settings)."""
        return self._kernel_length - 1

    @property
    def latency_seconds(self) -> float:
        return self.latency_samples / self.sample_rate

    def update(self, settings: GhostSettings) -> None:
        """Change settings while running (e.g. from a GUI slider). Thread-safe."""
        H, length = self._conv.prepare(build_kernel(self.sample_rate, settings))
        self.settings = settings
        self._kernel_length = length
        self._pending = (next(self._gen), H, length)

    def reset(self) -> None:
        self._conv.reset()

    def process(self, x: np.ndarray) -> np.ndarray:
        pending = self._pending
        if pending is not None and pending[0] != self._installed:
            self._installed = pending[0]
            self._conv.install(pending[1], pending[2], crossfade_blocks=self._fade_blocks)
        return soft_limit(self._conv.process(x))


def reverse_reverb_offline(x: np.ndarray, sample_rate: int, settings: GhostSettings | None = None) -> np.ndarray:
    """Literal reverse -> reverb -> reverse for a whole recording.

    Returns ``len(x) + tail_samples - 1`` samples. Because there's no real-time
    constraint, the output simply starts with the swell before the first word
    (no latency).
    """
    settings = settings or GhostSettings()
    x = np.asarray(x, dtype=np.float64).ravel()
    ir = build_reverb_ir(sample_rate, settings)
    mix = float(np.clip(settings.mix, 0.0, 1.0))

    reversed_x = x[::-1]                                   # 1. reverse
    reverbed = signal.fftconvolve(reversed_x, ir)          # 2. reverb
    wet = reverbed[::-1]                                   # 3. reverse again

    out = mix * wet
    out[ir.size - 1 :] += (1.0 - mix) * x                  # dry voice, aligned to the swell peak
    out *= 10 ** (settings.gain_db / 20)
    return soft_limit(out)
