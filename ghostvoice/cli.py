"""Command line interface: `ghostvoice devices | run | file`."""

from __future__ import annotations

import argparse
from dataclasses import replace

import numpy as np

from .ghost import GhostSettings, reverse_reverb_offline

PRESETS = {
    "ghost": GhostSettings(),
    "whisper": GhostSettings(tail=0.3, decay=0.8, mix=0.6, highpass=400.0),
    "phantom": GhostSettings(tail=0.7, decay=2.0, mix=0.8, highpass=200.0, lowpass=7000.0),
    "banshee": GhostSettings(tail=0.6, decay=1.5, mix=0.9, highpass=900.0, lowpass=12000.0),
}


def _add_effect_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("effect")
    g.add_argument("--preset", choices=PRESETS, default="ghost", help="starting point (default: ghost)")
    g.add_argument("--tail", type=float, help="swell length in seconds; also the live delay")
    g.add_argument("--decay", type=float, help="reverb decay time (RT60) in seconds")
    g.add_argument("--mix", type=float, help="0 = dry voice, 1 = only the ghostly wash")
    g.add_argument("--highpass", type=float, help="cut wash below this Hz (0 = off)")
    g.add_argument("--lowpass", type=float, help="cut wash above this Hz (0 = off)")
    g.add_argument("--gain", type=float, dest="gain_db", help="output gain in dB")
    g.add_argument("--ir", dest="ir_path", help="use a reverb impulse response WAV instead of the built-in one")


def settings_from_args(args: argparse.Namespace) -> GhostSettings:
    s = PRESETS[args.preset]
    overrides = {
        k: getattr(args, k)
        for k in ("tail", "decay", "mix", "highpass", "lowpass", "gain_db", "ir_path")
        if getattr(args, k) is not None
    }
    for k in ("highpass", "lowpass"):
        if overrides.get(k) == 0:
            overrides[k] = None
    return replace(s, **overrides)


def cmd_devices(args) -> None:
    from .realtime import list_devices

    print(list_devices())


def cmd_run(args) -> None:
    from .realtime import run

    run(settings_from_args(args), args.input, args.output, args.samplerate, args.block)


def cmd_file(args) -> None:
    import soundfile as sf

    data, sr = sf.read(args.infile, always_2d=True, dtype="float64")
    out = reverse_reverb_offline(data.mean(axis=1), sr, settings_from_args(args))
    sf.write(args.outfile, out.astype(np.float32), sr)
    print(f"Wrote {args.outfile} ({len(out) / sr:.2f} s)")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ghostvoice", description="Ghostly reverse-reverb voice filter for Discord.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("devices", help="list audio devices")
    p.set_defaults(func=cmd_devices)

    p = sub.add_parser("run", help="filter your mic live into a virtual audio cable")
    p.add_argument("-i", "--input", help="mic device (index or part of its name); default = system mic")
    p.add_argument("-o", "--output", help="output device, e.g. 'CABLE Input' or 'BlackHole'; default = speakers")
    p.add_argument("--samplerate", type=int, default=48000)
    p.add_argument("--block", type=int, default=256, help="audio block size in samples (default 256)")
    _add_effect_args(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("file", help="apply the effect to an audio file")
    p.add_argument("infile")
    p.add_argument("outfile")
    _add_effect_args(p)
    p.set_defaults(func=cmd_file)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
