import numpy as np
import pytest

from ghostvoice import GhostFilter, GhostSettings, reverse_reverb_offline
from ghostvoice.ghost import soft_limit

SR = 16000


@pytest.mark.parametrize("mix", [0.0, 0.5, 1.0])
def test_realtime_equals_literal_reverse_reverb_reverse(mix):
    settings = GhostSettings(tail=0.2, mix=mix, gain_db=-20)  # quiet: keep limiter out of it
    rng = np.random.default_rng(1)
    x = rng.standard_normal(SR // 2) * 0.1
    offline = reverse_reverb_offline(x, SR, settings)

    ghost = GhostFilter(SR, settings, block_size=128)
    n = -(-offline.size // 128) * 128
    padded = np.zeros(n)
    padded[: x.size] = x
    live = ghost.process(padded)[: offline.size]

    np.testing.assert_allclose(live, offline, atol=1e-9)


def test_swell_comes_before_the_sound():
    settings = GhostSettings(tail=0.3, mix=1.0)
    ghost = GhostFilter(SR, settings, block_size=160)
    x = np.zeros(SR)
    x[0] = 0.5  # a click at t=0
    y = ghost.process(x)
    lat = ghost.latency_samples
    assert lat == int(0.3 * SR) - 1
    before = y[:lat]
    # Energy rises towards the moment the dry sound would land.
    first, second = np.sum(before[: lat // 2] ** 2), np.sum(before[lat // 2 :] ** 2)
    assert second > 5 * first
    # And nothing after it: the reverb is entirely "pre-verb".
    assert np.allclose(y[lat + 1 :], 0, atol=1e-12)


def test_dry_only_is_pure_delay():
    ghost = GhostFilter(SR, GhostSettings(tail=0.1, mix=0.0), block_size=64)
    x = np.random.default_rng(2).standard_normal(SR // 4) * 0.1
    padded = np.concatenate((x, np.zeros(ghost.latency_samples + 64)))
    padded = padded[: len(padded) // 64 * 64]
    y = ghost.process(padded)
    d = ghost.latency_samples
    np.testing.assert_allclose(y[d : d + x.size], x, atol=1e-9)


def test_soft_limit_bounds_and_transparency():
    x = np.linspace(-10, 10, 1001)
    y = soft_limit(x)
    assert np.all(np.abs(y) <= 1.0)
    small = np.abs(x) <= 0.8
    np.testing.assert_array_equal(y[small], x[small])


def test_update_settings_live():
    ghost = GhostFilter(SR, GhostSettings(tail=0.2), block_size=128)
    ghost.process(np.zeros(128))
    ghost.update(GhostSettings(tail=0.2, mix=0.2))
    ghost.update(GhostSettings(tail=0.4))
    assert ghost.latency_samples == int(0.4 * SR) - 1
    assert ghost.process(np.zeros(256)).shape == (256,)
