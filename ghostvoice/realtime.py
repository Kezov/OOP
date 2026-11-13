"""Live audio: microphone -> ghost filter -> virtual audio cable (-> Discord)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from contextlib import contextmanager
from typing import NamedTuple

import numpy as np

from .ghost import GhostFilter, GhostSettings

CABLE_HINTS = ("cable input", "blackhole", "ghostvoice", "voicemeeter input", "vb-audio", "loopback")
"""Name fragments of virtual audio cables' playback side."""


def _sd():
    try:
        import sounddevice as sd
    except OSError as e:  # PortAudio missing
        raise SystemExit(
            f"Could not load the audio library ({e}).\n"
            "On Linux install PortAudio, e.g. `sudo apt install libportaudio2`."
        ) from e
    return sd


def list_devices() -> str:
    return str(_sd().query_devices())


def is_cable(name: str) -> bool:
    n = name.lower()
    return any(h in n for h in CABLE_HINTS)


def discord_input_name(output_name: str) -> str:
    """What the cable is called on the Discord (input) side."""
    n = output_name.lower()
    if "cable input" in n:
        return output_name[: n.index("cable input")] + "CABLE Output"
    if "voicemeeter input" in n:
        return "VoiceMeeter Output"
    if "ghostvoice" in n:
        return "GhostMic"
    return output_name


class PulseDevice(NamedTuple):
    """A PulseAudio/PipeWire device, reached through PortAudio's ALSA "pulse" device.

    On Linux PortAudio only sees ALSA's generic "pulse"/"default" devices, not
    the individual sinks and sources, so we pick one via PULSE_SINK/PULSE_SOURCE.
    """

    index: int  # PortAudio index of the ALSA "pulse" device
    name: str  # PulseAudio sink/source name
    kind: str  # "input" or "output"


def _pactl(*args: str) -> str:
    return subprocess.run(
        ["pactl", *args], capture_output=True, text=True, timeout=5,
        env={**os.environ, "LC_ALL": "C"}, check=True,
    ).stdout


def _parse_pactl_list(text: str) -> list[tuple[str, str]]:
    """``pactl list sinks|sources`` -> [(name, description)]."""
    found, name = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Name: "):
            name = line[6:]
        elif line.startswith("Description: ") and name is not None:
            found.append((name, line[13:]))
            name = None
    return found


def _pulse_choices(devices):
    """Pulse sinks/sources as device choices, or None if not applicable."""
    if not sys.platform.startswith("linux") or not shutil.which("pactl"):
        return None
    idx = next((i for i, d in enumerate(devices) if d["name"] == "pulse"), None)
    if idx is None:
        return None
    try:
        sources = _parse_pactl_list(_pactl("list", "sources"))
        sinks = _parse_pactl_list(_pactl("list", "sinks"))
        info = _pactl("info")
    except Exception:
        return None
    defaults = dict(
        line.split(": ", 1) for line in info.splitlines() if line.startswith(("Default Sink: ", "Default Source: "))
    )
    ins = [(PulseDevice(idx, n, "input"), d) for n, d in sources if not n.endswith(".monitor")]
    outs = [(PulseDevice(idx, n, "output"), d) for n, d in sinks]
    din = next((dev for dev, _ in ins if dev.name == defaults.get("Default Source")), -1)
    dout = next((dev for dev, _ in outs if dev.name == defaults.get("Default Sink")), -1)
    return ins, outs, din, dout


@contextmanager
def opened(device):
    """Yield the PortAudio device for a choice, routing PulseDevices via the environment."""
    if not isinstance(device, PulseDevice):
        yield device
        return
    var = "PULSE_SOURCE" if device.kind == "input" else "PULSE_SINK"
    old = os.environ.get(var)
    os.environ[var] = device.name
    try:
        yield device.index
    finally:
        if old is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = old


def _default_hostapi(sd):
    for kind in ("input", "output"):
        try:
            return sd.query_devices(kind=kind).get("hostapi")
        except Exception:
            continue
    return None


def device_choices():
    """Usable devices on the default audio system (one entry per device).

    Returns ``(inputs, outputs, default_input, default_output)`` where inputs and
    outputs are lists of ``(device, name)``. Sticking to one host API avoids the
    duplicates Windows lists (MME, DirectSound, WASAPI...) and guarantees the
    input and output can be opened together. On Linux with PulseAudio/PipeWire
    the devices are :class:`PulseDevice` entries.
    """
    sd = _sd()
    devices = list(sd.query_devices())
    pulse = _pulse_choices(devices)
    if pulse is not None:
        return pulse
    api = _default_hostapi(sd)

    def pick(key):
        return [
            (i, d["name"]) for i, d in enumerate(devices)
            if d[key] > 0 and (api is None or d.get("hostapi", api) == api)
        ]

    try:
        din, dout = (int(i) for i in sd.default.device)
    except Exception:
        din = dout = -1
    return pick("max_input_channels"), pick("max_output_channels"), din, dout


def resolve_device(spec: str | None, kind: str):
    """Accept a device index, a name substring, or None for the system default."""
    if spec is None:
        return None
    if spec.isdigit():
        return int(spec)
    sd = _sd()
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    matches = [
        i for i, d in enumerate(sd.query_devices())
        if d[key] > 0 and spec.lower() in d["name"].lower()
    ]
    if not matches:
        pulse = _pulse_choices(list(sd.query_devices()))
        for dev, desc in (pulse[0] if kind == "input" else pulse[1]) if pulse else []:
            if spec.lower() in dev.name.lower() or spec.lower() in desc.lower():
                return dev
        raise SystemExit(f"No {kind} device matching {spec!r}. Run `ghostvoice devices` to see the list.")
    return matches[0]


class LiveEngine:
    """Runs the ghost filter between an input and an output device.

    ``latency`` is the audio buffering requested from the driver. The ghost
    effect already delays the voice by its swell length, so a little extra
    buffering (60 ms) costs almost nothing and avoids dropouts, which on
    PulseAudio can silence the output for seconds.
    """

    def __init__(
        self,
        settings: GhostSettings,
        input_device=None,
        output_device=None,
        sample_rate: int = 48000,
        block_size: int = 256,
        latency: float | str = 0.06,
    ):
        self.settings = settings
        self.latency = latency
        self.input_device = input_device
        self.output_device = output_device
        self.sample_rate = sample_rate
        self.block_size = block_size
        self.filter: GhostFilter | None = None
        self.input_name = self.output_name = ""
        self.out_channels = 0
        self.dropouts = 0
        self._stream = None
        self._in_peak = 0.0
        self._out_peak = 0.0

    @property
    def running(self) -> bool:
        return self._stream is not None

    @property
    def latency_seconds(self) -> float:
        return self.filter.latency_seconds if self.filter else 0.0

    def start(self) -> None:
        with opened(self.input_device) as i, opened(self.output_device) as o:
            self._start(i, o)

    def _start(self, input_device, output_device) -> None:
        sd = _sd()
        in_info = sd.query_devices(input_device, "input")
        out_info = sd.query_devices(output_device, "output")
        self.input_name, self.output_name = in_info["name"], out_info["name"]
        for dev in (self.input_device, self.output_device):
            if isinstance(dev, PulseDevice):
                setattr(self, f"{dev.kind}_name", dev.name)
        self.out_channels = max(1, min(2, int(out_info["max_output_channels"])))
        rates = [self.sample_rate]
        for info in (out_info, in_info):
            r = int(info.get("default_samplerate") or 0)
            if r and r not in rates:
                rates.append(r)
        error = None
        for rate in rates:  # some devices refuse 48 kHz; fall back to their own rate
            try:
                self.filter = GhostFilter(rate, self.settings, self.block_size)
                stream = sd.Stream(
                    device=(input_device, output_device),
                    samplerate=rate,
                    blocksize=self.block_size,
                    channels=(1, self.out_channels),
                    dtype="float32",
                    latency=self.latency,
                    callback=self._callback,
                )
                stream.start()
            except Exception as e:  # PortAudioError, invalid sample rate, ...
                error = e
                continue
            self.sample_rate = rate
            self._stream = stream
            return
        self.filter = None
        raise RuntimeError(f"Could not open {self.input_name!r} -> {self.output_name!r}: {error}")

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.stop()
            stream.close()

    def update(self, settings: GhostSettings) -> None:
        self.settings = settings
        if self.filter is not None:
            self.filter.update(settings)

    def take_levels(self) -> tuple[float, float]:
        """Peak input and output level since the last call."""
        levels = (self._in_peak, self._out_peak)
        self._in_peak = self._out_peak = 0.0
        return levels

    def _callback(self, indata, outdata, frames, time, status):
        if status:
            self.dropouts += 1
        x = indata[:, 0]
        y = self.filter.process(x)
        outdata[:] = y[:, None].astype(np.float32)
        self._in_peak = max(self._in_peak, float(np.abs(x).max()))
        self._out_peak = max(self._out_peak, float(np.abs(y).max()))


def run(
    settings: GhostSettings,
    input_device: str | None = None,
    output_device: str | None = None,
    sample_rate: int = 48000,
    block_size: int = 256,
    stop_event: threading.Event | None = None,
    latency: float | str = 0.06,
) -> None:
    engine = LiveEngine(
        settings,
        resolve_device(input_device, "input"),
        resolve_device(output_device, "output"),
        sample_rate,
        block_size,
        latency,
    )
    try:
        engine.start()
    except RuntimeError as e:
        raise SystemExit(str(e)) from e

    print(f"Mic:    {engine.input_name}")
    print(f"Output: {engine.output_name}  ({engine.out_channels} ch, {engine.sample_rate} Hz)")
    print(f"Ghost delay: {engine.latency_seconds * 1000:.0f} ms + audio buffering")
    print("Running. Press Ctrl+C to stop.")

    stop_event = stop_event or threading.Event()
    reported = 0
    try:
        while not stop_event.wait(2.0):
            if engine.dropouts > reported:
                print(f"warning: {engine.dropouts - reported} audio dropouts (try a larger --latency)")
                reported = engine.dropouts
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop()
    print("Stopped.")
