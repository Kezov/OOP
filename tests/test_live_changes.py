"""Changing settings while streaming: smooth crossfade, then exactly the new sound."""

import numpy as np
import pytest

from ghostvoice import GhostFilter, GhostSettings
from ghostvoice.convolver import PartitionedConvolver

SR, B = 16000, 128
BASE = GhostSettings(tail=0.2, mix=0.5)


@pytest.mark.parametrize(
    "new",
    [
        GhostSettings(tail=0.2, mix=0.2, highpass=800.0),  # same length
        GhostSettings(tail=0.6, mix=0.5),  # longer swell: needs older input history
        GhostSettings(tail=0.1, mix=0.8),  # shorter swell
    ],
    ids=["same-length", "longer", "shorter"],
)
def test_update_crossfades_then_matches_new_settings(new):
    x = np.random.default_rng(3).standard_normal(SR * 2 // B * B) * 0.02  # quiet: limiter stays out of it
    switch = 100  # block index of the change
    g = GhostFilter(SR, BASE, B)
    out = []
    for n, i in enumerate(range(0, x.size, B)):
        if n == switch:
            g.update(new)
        out.append(g.process(x[i : i + B]))
    y = np.concatenate(out)

    old_only = GhostFilter(SR, BASE, B).process(x)
    new_only = GhostFilter(SR, new, B).process(x)
    s, fade = switch * B, g._fade_blocks * B
    ramp = np.arange(1, fade + 1) / fade

    np.testing.assert_allclose(y[:s], old_only[:s], atol=1e-12)
    np.testing.assert_allclose(y[s : s + fade], old_only[s : s + fade] + (new_only - old_only)[s : s + fade] * ramp, atol=1e-12)
    np.testing.assert_allclose(y[s + fade :], new_only[s + fade :], atol=1e-12)
    assert g.latency_samples == int(round(new.tail * SR)) - 1


def test_update_from_another_thread_is_picked_up_at_block_start():
    import threading

    g = GhostFilter(SR, BASE, B)
    t = threading.Thread(target=g.update, args=(GhostSettings(tail=0.3),))
    t.start()
    t.join()
    assert g._conv.kernel_length == int(0.2 * SR)  # not installed until the audio thread runs
    g.process(np.zeros(B))
    assert g._conv.kernel_length == int(0.3 * SR)


def test_convolver_keeps_history_when_kernel_grows():
    rng = np.random.default_rng(4)
    x = rng.standard_normal(64 * 40)
    h_long = rng.standard_normal(64 * 10)
    conv = PartitionedConvolver(np.ones(3), 64, history=64 * 16)
    a = conv.process(x[: 64 * 20])
    conv.set_kernel(h_long)
    b = conv.process(x[64 * 20 :])
    np.testing.assert_allclose(b, np.convolve(x, h_long)[64 * 20 : x.size], atol=1e-9)
    assert a.shape == (64 * 20,)
