import numpy as np
import pytest

from ghostvoice import GhostSettings, levels, with_levels
from ghostvoice.cli import main as cli_main
from ghostvoice.cli import settings_from_args
from ghostvoice.presets import DEFAULT_PRESET, NOTES, PRESETS, from_controls, to_controls


@pytest.mark.parametrize("voice,ghost", [(1.0, 0.32), (0.3, 0.7), (1.5, 0.0), (0.0, 1.2)])
def test_levels_round_trip(voice, ghost):
    v, g = levels(with_levels(GhostSettings(), voice, ghost))
    assert v == pytest.approx(voice) and g == pytest.approx(ghost)


def test_silence_is_allowed():
    v, g = levels(with_levels(GhostSettings(), 0, 0))
    assert v < 1e-9 and g < 1e-9


def test_mild_presets_keep_the_voice_at_full_volume():
    for name in ("mist", "breath", "faint", "veil"):
        voice, ghost = levels(PRESETS[name])
        assert voice == pytest.approx(1.0) and 0.2 < ghost < 0.5, name
    assert DEFAULT_PRESET == "mist"
    assert set(NOTES) == set(PRESETS)


@pytest.mark.parametrize("name", list(PRESETS))
def test_controls_round_trip(name):
    s = PRESETS[name]
    back = from_controls(to_controls(s))
    for field in ("tail", "decay", "highpass", "lowpass"):
        assert getattr(back, field) == getattr(s, field), field
    assert levels(back) == pytest.approx(levels(s))


def test_cli_voice_and_ghost_percent():
    import argparse

    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    from ghostvoice.cli import _add_effect_args

    _add_effect_args(sub.add_parser("x"))
    s = settings_from_args(p.parse_args(["x", "--preset", "ghost", "--voice", "100", "--ghost", "25"]))
    assert levels(s) == pytest.approx((1.0, 0.25))
    s = settings_from_args(p.parse_args(["x", "--preset", "mist", "--ghost", "50"]))
    assert levels(s) == pytest.approx((1.0, 0.5))


def test_cli_file_with_default_preset(tmp_path):
    import soundfile as sf

    sr = 16000
    x = np.random.default_rng(0).standard_normal(sr // 2) * 0.1
    sf.write(tmp_path / "in.wav", x, sr)
    cli_main(["file", str(tmp_path / "in.wav"), str(tmp_path / "out.wav")])
    y, ysr = sf.read(tmp_path / "out.wav")
    assert ysr == sr and y.size == x.size + int(PRESETS[DEFAULT_PRESET].tail * sr) - 1
