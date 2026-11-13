"""Live audio: microphone -> ghost filter -> virtual audio cable (-> Discord)."""

from __future__ import annotations

import threading

import numpy as np

from .ghost import GhostFilter, GhostSettings


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
        raise SystemExit(f"No {kind} device matching {spec!r}. Run `ghostvoice devices` to see the list.")
    return matches[0]


def run(
    settings: GhostSettings,
    input_device: str | None = None,
    output_device: str | None = None,
    sample_rate: int = 48000,
    block_size: int = 256,
    stop_event: threading.Event | None = None,
) -> None:
    sd = _sd()
    in_dev = resolve_device(input_device, "input")
    out_dev = resolve_device(output_device, "output")
    out_info = sd.query_devices(out_dev, "output")
    in_info = sd.query_devices(in_dev, "input")
    out_channels = min(2, int(out_info["max_output_channels"]))

    ghost = GhostFilter(sample_rate, settings, block_size)
    problems = {"count": 0}

    def callback(indata, outdata, frames, time, status):
        if status:
            problems["count"] += 1
        y = ghost.process(indata[:, 0])
        outdata[:] = y[:, None].astype(np.float32)

    print(f"Mic:    {in_info['name']}")
    print(f"Output: {out_info['name']}  ({out_channels} ch)")
    print(f"Ghost delay: {ghost.latency_seconds * 1000:.0f} ms + audio buffering")
    print("Running. Press Ctrl+C to stop.")

    stop_event = stop_event or threading.Event()
    with sd.Stream(
        device=(in_dev, out_dev),
        samplerate=sample_rate,
        blocksize=block_size,
        channels=(1, out_channels),
        dtype="float32",
        latency="low",
        callback=callback,
    ):
        try:
            while not stop_event.wait(2.0):
                if problems["count"]:
                    print(f"warning: {problems['count']} audio dropouts (try a larger --block)")
                    problems["count"] = 0
        except KeyboardInterrupt:
            pass
    print("Stopped.")
