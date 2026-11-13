import threading

import numpy as np
import pytest

from ghostvoice import GhostFilter
from ghostvoice import realtime
from ghostvoice.presets import PRESETS


def test_device_choices_one_entry_per_device(fake_sd):
    ins, outs, din, dout = realtime.device_choices()
    assert [i for i, _ in ins] == [0, 3]  # host API 1 duplicates are hidden
    assert [i for i, _ in outs] == [1, 2]
    assert (din, dout) == (0, 1)


def test_cable_detection_and_discord_names():
    assert realtime.is_cable("CABLE Input (VB-Audio Virtual C")
    assert realtime.is_cable("BlackHole 2ch")
    assert not realtime.is_cable("Speakers (Realtek Audio)")
    assert realtime.discord_input_name("CABLE Input (VB-Audio Virtual C") == "CABLE Output"
    assert realtime.discord_input_name("BlackHole 2ch") == "BlackHole 2ch"


def test_engine_streams_the_ghost_filter(fake_sd):
    s = PRESETS["mist"]
    eng = realtime.LiveEngine(s, 0, 2, 48000, 256)
    eng.start()
    stream = fake_sd.streams[-1]
    assert stream.started and stream.kw["device"] == (0, 2) and stream.kw["channels"] == (1, 2)

    x = (np.random.default_rng(5).standard_normal(256 * 300) * 0.1).astype(np.float32)
    out = stream.feed(x, status_at=(7, 9))
    ref = GhostFilter(48000, s, 256).process(x.astype(np.float64))
    np.testing.assert_allclose(out[:, 0], ref, atol=1e-6)
    np.testing.assert_array_equal(out[:, 0], out[:, 1])
    assert eng.dropouts == 2
    lin, lout = eng.take_levels()
    assert lin == pytest.approx(np.abs(x).max()) and 0 < lout <= 1
    assert eng.take_levels() == (0.0, 0.0)

    eng.stop()
    assert stream.closed and not eng.running


def test_engine_falls_back_to_device_sample_rate(fake_sd):
    fake_sd.supported_rates = {44100}
    fake_sd.devices[2] = {**fake_sd.devices[2], "default_samplerate": 44100.0}
    eng = realtime.LiveEngine(PRESETS["mist"], 0, 2)
    eng.start()
    assert eng.sample_rate == 44100 and eng.filter.sample_rate == 44100


def test_engine_reports_unusable_devices(fake_sd):
    fake_sd.supported_rates = set()
    with pytest.raises(RuntimeError, match="Could not open"):
        realtime.LiveEngine(PRESETS["mist"], 0, 2).start()


def test_run_cli_loop(fake_sd, capsys):
    stop = threading.Event()
    stop.set()
    realtime.run(PRESETS["mist"], "microphone", "cable input", stop_event=stop)
    out = capsys.readouterr().out
    assert "CABLE Input" in out and "Ghost delay: 450 ms" in out and "Stopped." in out
    assert fake_sd.streams[-1].closed


PACTL = {
    ("list", "sources"): "Source #1\n\tName: alsa_output.monitor\n\tDescription: Monitor of Speakers\n"
                         "Source #2\n\tName: alsa_input.mic\n\tDescription: Built-in Mic\n"
                         "Source #4\n\tName: ghostmic\n\tDescription: GhostMic\n",
    ("list", "sinks"): "Sink #1\n\tName: alsa_output.speakers\n\tDescription: Speakers\n"
                       "Sink #2\n\tName: ghostvoice\n\tDescription: ghostvoice\n",
    ("info",): "Server Name: pulseaudio\nDefault Sink: alsa_output.speakers\nDefault Source: alsa_input.mic\n",
}


@pytest.fixture
def linux_pulse(fake_sd, monkeypatch):
    fake_sd.devices[:] = [
        {"name": "HDA Intel: ALC (hw:0,0)", "max_input_channels": 2, "max_output_channels": 2, "hostapi": 0,
         "default_samplerate": 48000.0},
        {"name": "pulse", "max_input_channels": 32, "max_output_channels": 32, "hostapi": 0,
         "default_samplerate": 44100.0},
        {"name": "default", "max_input_channels": 32, "max_output_channels": 32, "hostapi": 0,
         "default_samplerate": 44100.0},
    ]
    monkeypatch.setattr(realtime.sys, "platform", "linux")
    monkeypatch.setattr(realtime.shutil, "which", lambda name: "/usr/bin/pactl")
    monkeypatch.setattr(realtime, "_pactl", lambda *a: PACTL[a])
    return fake_sd


def test_linux_lists_pulse_devices(linux_pulse):
    ins, outs, din, dout = realtime.device_choices()
    assert [n for _, n in ins] == ["Built-in Mic", "GhostMic"]  # monitors hidden
    assert [n for _, n in outs] == ["Speakers", "ghostvoice"]
    assert din == realtime.PulseDevice(1, "alsa_input.mic", "input")
    assert dout.name == "alsa_output.speakers"
    assert realtime.resolve_device("ghostvoice", "output") == realtime.PulseDevice(1, "ghostvoice", "output")


def test_linux_engine_routes_through_pulse_env(linux_pulse, monkeypatch):
    import os

    monkeypatch.delenv("PULSE_SINK", raising=False)
    seen = {}
    original = linux_pulse.Stream

    def stream(**kw):
        seen.update(sink=os.environ.get("PULSE_SINK"), source=os.environ.get("PULSE_SOURCE"), device=kw["device"])
        return original(**kw)

    linux_pulse.Stream = stream
    eng = realtime.LiveEngine(PRESETS["mist"], realtime.PulseDevice(1, "alsa_input.mic", "input"),
                              realtime.PulseDevice(1, "ghostvoice", "output"))
    eng.start()
    assert seen == {"sink": "ghostvoice", "source": "alsa_input.mic", "device": (1, 1)}
    assert "PULSE_SINK" not in os.environ  # restored after opening
    assert eng.output_name == "ghostvoice"
