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
