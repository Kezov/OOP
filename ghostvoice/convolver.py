"""Streaming FIR convolution using uniformly partitioned overlap-save (UPOLS).

Long kernels (tens of thousands of taps) are split into partitions of
``block_size`` samples so each audio block costs one FFT, one inverse FFT and a
cheap spectral multiply-accumulate, with only ``block_size`` samples of
buffering latency.

The kernel can be swapped while streaming. The input history is kept (up to
``history`` samples, growing if a longer kernel arrives), and the swap can be
crossfaded over a few blocks so it doesn't click.
"""

from __future__ import annotations

import numpy as np


class PartitionedConvolver:
    def __init__(self, kernel: np.ndarray, block_size: int, history: int = 0):
        if block_size <= 0:
            raise ValueError("block_size must be positive")
        self.block_size = block_size
        self._fft_size = 2 * block_size
        self._prev = np.zeros(block_size)
        self._ring_pos = 0
        # Frequency-domain delay line: spectra of the most recent input frames.
        self._fdl = np.zeros((max(-(-history // block_size), 1), block_size + 1), dtype=complex)
        self._H: np.ndarray | None = None
        self._fade_from: np.ndarray | None = None
        self._fade_pos = 0
        self._fade_len = 0
        self.set_kernel(kernel)

    @property
    def num_partitions(self) -> int:
        return self._H.shape[0]

    def prepare(self, kernel: np.ndarray) -> tuple[np.ndarray, int]:
        """Partition and FFT a kernel without touching the stream state.

        Safe to call from another thread; pass the result to :meth:`install`.
        """
        kernel = np.asarray(kernel, dtype=np.float64).ravel()
        if kernel.size == 0:
            kernel = np.zeros(1)
        b = self.block_size
        parts = -(-kernel.size // b)
        padded = np.zeros(parts * b)
        padded[: kernel.size] = kernel
        return np.fft.rfft(padded.reshape(parts, b), n=self._fft_size, axis=1), kernel.size

    def install(self, H: np.ndarray, length: int, crossfade_blocks: int = 0) -> None:
        """Switch to a prepared kernel, optionally crossfading from the old one."""
        self._ensure_history(H.shape[0])
        if crossfade_blocks > 0 and self._H is not None:
            self._fade_from = self._H
            self._fade_pos = 0
            self._fade_len = crossfade_blocks * self.block_size
        else:
            self._fade_from = None
        self._H = H
        self.kernel_length = length

    def set_kernel(self, kernel: np.ndarray, crossfade_blocks: int = 0) -> None:
        """Swap the kernel. The input history is kept, so streaming continues."""
        self.install(*self.prepare(kernel), crossfade_blocks)

    def _ensure_history(self, partitions: int) -> None:
        cap = self._fdl.shape[0]
        if partitions <= cap:
            return
        # Re-linearise newest-first; history older than we kept is silence.
        grown = np.zeros((partitions, self._fdl.shape[1]), dtype=complex)
        grown[:cap] = self._fdl[(self._ring_pos + np.arange(cap)) % cap]
        self._fdl = grown
        self._ring_pos = 0

    def reset(self) -> None:
        self._fdl[:] = 0
        self._prev[:] = 0
        self._ring_pos = 0
        self._fade_from = None

    def _convolve(self, H: np.ndarray) -> np.ndarray:
        # Partition k multiplies the input spectrum from k blocks ago.
        idx = (self._ring_pos + np.arange(H.shape[0])) % self._fdl.shape[0]
        Y = np.einsum("kf,kf->f", self._fdl[idx], H)
        return np.fft.irfft(Y, n=self._fft_size)[self.block_size :]

    def process_block(self, block: np.ndarray) -> np.ndarray:
        """Convolve exactly one block of ``block_size`` samples."""
        b = self.block_size
        frame = np.concatenate((self._prev, block))
        self._prev = np.array(block, dtype=np.float64, copy=True)

        self._ring_pos = (self._ring_pos - 1) % self._fdl.shape[0]
        self._fdl[self._ring_pos] = np.fft.rfft(frame)

        y = self._convolve(self._H)
        if self._fade_from is not None:
            old = self._convolve(self._fade_from)
            ramp = np.minimum((self._fade_pos + np.arange(1, b + 1)) / self._fade_len, 1.0)
            y = old + (y - old) * ramp
            self._fade_pos += b
            if self._fade_pos >= self._fade_len:
                self._fade_from = None
        return y

    def process(self, x: np.ndarray) -> np.ndarray:
        """Convolve any number of whole blocks."""
        x = np.asarray(x, dtype=np.float64).ravel()
        b = self.block_size
        if x.size % b:
            raise ValueError(f"input length {x.size} is not a multiple of block_size {b}")
        out = np.empty_like(x)
        for i in range(0, x.size, b):
            out[i : i + b] = self.process_block(x[i : i + b])
        return out
