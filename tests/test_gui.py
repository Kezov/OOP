"""Drive the app window with a fake audio driver (skipped where Tk or a display is missing)."""

import os
import sys

import numpy as np
import pytest

tk = pytest.importorskip("tkinter")
if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
    pytest.skip("no display", allow_module_level=True)

from ghostvoice import levels  # noqa: E402
from ghostvoice.gui import App, selftest  # noqa: E402
from ghostvoice.presets import PRESETS  # noqa: E402


@pytest.fixture
def app(fake_sd, monkeypatch, tmp_path):
    monkeypatch.setattr("ghostvoice.gui.config_path", lambda: tmp_path / "settings.json")
    root = tk.Tk()
    a = App(root, persist=True)
    root.update()
    yield a
    if a.engine is not None:
        a.stop()
    try:
        a.close()
    except tk.TclError:
        pass


def test_defaults(app):
    assert app.in_var.get() == "Microphone (Realtek Audio)"
    assert app.out_var.get().startswith("CABLE Input")  # virtual cable picked automatically
    assert app.preset_var.get() == "mist"
    assert levels(app.current_settings()) == pytest.approx(levels(PRESETS["mist"]), abs=0.01)
    assert "CABLE Output" in app.discord_tip.cget("text")


def test_preset_and_sliders(app):
    app.preset_var.set("breath")
    app.on_preset()
    assert app.current_settings().highpass == 1200.0
    app.vars["ghost"].set(10)
    app.on_slider("ghost")
    assert app.preset_var.get() == "custom"
    assert levels(app.current_settings())[1] == pytest.approx(0.10)


def test_switch_to_normal_while_live(app, fake_sd):
    app.toggle()
    stream = fake_sd.streams[-1]
    x = np.full(256 * 40, 0.1, np.float32)
    stream.feed(x)
    app.preset_var.set("normal")
    app.on_preset()
    app.apply()
    assert app.engine.filter.latency_samples == 0
    assert "voice delay 0 ms" in app.status.cget("text")
    out = stream.feed(x)  # crossfades from the ghost kernel to the plain voice
    np.testing.assert_allclose(out[-256:, 0], 0.1, atol=1e-6)  # then just the voice
    app.toggle()


def test_live_start_update_stop(app, fake_sd):
    app.toggle()
    assert app.engine is not None and app.engine.running
    stream = fake_sd.streams[-1]
    app.vars["tail"].set(0.3)
    app.on_slider("tail")
    app.apply()
    assert app.engine.filter.latency_samples == int(0.3 * 48000) - 1
    out = stream.feed(np.full(256 * 4, 0.1, np.float32))
    assert np.isfinite(out).all()
    app._poll()
    assert "On" in app.status.cget("text")
    app.toggle()
    assert app.engine is None and stream.closed


def test_record_and_play(app, fake_sd):
    app.record_test()
    for _ in range(100):
        app.root.update()
        app._check_recording() if app._rec_result is not None else None
        if app.recording is not None:
            break
    assert app.recording is not None
    app.play_test(True)
    app.play_test(False)
    ghost, orig = fake_sd.played[-2][0], fake_sd.played[-1][0]
    assert ghost.size > orig.size  # ghost version starts with the swell


def test_record_and_play_on_normal_speakers(app, fake_sd):
    app.recording = (np.zeros(4800), 48000)
    app.play_test(False)
    assert fake_sd.played[-1][2] is None  # default output is the speakers: play there
    assert "Windows is sending all its sound" not in app.discord_tip.cget("text")


def test_cable_as_windows_default(fake_sd, monkeypatch, tmp_path):
    """Installing VB-CABLE made it the Windows default mic and speakers."""
    from conftest import CABLE_IS_DEFAULT

    fake_sd.devices = CABLE_IS_DEFAULT
    fake_sd.default.device = [1, 4]
    monkeypatch.setattr("ghostvoice.gui.config_path", lambda: tmp_path / "settings.json")
    root = tk.Tk()
    try:
        a = App(root, persist=False)
        # not the cable, and not "Sound Mapper", which follows the default to the cable
        assert a.in_var.get() == "HX Microphone (HyperX Cloud II "
        assert a.out_var.get() == "CABLE Input (VB-Audio Virtual C"
        assert "Windows is sending all its sound" in a.discord_tip.cget("text")
        a.recording = (np.zeros(4800), 48000)
        a.play_test(True)
        assert fake_sd.played[-1][2] == 6  # the real speakers, not the cable
        a.out_var.set("CABLE In 16ch (VB-Audio Virtual")
        a.on_device_change()
        assert "“CABLE Output”" in a.discord_tip.cget("text")
    finally:
        root.destroy()


def test_warns_when_mic_is_silent(app, fake_sd):
    app.toggle()
    stream = fake_sd.streams[-1]
    for _ in range(45):
        stream.feed(np.zeros(256, np.float32))
        app._poll()
    assert "muted" in app.status.cget("text")
    stream.feed(np.full(256, 0.01, np.float32))
    app._poll()
    assert "muted" not in app.status.cget("text")
    app.toggle()


def test_settings_are_remembered(app, tmp_path):
    app.preset_var.set("veil")
    app.on_preset()
    app.close()
    import json

    saved = json.loads((tmp_path / "settings.json").read_text())
    assert saved["preset"] == "veil" and saved["output"].startswith("CABLE Input")


def test_selftest(fake_sd):
    try:
        import sounddevice  # noqa: F401
    except Exception:
        # OSError: PortAudio library missing. PortAudioError: the library
        # loads but can't initialize (e.g. no audio server on a CI runner).
        pytest.skip("PortAudio not available")
    assert selftest() == 0
