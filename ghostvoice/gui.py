"""Desktop app: pick devices, tune the ghost, start/stop, test without Discord."""

from __future__ import annotations

import json
import os
import platform
import sys
import threading
import traceback
import webbrowser
from pathlib import Path

import numpy as np

from . import __version__
from .ghost import GhostFilter, reverse_reverb_offline
from .presets import DEFAULT_PRESET, LOWPASS_OFF, NOTES, PRESETS, from_controls, to_controls

SAMPLE_RATE = 48000
BLOCK_SIZE = 256
TEST_SECONDS = 5
CUSTOM = "custom"
REPO_URL = "https://github.com/Kezov/OOP#hooking-it-up-to-discord"

# key, label, min, max, step, value formatter
SLIDERS = [
    ("voice", "Voice volume", 0, 150, 1, lambda v: f"{v:.0f}%"),
    ("ghost", "Ghost volume", 0, 150, 1, lambda v: f"{v:.0f}%"),
    ("tail", "Swell length (= delay)", 0.1, 1.0, 0.05, lambda v: f"{v:.2f} s"),
    ("decay", "Reverb decay", 0.3, 4.0, 0.1, lambda v: f"{v:.1f} s"),
    ("highpass", "Ghost low cut", 0, 2000, 50, lambda v: "off" if v <= 0 else f"{v:.0f} Hz"),
    ("lowpass", "Ghost high cut", 2000, LOWPASS_OFF, 500, lambda v: "off" if v >= LOWPASS_OFF else f"{v / 1000:.1f} kHz"),
]


def config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home())
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "ghostvoice" / "settings.json"


def cable_download_url() -> str:
    if sys.platform == "win32":
        return "https://vb-audio.com/Cable/"
    if sys.platform == "darwin":
        return "https://existential.audio/blackhole/"
    return REPO_URL


class App:
    def __init__(self, root, persist: bool = True):
        import tkinter as tk
        from tkinter import ttk

        self.tk, self.ttk = tk, ttk
        self.root = root
        self.persist = persist
        self.cfg = self._load_config() if persist else {}
        self.engine = None
        self.inputs: list[tuple[int, str]] = []
        self.outputs: list[tuple[int, str]] = []
        self.audio_error: str | None = None
        self.recording: tuple[np.ndarray, int] | None = None
        self._rec_result = None
        self._rec_left = 0
        self._apply_job = None
        self._loading = False
        self._meter = [0.0, 0.0]

        root.title(f"GhostVoice {__version__}")
        root.resizable(True, False)
        self._build()
        self.refresh_devices(initial=True)
        self._load_controls()
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._poll()

    # ---------------------------------------------------------------- layout
    def _build(self) -> None:
        tk, ttk = self.tk, self.ttk
        root = self.root
        root.columnconfigure(0, weight=1)
        main = ttk.Frame(root, padding=12)
        main.grid(sticky="nsew")
        main.columnconfigure(0, weight=1)

        # 1. devices
        dev = ttk.LabelFrame(main, text=" 1  Devices ", padding=8)
        dev.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        dev.columnconfigure(1, weight=1)
        ttk.Label(dev, text="Microphone").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.in_var = tk.StringVar()
        self.in_box = ttk.Combobox(dev, textvariable=self.in_var, state="readonly", width=44)
        self.in_box.grid(row=0, column=1, sticky="ew", pady=2)
        ttk.Button(dev, text="Refresh", command=self.refresh_devices).grid(row=0, column=2, padx=(6, 0))
        ttk.Label(dev, text="Send ghost voice to").grid(row=1, column=0, sticky="w", padx=(0, 8))
        self.out_var = tk.StringVar()
        self.out_box = ttk.Combobox(dev, textvariable=self.out_var, state="readonly", width=44)
        self.out_box.grid(row=1, column=1, sticky="ew", pady=2)
        self.cable_hint = ttk.Label(dev, text="", wraplength=470, justify="left")
        self.cable_hint.grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.cable_btn = ttk.Button(dev, text="Get a virtual cable…", command=lambda: webbrowser.open(cable_download_url()))
        self.cable_btn.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.in_box.bind("<<ComboboxSelected>>", lambda e: self.on_device_change())
        self.out_box.bind("<<ComboboxSelected>>", lambda e: self.on_device_change())

        # 2. sound
        snd = ttk.LabelFrame(main, text=" 2  Sound ", padding=8)
        snd.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        snd.columnconfigure(1, weight=1)
        ttk.Label(snd, text="Preset").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.preset_var = tk.StringVar(value=DEFAULT_PRESET)
        self.preset_box = ttk.Combobox(snd, textvariable=self.preset_var, values=list(PRESETS), state="readonly", width=14)
        self.preset_box.grid(row=0, column=1, sticky="w", pady=2)
        self.preset_box.bind("<<ComboboxSelected>>", lambda e: self.on_preset())
        self.note = ttk.Label(snd, text="", foreground="#555", wraplength=470, justify="left")
        self.note.grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 6))
        self.vars: dict[str, tk.DoubleVar] = {}
        self.value_labels: dict[str, ttk.Label] = {}
        for r, (key, label, lo, hi, _step, _fmt) in enumerate(SLIDERS, start=2):
            ttk.Label(snd, text=label).grid(row=r, column=0, sticky="w", padx=(0, 8))
            var = tk.DoubleVar(value=lo)
            scale = ttk.Scale(snd, from_=lo, to=hi, variable=var, orient="horizontal",
                              command=lambda _v, k=key: self.on_slider(k))
            scale.grid(row=r, column=1, sticky="ew", pady=1)
            val = ttk.Label(snd, width=8, anchor="e")
            val.grid(row=r, column=2, sticky="e")
            self.vars[key], self.value_labels[key] = var, val

        # 3. live
        live = ttk.LabelFrame(main, text=" 3  Live ", padding=8)
        live.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        live.columnconfigure(1, weight=1)
        self.start_btn = ttk.Button(live, text="▶  Start", command=self.toggle, width=12)
        self.start_btn.grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, 12))
        self.status = ttk.Label(live, text="Stopped")
        self.status.grid(row=0, column=1, columnspan=2, sticky="w")
        meters = ttk.Frame(live)
        meters.grid(row=1, column=1, sticky="ew", pady=(4, 0))
        self.meter_canvases = []
        for i, name in enumerate(("Mic", "Out")):
            ttk.Label(meters, text=name, width=4).grid(row=i, column=0, sticky="w")
            c = tk.Canvas(meters, width=240, height=10, highlightthickness=0, background="#e4e4e4")
            c.grid(row=i, column=1, sticky="w", pady=1)
            self.meter_canvases.append(c)

        # test
        test = ttk.LabelFrame(main, text=" Test without Discord ", padding=8)
        test.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        self.rec_btn = ttk.Button(test, text=f"●  Record {TEST_SECONDS} s", command=self.record_test)
        self.rec_btn.grid(row=0, column=0, padx=(0, 6))
        self.play_ghost_btn = ttk.Button(test, text="▶  Play ghost", command=lambda: self.play_test(True))
        self.play_ghost_btn.grid(row=0, column=1, padx=(0, 6))
        self.play_orig_btn = ttk.Button(test, text="▶  Play original", command=lambda: self.play_test(False))
        self.play_orig_btn.grid(row=0, column=2)
        self.test_status = ttk.Label(test, text="Record yourself, then hear it with the current sound "
                                               "on your default speakers or headphones.",
                                     foreground="#555", wraplength=470, justify="left")
        self.test_status.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

        self.discord_tip = ttk.Label(main, text="", foreground="#555", wraplength=490, justify="left")
        self.discord_tip.grid(row=4, column=0, sticky="w")

    # --------------------------------------------------------------- devices
    def refresh_devices(self, initial: bool = False) -> None:
        from .realtime import device_choices, is_cable

        din = dout = -1
        try:
            self.inputs, self.outputs, din, dout = device_choices()
            self.audio_error = None
        except BaseException as e:  # SystemExit from a missing PortAudio, PortAudioError, ...
            self.inputs, self.outputs = [], []
            self.audio_error = str(e) or e.__class__.__name__
        in_names = [n for _, n in self.inputs]
        out_names = [n for _, n in self.outputs]
        self.in_box["values"] = in_names
        self.out_box["values"] = out_names

        want_in = self.in_var.get() or (self.cfg.get("input") if initial else "")
        if want_in not in in_names:
            default = next((n for i, n in self.inputs if i == din and not is_cable(n)), None)
            want_in = default or next((n for n in in_names if not is_cable(n)), in_names[0] if in_names else "")
        self.in_var.set(want_in)

        want_out = self.out_var.get() or (self.cfg.get("output") if initial else "")
        if want_out not in out_names:
            cable = next((n for n in out_names if is_cable(n)), None)
            default = next((n for i, n in self.outputs if i == dout), None)
            want_out = cable or default or (out_names[0] if out_names else "")
        self.out_var.set(want_out)
        self._update_hints()

    def _selected(self, devices, var):
        return next((i for i, n in devices if n == var.get()), None)

    def on_device_change(self) -> None:
        self._update_hints()
        if self.engine is not None:
            self.stop()
            self.start()

    def _update_hints(self) -> None:
        from .realtime import discord_input_name, is_cable

        out = self.out_var.get()
        has_cable = any(is_cable(n) for _, n in self.outputs)
        show_btn = False
        if self.audio_error:
            hint = f"⚠ Audio is unavailable: {self.audio_error}"
            tip = ""
        elif not self.outputs or not self.inputs:
            hint = "⚠ No microphone or output device found. Plug one in and press Refresh."
            tip = ""
        elif is_cable(out):
            hint = "✓ Sending to a virtual cable, so Discord can hear the ghost voice."
            tip = (f"In Discord: User Settings → Voice & Video → Input Device: “{discord_input_name(out)}”. "
                   "Turn off Noise Suppression and Echo Cancellation, or Discord removes the ghostly wash.")
        else:
            show_btn = not has_cable
            hint = ("Playing on this device: good for listening (use headphones to avoid feedback), "
                    "but Discord can't hear it. ")
            hint += ("Pick your virtual cable above to use it in Discord." if has_cable else
                     "To use it in Discord, install a free virtual audio cable, press Refresh, "
                     "and pick it here.")
            tip = ""
        self.cable_hint.configure(text=hint)
        self.discord_tip.configure(text=tip)
        if show_btn:
            self.cable_btn.grid()
        else:
            self.cable_btn.grid_remove()
        ok = not self.audio_error and bool(self.inputs) and bool(self.outputs)
        for b in (self.start_btn, self.rec_btn):
            b.state(["!disabled"] if ok else ["disabled"])
        self._update_play_buttons()

    # ------------------------------------------------------------- controls
    def controls(self) -> dict[str, float]:
        return {key: self._quantize(key, self.vars[key].get()) for key, *_ in SLIDERS}

    def current_settings(self):
        return from_controls(self.controls())

    @staticmethod
    def _quantize(key: str, v: float) -> float:
        spec = next(s for s in SLIDERS if s[0] == key)
        lo, hi, step = spec[2], spec[3], spec[4]
        return float(min(max(round((v - lo) / step) * step + lo, lo), hi))

    def set_controls(self, c: dict[str, float]) -> None:
        self._loading = True
        try:
            for key, *_rest in SLIDERS:
                if key in c:
                    self.vars[key].set(self._quantize(key, c[key]))
                self._show_value(key)
        finally:
            self._loading = False

    def _show_value(self, key: str) -> None:
        fmt = next(s[5] for s in SLIDERS if s[0] == key)
        self.value_labels[key].configure(text=fmt(self._quantize(key, self.vars[key].get())))

    def _load_controls(self) -> None:
        preset = self.cfg.get("preset", DEFAULT_PRESET)
        saved = self.cfg.get("controls")
        if preset in PRESETS:
            self.preset_var.set(preset)
            self.set_controls(to_controls(PRESETS[preset]))
            self.note.configure(text=NOTES.get(preset, ""))
        if preset == CUSTOM and isinstance(saved, dict):
            self.set_controls({**to_controls(PRESETS[DEFAULT_PRESET]), **saved})
            self.preset_var.set(CUSTOM)
        elif preset not in PRESETS:
            self.preset_var.set(DEFAULT_PRESET)
            self.set_controls(to_controls(PRESETS[DEFAULT_PRESET]))
            self.note.configure(text=NOTES[DEFAULT_PRESET])
        self._update_status()

    def on_preset(self) -> None:
        name = self.preset_var.get()
        if name in PRESETS:
            self.set_controls(to_controls(PRESETS[name]))
            self.note.configure(text=NOTES.get(name, ""))
            self.schedule_apply(0)

    def on_slider(self, key: str) -> None:
        self._show_value(key)
        if self._loading:
            return
        self.preset_var.set(CUSTOM)
        self.note.configure(text="Your own mix. Pick a preset to start over.")
        self.schedule_apply()

    def schedule_apply(self, delay_ms: int = 120) -> None:
        if self._apply_job is not None:
            self.root.after_cancel(self._apply_job)
        self._apply_job = self.root.after(delay_ms, self.apply)

    def apply(self) -> None:
        self._apply_job = None
        if self.engine is not None:
            self.engine.update(self.current_settings())
        self._update_status()

    # ------------------------------------------------------------------ live
    def toggle(self) -> None:
        if self.engine is not None:
            self.stop()
        else:
            self.start()

    def start(self) -> None:
        from tkinter import messagebox

        from .realtime import LiveEngine

        i, o = self._selected(self.inputs, self.in_var), self._selected(self.outputs, self.out_var)
        if i is None or o is None:
            messagebox.showerror("GhostVoice", "Choose a microphone and an output device first.")
            return
        engine = LiveEngine(self.current_settings(), i, o, SAMPLE_RATE, BLOCK_SIZE)
        try:
            engine.start()
        except BaseException as e:
            messagebox.showerror("Couldn't start audio",
                                 f"{e}\n\nTry another device, or close other apps that are using it.")
            return
        self.engine = engine
        self.start_btn.configure(text="■  Stop")
        self._update_status()

    def stop(self) -> None:
        engine, self.engine = self.engine, None
        if engine is not None:
            try:
                engine.stop()
            except Exception:
                pass
        self.start_btn.configure(text="▶  Start")
        self._update_status()

    def _update_status(self) -> None:
        delay = self.current_settings().tail * 1000
        if self.engine is not None:
            drop = self.engine.dropouts
            text = f"● On   ·   voice delay {delay:.0f} ms"
            if drop:
                text += f"   ·   {drop} audio dropouts"
        else:
            text = f"Stopped   ·   voice delay will be {delay:.0f} ms"
        self.status.configure(text=text)

    def _draw_meter(self, canvas, peak: float) -> None:
        w, h = int(canvas["width"]), int(canvas["height"])
        db = 20 * np.log10(max(peak, 1e-6))
        frac = float(np.clip((db + 60) / 60, 0, 1))
        color = "#3aa655" if db < -12 else "#e0b000" if db < -3 else "#d33"
        canvas.delete("all")
        if frac > 0:
            canvas.create_rectangle(0, 0, frac * w, h, fill=color, width=0)

    def _poll(self) -> None:
        if self.engine is not None:
            levels = self.engine.take_levels()
            self._meter = [max(new, old * 0.8) for new, old in zip(levels, self._meter)]
            if self.engine.dropouts:
                self._update_status()
        else:
            self._meter = [m * 0.8 for m in self._meter]
        for c, m in zip(self.meter_canvases, self._meter):
            self._draw_meter(c, m)
        self._check_recording()
        self._poll_job = self.root.after(50, self._poll)

    # ------------------------------------------------------------------ test
    def record_test(self) -> None:
        from .realtime import _sd, opened

        i = self._selected(self.inputs, self.in_var)
        if i is None:
            return
        self.rec_btn.state(["disabled"])
        self._rec_left = TEST_SECONDS
        self._rec_result = None
        self.test_status.configure(text=f"Recording… say something! ({TEST_SECONDS} s)")

        def work():
            try:
                sd = _sd()
                error = None
                with opened(i) as dev:
                    info = sd.query_devices(dev, "input")
                    for rate in dict.fromkeys((SAMPLE_RATE, int(info.get("default_samplerate") or SAMPLE_RATE))):
                        try:
                            data = sd.rec(int(TEST_SECONDS * rate), samplerate=rate, channels=1, device=dev,
                                          dtype="float32")
                            sd.wait()
                            self._rec_result = (data[:, 0].astype(np.float64), rate)
                            return
                        except Exception as e:
                            error = e
                self._rec_result = error
            except BaseException as e:
                self._rec_result = e

        threading.Thread(target=work, daemon=True).start()
        self._tick_countdown()

    def _tick_countdown(self) -> None:
        if self._rec_result is None and self._rec_left > 0:
            self.test_status.configure(text=f"Recording… say something! ({self._rec_left} s)")
            self._rec_left -= 1
            self.root.after(1000, self._tick_countdown)

    def _check_recording(self) -> None:
        result, self._rec_result = self._rec_result, None
        if result is None:
            return
        self.rec_btn.state(["!disabled"])
        if isinstance(result, BaseException):
            self.test_status.configure(text=f"⚠ Recording failed: {result}")
            return
        x, rate = result
        peak = float(np.abs(x).max()) if x.size else 0.0
        self.recording = (x, rate)
        if peak < 0.003:
            self.test_status.configure(text="⚠ That was silent. Is the right microphone selected and unmuted?")
        else:
            self.test_status.configure(text="Got it. Play it back, change the sound, and play again.")
        self._update_play_buttons()

    def _update_play_buttons(self) -> None:
        state = ["!disabled"] if self.recording is not None and not self.audio_error else ["disabled"]
        self.play_ghost_btn.state(state)
        self.play_orig_btn.state(state)

    def play_test(self, ghost: bool) -> None:
        from .realtime import _sd

        if self.recording is None:
            return
        x, rate = self.recording
        y = reverse_reverb_offline(x, rate, self.current_settings()) if ghost else x
        try:
            sd = _sd()
            sd.stop()
            sd.play(y.astype(np.float32), rate)
        except BaseException as e:
            self.test_status.configure(text=f"⚠ Playback failed: {e}")

    # ------------------------------------------------------------- lifecycle
    def _load_config(self) -> dict:
        try:
            return json.loads(config_path().read_text())
        except Exception:
            return {}

    def save_config(self) -> None:
        if not self.persist:
            return
        cfg = {
            "input": self.in_var.get(),
            "output": self.out_var.get(),
            "preset": self.preset_var.get(),
            "controls": self.controls(),
        }
        try:
            path = config_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cfg, indent=2))
        except Exception:
            pass

    def close(self) -> None:
        self.root.after_cancel(self._poll_job)
        if self._apply_job is not None:
            self.root.after_cancel(self._apply_job)
        self.save_config()
        self.stop()
        try:
            import sounddevice as sd

            sd.stop()
        except BaseException:
            pass
        self.root.destroy()


def _windows_dpi_fix() -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


def selftest(report: str | None = None) -> int:
    """Check the bundled app works without touching the user's settings or audio."""
    info: dict = {
        "version": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "frozen": bool(getattr(sys, "frozen", False)),
    }
    ok = True
    try:
        g = GhostFilter(SAMPLE_RATE, PRESETS[DEFAULT_PRESET], BLOCK_SIZE)
        y = g.process(np.random.default_rng(0).standard_normal(BLOCK_SIZE * 40) * 0.1)
        assert np.isfinite(y).all()
        info["dsp"] = "ok"
        try:
            import sounddevice as sd

            info["portaudio"] = sd.get_portaudio_version()[1]
        except OSError:
            raise  # PortAudio missing from the bundle: a real failure
        except Exception as e:  # loaded, but this machine has no audio system (e.g. a build server)
            info["portaudio"] = f"loaded, no audio system: {e}"
        from .realtime import device_choices

        try:
            ins, outs, _, _ = device_choices()
            info["devices"] = {"inputs": [n for _, n in ins], "outputs": [n for _, n in outs]}
        except Exception as e:  # build machines often have no sound hardware
            info["devices"] = f"unavailable: {e}"
        import tkinter as tk

        root = tk.Tk()
        app = App(root, persist=False)
        root.update_idletasks()
        root.update()
        app.close()
        info["gui"] = "ok"
    except BaseException:
        ok = False
        info["error"] = traceback.format_exc()
    text = json.dumps(info, indent=2)
    if report:
        Path(report).write_text(text)
    print(text)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        report = None
        if "--report" in argv and argv.index("--report") + 1 < len(argv):
            report = argv[argv.index("--report") + 1]
        return selftest(report)

    import tkinter as tk

    _windows_dpi_fix()
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
