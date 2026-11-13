import numpy as np
import pytest

from ghostvoice.convolver import PartitionedConvolver


@pytest.mark.parametrize("block,klen", [(64, 1), (64, 64), (64, 1000), (128, 129)])
def test_matches_direct_convolution(block, klen):
    rng = np.random.default_rng(0)
    h = rng.standard_normal(klen)
    x = rng.standard_normal(block * 30)
    y = PartitionedConvolver(h, block).process(x)
    np.testing.assert_allclose(y, np.convolve(x, h)[: x.size], atol=1e-9)


def test_rejects_partial_blocks():
    with pytest.raises(ValueError):
        PartitionedConvolver(np.ones(4), 8).process(np.ones(5))


def test_set_kernel_same_size_keeps_streaming():
    conv = PartitionedConvolver(np.ones(100), 32)
    conv.process(np.ones(64))
    conv.set_kernel(np.zeros(100))
    assert np.allclose(conv.process(np.ones(32)), 0)
