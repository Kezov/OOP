"""Effects, favorites, saved presets and hotkeys in the app (fake audio driver)."""

import json
import os
import sys

import numpy as np
import pytest

tk = pytest.importorskip("tkinter")
if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
    pytest.skip("no display", allow_module_level=True)

from ghostvoice.ghost import GhostSettings  # noqa: E402
from ghostvoice.gui import App  # noqa: E402
from ghostvoice.pitch import PitchFilter, PitchSettings  # noqa: E402


def make_app(tmp_path, monkeypatch, cfg=None):
    path = tmp_path / "settings.json"
    if cfg is not None:
        path.write_text(json.dumps(cfg))
    monkeypatch.setattr("ghostvoice.gui.config_path", lambda: path)
    root = tk.Tk()
    a = App(root, persist=True, global_hotkeys=False)
    root.update()
    return a


@pytest.fixture
def app(fake_sd, monkeypatch, tmp_path):
    a = make_app(tmp_path, monkeypatch)
    yield a
    if a.engine is not None:
        a.stop()
    try:
        a.close()
    except tk.TclError:
        pass


def saved(tmp_path):
    return json.loads((tmp_path / "settings.json").read_text())


def test_switch_effect_rebuilds_sliders_and_remembers_each(app):
    app.select_preset("veil")
    app.effect_var.set("Pitch shift")
    app.on_effect()
    assert app.effect.key == "pitch" and set(app.vars) == {"pitch", "shifted", "original", "grain", "lowcut", "highcut"}
    assert app.preset_name == "deep" and isinstance(app.current_settings(), PitchSettings)
    assert len(app.slider_frame.winfo_children()) == 3 * 6
    app.select_preset("helium")
    app.set_effect("ghost")
    assert app.preset_name == "veil" and isinstance(app.current_settings(), GhostSettings)
    app.set_effect("pitch")
    assert app.preset_name == "helium"


def test_save_favorite_delete_custom_preset(app, tmp_path):
    app.set_effect("pitch")
    app.vars["pitch"].set(-7)
    app.on_slider("pitch")
    assert app.preset_name is None
    name, err = app.check_name("  my   villain ")
    assert (name, err) == ("my villain", None)
    app.save_preset(name)
    assert app.preset_var.get() == "my villain (custom)"
    assert saved(tmp_path)["custom_presets"]["pitch"]["my villain"]["pitch"] == -7

    app.toggle_favorite()
    assert app.preset_names()[0] == "my villain" and app.preset_var.get() == "★ my villain (custom)"
    assert "pitch:my villain" in saved(tmp_path)["favorites"]

    app.select_preset("demon")  # pick it again from the list by its display name
    app.preset_var.set("★ my villain (custom)")
    app.on_preset()
    assert app.preset_name == "my villain" and app.controls()["pitch"] == -7

    app.assign_hotkey(4)
    app.delete_preset("my villain")
    assert "my villain" not in app.custom["pitch"] and app.preset_name is None  # sliders kept, unsaved
    assert 4 not in app.hotkeys and "pitch:my villain" not in app.favorites
    assert saved(tmp_path)["custom_presets"]["pitch"] == {}


def test_built_in_presets_can_be_favorited_not_deleted_or_overwritten(app):
    assert app.check_name("mist")[1] is not None  # built-in name
    assert app.check_name("")[1] is not None
    assert app.check_name("custom (unsaved)")[1] is not None
    assert app.check_name("x" * 41)[1] is not None
    app.select_preset("breath")
    assert app.del_btn.instate(["disabled"])
    app.toggle_favorite()
    assert app.preset_names()[0] == "breath" and app.preset_var.get() == "★ breath"
    app.delete_preset("breath")
    assert "breath" in app.effect.presets


def test_hotkeys_switch_presets_across_effects_while_live(app, fake_sd, tmp_path):
    app.select_preset("mist")
    app.assign_hotkey(1)
    app.set_effect("pitch")
    app.select_preset("demon")
    app.assign_hotkey(2)
    assert saved(tmp_path)["hotkeys"] == {"1": ["ghost", "mist"], "2": ["pitch", "demon"]}
    assert "Num 2" in app.hotkey_list.cget("text") and "Pitch shift: demon" in app.hotkey_list.cget("text")

    app.toggle()
    stream = fake_sd.streams[-1]
    x = np.full(256 * 40, 0.05, np.float32)
    stream.feed(x)
    assert isinstance(app.engine.filter, PitchFilter)

    app._hotkey_queue.put(1)  # as the global hook thread does
    app._poll()
    assert app.effect.key == "ghost" and app.preset_name == "mist"
    stream.feed(x)
    assert not isinstance(app.engine.filter, PitchFilter) and app.engine._fade_from is None
    assert "Ghost voice: mist" in app.status.cget("text")

    app.trigger_hotkey(2)
    stream.feed(x)
    assert isinstance(app.engine.filter, PitchFilter) and app.preset_name == "demon"
    app.trigger_hotkey(7)  # unassigned: nothing happens
    assert app.preset_name == "demon"
    app.clear_hotkey(2)
    assert 2 not in app.hotkeys
    app.toggle()


def test_tk_fallback_keys():
    from ghostvoice.hotkeys import digit_from_keysym

    assert digit_from_keysym("KP_End") == 1 and digit_from_keysym("KP_7") == 7
    assert digit_from_keysym("End") is None


def test_everything_is_remembered(fake_sd, monkeypatch, tmp_path):
    a = make_app(tmp_path, monkeypatch)
    a.set_effect("pitch")
    a.vars["original"].set(55)
    a.on_slider("original")
    a.save_preset("choir")
    a.toggle_favorite()
    a.assign_hotkey(3)
    a.close()

    b = make_app(tmp_path, monkeypatch)
    try:
        assert b.effect.key == "pitch" and b.preset_name == "choir" and b.controls()["original"] == 55
        assert b.is_favorite("pitch", "choir") and b.hotkeys == {3: ("pitch", "choir")}
    finally:
        b.close()


def test_settings_from_older_versions_still_load(fake_sd, monkeypatch, tmp_path):
    old = {"input": "Microphone (Realtek Audio)", "output": "CABLE Input (VB-Audio Virtual C",
           "preset": "custom", "controls": {"voice": 40.0, "ghost": 90.0, "tail": 0.6, "decay": 2.0,
                                             "highpass": 300.0, "lowpass": 8000.0}}
    a = make_app(tmp_path, monkeypatch, old)
    try:
        assert a.effect.key == "ghost" and a.preset_name is None
        assert a.controls()["ghost"] == 90 and a.controls()["tail"] == pytest.approx(0.6)
    finally:
        a.close()


def test_broken_settings_file_falls_back_to_defaults(fake_sd, monkeypatch, tmp_path):
    bad = {"effect": "nope", "effects": [], "custom_presets": {"pitch": {"x": "bad", "deep": {}}},
           "favorites": "x", "hotkeys": {"11": ["ghost", "mist"], "2": ["pitch", "missing"]}}
    a = make_app(tmp_path, monkeypatch, bad)
    try:
        assert a.effect.key == "ghost" and a.preset_name == "mist" and a.hotkeys == {}
        assert a.custom["pitch"] == {}
    finally:
        a.close()
