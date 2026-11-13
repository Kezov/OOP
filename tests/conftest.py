"""A stand-in for the `sounddevice` module, so live audio code runs without sound hardware."""

from types import SimpleNamespace

import numpy as np
import pytest


class FakeStream:
    def __init__(self, sd, **kw):
        if kw["samplerate"] not in sd.supported_rates:
            raise sd.PortAudioError(f"Invalid sample rate {kw['samplerate']}")
        self.kw = kw
        self.callback = kw["callback"]
        self.started = self.closed = False
        sd.streams.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True

    def feed(self, x, status_at=()):
        """Push mono input through the callback block by block; returns (frames, channels) output."""
        b = self.kw["blocksize"]
        ch_in, ch_out = self.kw["channels"]
        outs = []
        for n, i in enumerate(range(0, x.size, b)):
            indata = np.repeat(x[i : i + b, None], ch_in, axis=1).astype(np.float32)
            outdata = np.zeros((b, ch_out), np.float32)
            self.callback(indata, outdata, b, None, "input overflow" if n in status_at else None)
            outs.append(outdata)
        return np.concatenate(outs)


class FakeSD:
    def __init__(self, devices, default=(0, 1), supported_rates=(48000,)):
        self.PortAudioError = type("PortAudioError", (Exception,), {})
        self.devices = devices
        self.default = SimpleNamespace(device=list(default))
        self.supported_rates = set(supported_rates)
        self.streams = []
        self.played = []

    def query_devices(self, device=None, kind=None):
        if device is None and kind is None:
            return list(self.devices)
        if device is None:
            device = self.default.device[0 if kind == "input" else 1]
        if device < 0:
            raise self.PortAudioError("no default device")
        return self.devices[device]

    def Stream(self, **kw):
        return FakeStream(self, **kw)

    def rec(self, frames, samplerate, channels, device, dtype):
        t = np.arange(frames) / samplerate
        return (0.3 * np.sin(2 * np.pi * 220 * t))[:, None].astype(np.float32)

    def wait(self):
        pass

    def play(self, data, samplerate, device=None):
        self.played.append((np.asarray(data), samplerate, device))

    def stop(self):
        pass


def _dev(name, ins, outs, api=0, rate=48000.0):
    return {"name": name, "max_input_channels": ins, "max_output_channels": outs,
            "hostapi": api, "default_samplerate": rate}


WINDOWS_LIKE = [
    _dev("Microphone (Realtek Audio)", 2, 0),
    _dev("Speakers (Realtek Audio)", 0, 2),
    _dev("CABLE Input (VB-Audio Virtual C", 0, 8),
    _dev("CABLE Output (VB-Audio Virtual ", 8, 0),
    _dev("Microphone (Realtek Audio)", 2, 0, api=1),  # same devices again on another host API
    _dev("CABLE Input (VB-Audio Virtual Cable)", 0, 8, api=1),
]


CABLE_IS_DEFAULT = [  # Windows MME right after installing VB-CABLE: the cable became the default
    _dev("Microsoft Sound Mapper - Input", 2, 0),
    _dev("CABLE Output (VB-Audio Virtual ", 16, 0),
    _dev("HX Microphone (HyperX Cloud II ", 1, 0),
    _dev("Microsoft Sound Mapper - Output", 0, 2),
    _dev("CABLE Input (VB-Audio Virtual C", 0, 16),
    _dev("CABLE In 16ch (VB-Audio Virtual", 0, 16),
    _dev("Speakers (HyperX Cloud II Wirel", 0, 2),
]


@pytest.fixture
def fake_sd(monkeypatch):
    from ghostvoice import realtime

    sd = FakeSD(WINDOWS_LIKE, default=(0, 1))
    monkeypatch.setattr(realtime, "_sd", lambda: sd)
    return sd
