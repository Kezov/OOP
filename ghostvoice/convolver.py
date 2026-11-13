"""Streaming FIR convolution using uniformly partitioned overlap-save (UPOLS).

Long kernels (tens of thousands of taps) are split into partitions of
``block_size`` samples so each audio block costs one FFT, one inverse FFT and a
cheap spectral multiply-accumulate, with only ``block_size`` samples of
buffering latency.
"""

from __future__ import annotations

import numpy as np


class PartitionedConvolver:
    def __init__(self, kernel: np.ndarray, block_size: int):
        if block_size <= 0:
            raise ValueError("block_size must be positive")
        self.block_size = block_size
        self._fft_size = 2 * block_size
        self._prev = np.zeros(block_size)
        self._ring_pos = 0
        self.set_kernel(kernel)

    @property
    def num_partitions(self) -> int:
        return self._H.shape[0]

    def set_kernel(self, kernel: np.ndarray) -> None:
        """Swap the kernel. Resets the convolution history if the size changes."""
        kernel = np.asarray(kernel, dtype=np.float64).ravel()
        if kernel.size == 0:
            kernel = np.zeros(1)
        b = self.block_size
        parts = -(-kernel.size // b)
        padded = np.zeros(parts * b)
        padded[: kernel.size] = kernel
        H = np.fft.rfft(padded.reshape(parts, b), n=self._fft_size, axis=1)
        if not hasattr(self, "_H") or H.shape != self._H.shape:
            self._fdl = np.zeros_like(H)  # frequency-domain delay line
            self._ring_pos = 0
            self._prev[:] = 0
        self._H = H
        self.kernel_length = kernel.size

    def reset(self) -> None:
        self._fdl[:] = 0
        self._prev[:] = 0
        self._ring_pos = 0

    def process_block(self, block: np.ndarray) -> np.ndarray:
        """Convolve exactly one block of ``block_size`` samples."""
        b = self.block_size
        frame = np.concatenate((self._prev, block))
        self._prev = np.array(block, dtype=np.float64, copy=True)

        p = self.num_partitions
        self._ring_pos = (self._ring_pos - 1) % p
        self._fdl[self._ring_pos] = np.fft.rfft(frame)

        # Partition k multiplies the input spectrum from k blocks ago.
        order = (self._ring_pos + np.arange(p)) % p
        Y = np.einsum("kf,kf->f", self._fdl[order], self._H)
        return np.fft.irfft(Y, n=self._fft_size)[b:]

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
