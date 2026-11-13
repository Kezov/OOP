"""Command line interface: `ghostvoice gui | devices | run | file`."""

from __future__ import annotations

import argparse
from dataclasses import replace

import numpy as np

from .ghost import GhostSettings, levels, reverse_reverb_offline, with_levels
from .presets import DEFAULT_PRESET, PRESETS


def _add_effect_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("effect")
    g.add_argument("--preset", choices=PRESETS, default=DEFAULT_PRESET, help=f"starting point (default: {DEFAULT_PRESET})")
    g.add_argument("--voice", type=float, help="voice volume in %% (100 = unchanged)")
    g.add_argument("--ghost", type=float, help="ghostly wash volume in %% of the voice level")
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
    s = replace(s, **overrides)
    if args.voice is not None or args.ghost is not None:
        voice, ghost = levels(s)
        s = with_levels(
            s,
            voice if args.voice is None else args.voice / 100,
            ghost if args.ghost is None else args.ghost / 100,
        )
    return s


def cmd_devices(args) -> None:
    from .realtime import list_devices

    print(list_devices())


def cmd_run(args) -> None:
    from .realtime import run

    run(settings_from_args(args), args.input, args.output, args.samplerate, args.block, latency=args.latency)


def cmd_gui(args) -> None:
    from .gui import main as gui_main

    raise SystemExit(gui_main(["--selftest"] if args.selftest else []))


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
    p.add_argument("--latency", type=float, default=0.06, help="audio buffering in seconds (default 0.06)")
    _add_effect_args(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("gui", help="open the app window")
    p.add_argument("--selftest", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser("file", help="apply the effect to an audio file")
    p.add_argument("infile")
    p.add_argument("outfile")
    _add_effect_args(p)
    p.set_defaults(func=cmd_file)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
