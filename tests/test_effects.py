import numpy as np
import pytest

from ghostvoice import realtime
from ghostvoice.effects import EFFECTS, delay_seconds, make_filter, render
from ghostvoice.ghost import GhostFilter
from ghostvoice.hotkeys import NUMPAD_SCANCODES, ChordTracker
from ghostvoice.pitch import PITCH_PRESETS, PitchFilter, PitchSettings

SR = 48000


def peak_hz(y, sr=SR):
    y = y[sr // 4 :]
    spec = np.abs(np.fft.rfft(y * np.hanning(y.size)))
    return np.fft.rfftfreq(y.size, 1 / sr)[spec.argmax()]


@pytest.mark.parametrize("semitones", [-12, -4, 0, 7, 12])
def test_pitch_shift_lands_on_the_right_note(semitones):
    x = 0.3 * np.sin(2 * np.pi * 440 * np.arange(SR) / SR)
    y = PitchFilter(SR, PitchSettings(semitones=semitones), 256).process(x)
    assert peak_hz(y) == pytest.approx(440 * 2 ** (semitones / 12), abs=2)
    assert np.sqrt(np.mean(y[SR // 4 :] ** 2)) == pytest.approx(0.3 / np.sqrt(2), rel=0.05)  # same loudness


def test_pitch_update_is_smooth_and_then_exact():
    x = 0.3 * np.sin(2 * np.pi * 300 * np.arange(SR) / SR)
    f = PitchFilter(SR, PitchSettings(semitones=-5), 256)
    first = f.process(x[: SR // 2])
    new = PitchSettings(semitones=5, lowcut=100.0)
    f.update(new)
    second = f.process(x[SR // 2 :])
    y = np.concatenate((first, second))
    assert np.abs(np.diff(y)).max() < 0.1  # no clicks at the switch
    fresh = PitchFilter(SR, new, 256).process(x)[SR // 2 :]
    assert peak_hz(second) == peak_hz(fresh)  # then sounds just like the new settings
    assert peak_hz(second) == pytest.approx(300 * 2 ** (5 / 12), abs=10)  # (grain-rate sidebands aside)


def test_original_voice_mix_and_offline_render():
    x = np.random.default_rng(2).standard_normal(SR // 2) * 0.05
    s = PitchSettings(semitones=0, shifted=0.0, original=1.0)
    np.testing.assert_allclose(PitchFilter(SR, s, 256).process(x), x, atol=1e-12)
    y = render(x, SR, PITCH_PRESETS["chipmunk"])
    assert y.size > x.size and np.isfinite(y).all()


@pytest.mark.parametrize("key", list(EFFECTS))
def test_every_preset_runs_and_round_trips(key):
    e = EFFECTS[key]
    assert e.default in e.presets and set(e.notes) >= set(e.presets)
    x = np.random.default_rng(0).standard_normal(256 * 8) * 0.1
    for name, c in e.presets.items():
        assert e.clean(c) == pytest.approx(c), name
        s = e.settings(c)
        assert np.isfinite(make_filter(SR, s, 256).process(x)).all(), name
        assert 0 <= delay_seconds(s) <= 1.0


def test_clean_fills_and_clamps_controls():
    pitch = EFFECTS["pitch"]
    c = pitch.clean({"pitch": 99, "grain": "junk"})
    assert c["pitch"] == 12 and c["grain"] == pitch.presets["deep"]["grain"]


def test_engine_switches_effect_with_crossfade(fake_sd):
    eng = realtime.LiveEngine(EFFECTS["ghost"].settings(EFFECTS["ghost"].presets["normal"]), 0, 2, SR, 256)
    eng.start()
    stream = fake_sd.streams[-1]
    x = np.full(256 * 20, 0.1, np.float32)
    out = stream.feed(x)
    assert isinstance(eng.filter, GhostFilter)
    np.testing.assert_allclose(out[-256:, 0], 0.1, atol=1e-6)

    eng.update(PitchSettings(semitones=0, shifted=0.0, original=1.0))  # another effect: swapped by the audio thread
    assert isinstance(eng.filter, GhostFilter)
    out = stream.feed(x)
    assert isinstance(eng.filter, PitchFilter) and eng._fade_from is None
    np.testing.assert_allclose(out[:, 0], 0.1, atol=1e-6)  # both sound the same here, so the fade is seamless
    eng.update(PitchSettings(semitones=3))  # same effect: updates in place
    assert isinstance(eng.filter, PitchFilter) and eng.filter.settings.semitones == 3


def test_hotkey_chords_survive_numlock_fake_shifts():
    t = ChordTracker()
    num1 = next(sc for sc, d in NUMPAD_SCANCODES.items() if d == 1)
    assert t.feed(0xA2, 0x1D, 0, True) == (None, False)  # Left Ctrl down
    assert t.feed(0xA0, 0x2A, 0, True) == (None, False)  # Left Shift down
    t.feed(0xA0, 0x2A, 0x01, False)  # driver's fake Shift release (extended), NumLock on
    assert t.feed(0x23, num1, 0, True) == (1, True)  # arrives as VK_END, recognised by scan code, swallowed
    t.feed(0xA0, 0x2A, 0x01, True)  # fake Shift press back
    assert t.feed(0x23, num1, 0, False) == (None, True)  # its key-up swallowed too
    # the real End key (extended) and numpad keys without both modifiers pass through
    assert t.feed(0x23, num1, 0x01, True) == (None, False)
    t.feed(0xA0, 0x2A, 0, False)  # real Shift release
    assert t.feed(0x61, num1, 0, True) == (None, False)
