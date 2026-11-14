"""Desktop app: pick devices, an effect and a preset, start/stop, hotkeys, test without Discord."""

from __future__ import annotations

import json
import os
import platform
import queue
import sys
import threading
import traceback
import webbrowser
from pathlib import Path

import numpy as np

from . import __version__
from .effects import DEFAULT_EFFECT, EFFECTS, Effect, delay_seconds, make_filter, render
from .hotkeys import HOTKEY_LABEL, GlobalHotkeys, digit_from_keysym

SAMPLE_RATE = 48000
BLOCK_SIZE = 256
TEST_SECONDS = 5
SILENT_MIC = 1e-4  # input peak below this (-80 dBFS) counts as a muted mic
SILENT_POLLS = 40  # warn after 2 s of it (the meters poll every 50 ms)
UNSAVED = "custom (unsaved)"
CUSTOM_SUFFIX = " (custom)"
FAV = "★ "
MAX_NAME = 40
REPO_URL = "https://github.com/Kezov/OOP#hooking-it-up-to-discord"


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
    def __init__(self, root, persist: bool = True, global_hotkeys: bool = True):
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
        self._silent_polls = 0
        self.default_out_is_cable = False
        self.listen_device = None
        # effects and presets
        self.effect: Effect = EFFECTS[DEFAULT_EFFECT]
        self.preset_name: str | None = self.effect.default  # None = sliders changed, not saved
        self.custom: dict[str, dict[str, dict[str, float]]] = {k: {} for k in EFFECTS}
        self.favorites: list[str] = []  # "effect:preset"
        self.hotkeys: dict[int, tuple[str, str]] = {}  # numpad digit -> (effect, preset)
        self.last: dict[str, dict] = {}  # per effect: the preset/sliders you had
        self._display_map: dict[str, str] = {}
        self.vars: dict = {}
        self.value_labels: dict = {}
        self._hotkey_queue: queue.SimpleQueue = queue.SimpleQueue()
        self.global_hotkeys: GlobalHotkeys | None = None

        root.title(f"GhostVoice {__version__}")
        root.resizable(True, False)
        self._build()
        self.refresh_devices(initial=True)
        self._load_state()
        self._start_hotkeys(global_hotkeys)
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
        ttk.Label(dev, text="Send voice to").grid(row=1, column=0, sticky="w", padx=(0, 8))
        self.out_var = tk.StringVar()
        self.out_box = ttk.Combobox(dev, textvariable=self.out_var, state="readonly", width=44)
        self.out_box.grid(row=1, column=1, sticky="ew", pady=2)
        self.cable_hint = ttk.Label(dev, text="", wraplength=470, justify="left")
        self.cable_hint.grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.cable_btn = ttk.Button(dev, text="Get a virtual cable…", command=lambda: webbrowser.open(cable_download_url()))
        self.cable_btn.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.in_box.bind("<<ComboboxSelected>>", lambda e: self.on_device_change())
        self.out_box.bind("<<ComboboxSelected>>", lambda e: self.on_device_change())

        # 2. tabs: sound, hotkeys, test (keeps the window short enough for small screens)
        self.tabs = ttk.Notebook(main)
        self.tabs.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        snd = ttk.Frame(self.tabs, padding=8)
        hk = ttk.Frame(self.tabs, padding=8)
        test = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(snd, text="  2  Sound  ")
        self.tabs.add(hk, text=f"  Hotkeys ({HOTKEY_LABEL})  ")
        self.tabs.add(test, text="  Test without Discord  ")
        snd.columnconfigure(1, weight=1)
        ttk.Label(snd, text="Effect").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.effect_var = tk.StringVar(value=self.effect.label)
        self.effect_box = ttk.Combobox(snd, textvariable=self.effect_var, state="readonly", width=16,
                                       values=[e.label for e in EFFECTS.values()])
        self.effect_box.grid(row=0, column=1, sticky="w", pady=2)
        self.effect_box.bind("<<ComboboxSelected>>", lambda e: self.on_effect())
        ttk.Label(snd, text="Preset").grid(row=1, column=0, sticky="w", padx=(0, 8))
        row = ttk.Frame(snd)
        row.grid(row=1, column=1, sticky="w", pady=2)
        self.preset_var = tk.StringVar()
        self.preset_box = ttk.Combobox(row, textvariable=self.preset_var, state="readonly", width=22)
        self.preset_box.grid(row=0, column=0)
        self.preset_box.bind("<<ComboboxSelected>>", lambda e: self.on_preset())
        self.fav_btn = ttk.Button(row, text="☆ Favorite", width=11, command=self.toggle_favorite)
        self.fav_btn.grid(row=0, column=1, padx=(6, 0))
        ttk.Button(row, text="Save as…", command=self.ask_save).grid(row=0, column=2, padx=(4, 0))
        self.del_btn = ttk.Button(row, text="Delete", command=self.ask_delete)
        self.del_btn.grid(row=0, column=3, padx=(4, 0))
        self.note = ttk.Label(snd, text="", foreground="#555", wraplength=470, justify="left")
        self.note.grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.slider_frame = ttk.Frame(snd)
        self.slider_frame.grid(row=3, column=0, columnspan=2, sticky="ew")
        self.slider_frame.columnconfigure(1, weight=1)

        # 3. live
        live = ttk.LabelFrame(main, text=" 3  Live ", padding=8)
        live.grid(row=3, column=0, sticky="ew", pady=(0, 8))
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

        # hotkeys tab
        ttk.Label(hk, text="Switch presets instantly, even mid-sentence. Pick a preset on the Sound tab, "
                           "choose a numpad key and press Assign. Presets from different effects can "
                           "each have their own key.", wraplength=470, justify="left"
                  ).grid(row=0, column=0, sticky="w", pady=(0, 8))
        hk_row = ttk.Frame(hk)
        hk_row.grid(row=1, column=0, sticky="w")
        ttk.Label(hk_row, text="Numpad key").grid(row=0, column=0, padx=(0, 8))
        self.hotkey_var = tk.StringVar(value="Num 1")
        ttk.Combobox(hk_row, textvariable=self.hotkey_var, state="readonly", width=7,
                     values=[f"Num {d}" for d in range(10)]).grid(row=0, column=1)
        self.assign_btn = ttk.Button(hk_row, text="Assign current preset", command=self.on_assign)
        self.assign_btn.grid(row=0, column=2, padx=(6, 0))
        ttk.Button(hk_row, text="Clear", command=self.on_clear_hotkey).grid(row=0, column=3, padx=(4, 0))
        self.hotkey_list = ttk.Label(hk, text="", justify="left")
        self.hotkey_list.grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.hotkey_hint = ttk.Label(hk, text="", foreground="#555", wraplength=470, justify="left")
        self.hotkey_hint.grid(row=3, column=0, sticky="w", pady=(8, 0))

        # test tab
        self.rec_btn = ttk.Button(test, text=f"●  Record {TEST_SECONDS} s", command=self.record_test)
        self.rec_btn.grid(row=0, column=0, padx=(0, 6))
        self.play_ghost_btn = ttk.Button(test, text="▶  Play with effect", command=lambda: self.play_test(True))
        self.play_ghost_btn.grid(row=0, column=1, padx=(0, 6))
        self.play_orig_btn = ttk.Button(test, text="▶  Play original", command=lambda: self.play_test(False))
        self.play_orig_btn.grid(row=0, column=2)
        self.test_status = ttk.Label(test, text="Record yourself, then hear it with the current sound "
                                               "on your default speakers or headphones.",
                                     foreground="#555", wraplength=470, justify="left")
        self.test_status.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

        self.discord_tip = ttk.Label(main, text="", foreground="#555", wraplength=490, justify="left")
        self.discord_tip.grid(row=4, column=0, sticky="w")

    def _build_sliders(self) -> None:
        tk, ttk = self.tk, self.ttk
        for w in self.slider_frame.winfo_children():
            w.destroy()
        self.vars, self.value_labels = {}, {}
        for r, s in enumerate(self.effect.sliders):
            ttk.Label(self.slider_frame, text=s.label).grid(row=r, column=0, sticky="w", padx=(0, 8))
            var = tk.DoubleVar(value=s.lo)
            ttk.Scale(self.slider_frame, from_=s.lo, to=s.hi, variable=var, orient="horizontal",
                      command=lambda _v, k=s.key: self.on_slider(k)).grid(row=r, column=1, sticky="ew", pady=1)
            val = ttk.Label(self.slider_frame, width=9, anchor="e")
            val.grid(row=r, column=2, sticky="e")
            self.vars[s.key], self.value_labels[s.key] = var, val

    # --------------------------------------------------------------- devices
    def refresh_devices(self, initial: bool = False) -> None:
        from .realtime import device_choices, is_cable, pick_real

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
            mic = pick_real(self.inputs, din)
            want_in = mic[1] if mic else (in_names[0] if in_names else "")
        self.in_var.set(want_in)
        # Where test playback goes: the default output, unless that's the cable.
        self.default_out_is_cable = any(i == dout and is_cable(n) for i, n in self.outputs)
        listen = pick_real(self.outputs, dout) if self.default_out_is_cable else None
        self.listen_device = listen[0] if listen else None

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
            hint = "✓ Sending to a virtual cable, so Discord can hear the effect."
            tip = (f"In Discord: User Settings → Voice & Video → Input Device: “{discord_input_name(out)}”. "
                   "Turn off Noise Suppression and Echo Cancellation, or Discord removes much of the effect.")
        else:
            show_btn = not has_cable
            hint = ("Playing on this device: good for listening (use headphones to avoid feedback), "
                    "but Discord can't hear it. ")
            hint += ("Pick your virtual cable above to use it in Discord." if has_cable else
                     "To use it in Discord, install a free virtual audio cable, press Refresh, "
                     "and pick it here.")
            tip = ""
        if self.default_out_is_cable and not self.audio_error:
            tip = ("⚠ Windows is sending all its sound into the virtual cable (it's the default output), "
                   "so you won't hear Discord or anything else. In Windows Sound settings, set Output back "
                   "to your headphones or speakers. " + tip)
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
        return {s.key: s.quantize(self.vars[s.key].get()) for s in self.effect.sliders}

    def current_settings(self):
        return self.effect.settings(self.controls())

    def set_controls(self, c: dict[str, float]) -> None:
        self._loading = True
        try:
            for s in self.effect.sliders:
                if s.key in c:
                    self.vars[s.key].set(s.quantize(c[s.key]))
                self._show_value(s.key)
        finally:
            self._loading = False

    def _show_value(self, key: str) -> None:
        s = self.effect.slider(key)
        self.value_labels[key].configure(text=s.fmt(s.quantize(self.vars[key].get())))

    def on_slider(self, key: str) -> None:
        self._show_value(key)
        if self._loading:
            return
        if self.preset_name is not None:
            self.preset_name = None
            self._refresh_presets()
        self.schedule_apply()

    def schedule_apply(self, delay_ms: int = 120) -> None:
        if self._apply_job is not None:
            self.root.after_cancel(self._apply_job)
        self._apply_job = self.root.after(delay_ms, self.apply)

    def apply(self) -> None:
        if self._apply_job is not None:
            self.root.after_cancel(self._apply_job)
        self._apply_job = None
        if self.engine is not None:
            self.engine.update(self.current_settings())
        self._update_status()

    # -------------------------------------------------------------- effects
    def on_effect(self) -> None:
        key = next((e.key for e in EFFECTS.values() if e.label == self.effect_var.get()), None)
        if key is not None:
            self.set_effect(key)

    def set_effect(self, key: str) -> None:
        if key == self.effect.key:
            return
        self._remember()
        self.effect = EFFECTS[key]
        self.effect_var.set(self.effect.label)
        self._build_sliders()
        self._restore(key)
        self.schedule_apply(0)

    def _remember(self) -> None:
        self.last[self.effect.key] = {"preset": self.preset_name, "controls": self.controls()}

    def _restore(self, key: str) -> None:
        """Bring back the preset or sliders this effect had (or its default preset)."""
        st = self.last.get(key) or {}
        name = st.get("preset")
        if name and self.preset_controls(key, name) is not None:
            self.preset_name = name
            self.set_controls(self.preset_controls(key, name))
        elif isinstance(st.get("controls"), dict):
            self.preset_name = None
            self.set_controls(self.effect.clean(st["controls"]))
        else:
            self.preset_name = self.effect.default
            self.set_controls(self.effect.presets[self.effect.default])
        self._refresh_presets()

    # -------------------------------------------------------------- presets
    def preset_controls(self, effect_key: str, name: str) -> dict[str, float] | None:
        e = EFFECTS[effect_key]
        if name in e.presets:
            return e.presets[name]
        return self.custom.get(effect_key, {}).get(name)

    def is_favorite(self, effect_key: str, name: str) -> bool:
        return f"{effect_key}:{name}" in self.favorites

    def preset_names(self, effect_key: str | None = None) -> list[str]:
        """Favorites first, then built-in presets, then your own (A-Z)."""
        e = EFFECTS[effect_key or self.effect.key]
        names = list(e.presets) + sorted(self.custom[e.key], key=str.lower)
        favs = [n for n in names if self.is_favorite(e.key, n)]
        return favs + [n for n in names if n not in favs]

    def display(self, name: str) -> str:
        text = name + (CUSTOM_SUFFIX if name in self.custom[self.effect.key] else "")
        return FAV + text if self.is_favorite(self.effect.key, name) else text

    def _refresh_presets(self) -> None:
        ek, name = self.effect.key, self.preset_name
        self._display_map = {self.display(n): n for n in self.preset_names()}
        self.preset_box["values"] = list(self._display_map)
        self.preset_var.set(self.display(name) if name else UNSAVED)
        saved = name is not None
        self.fav_btn.configure(text="★ Favorite" if saved and self.is_favorite(ek, name) else "☆ Favorite")
        self.fav_btn.state(["!disabled"] if saved else ["disabled"])
        self.del_btn.state(["!disabled"] if saved and name in self.custom[ek] else ["disabled"])
        self.assign_btn.state(["!disabled"] if saved else ["disabled"])
        if not saved:
            note = "Your own mix. Use Save as… to keep it, or pick a preset."
        elif name in self.custom[ek]:
            note = "One of your saved presets."
        else:
            note = self.effect.notes.get(name, "")
        self.note.configure(text=note)

    def on_preset(self) -> None:
        name = self._display_map.get(self.preset_var.get())
        if name is not None:
            self.select_preset(name)

    def select_preset(self, name: str) -> None:
        c = self.preset_controls(self.effect.key, name)
        if c is None:
            return
        self.preset_name = name
        self.set_controls(c)
        self._refresh_presets()
        self.schedule_apply(0)

    def check_name(self, name: str) -> tuple[str, str | None]:
        """Clean up a new preset name; returns ``(name, error or None)``."""
        name = " ".join((name or "").split())
        if not name:
            return name, "Type a name for the preset."
        if len(name) > MAX_NAME:
            return name, f"Keep the name under {MAX_NAME} characters."
        if name.lower() in (UNSAVED, "custom") or name.startswith(FAV.strip()) or name.endswith(CUSTOM_SUFFIX):
            return name, "That name is reserved. Pick another one."
        if name in self.effect.presets:
            return name, f"“{name}” is a built-in preset. Pick another name."
        return name, None

    def save_preset(self, name: str) -> None:
        """Save the current sliders as one of your presets (replacing one with the same name)."""
        self.custom[self.effect.key][name] = self.controls()
        self.preset_name = name
        self._refresh_presets()
        self._refresh_hotkeys()
        self.save_config()

    def delete_preset(self, name: str) -> None:
        ek = self.effect.key
        if name not in self.custom[ek]:
            return
        del self.custom[ek][name]
        if self.is_favorite(ek, name):
            self.favorites.remove(f"{ek}:{name}")
        for d in [d for d, target in self.hotkeys.items() if target == (ek, name)]:
            del self.hotkeys[d]
        if self.preset_name == name:
            self.preset_name = None  # keep the sliders, now unsaved
        self._refresh_presets()
        self._refresh_hotkeys()
        self.save_config()

    def toggle_favorite(self) -> None:
        if self.preset_name is None:
            return
        key = f"{self.effect.key}:{self.preset_name}"
        if key in self.favorites:
            self.favorites.remove(key)
        else:
            self.favorites.append(key)
        self._refresh_presets()
        self.save_config()

    def ask_save(self) -> None:
        from tkinter import messagebox, simpledialog

        suggested = self.preset_name if self.preset_name in self.custom[self.effect.key] else ""
        raw = simpledialog.askstring("Save preset", f"Name for this {self.effect.label} preset:",
                                     initialvalue=suggested, parent=self.root)
        if raw is None:
            return
        name, error = self.check_name(raw)
        if error:
            messagebox.showerror("Save preset", error, parent=self.root)
            return
        if name in self.custom[self.effect.key] and not messagebox.askyesno(
                "Save preset", f"Replace your preset “{name}”?", parent=self.root):
            return
        self.save_preset(name)

    def ask_delete(self) -> None:
        from tkinter import messagebox

        name = self.preset_name
        if name is None or name not in self.custom[self.effect.key]:
            return
        keys = [d for d, t in self.hotkeys.items() if t == (self.effect.key, name)]
        extra = f"\nIts hotkey (Num {', '.join(map(str, sorted(keys)))}) will be cleared too." if keys else ""
        if messagebox.askyesno("Delete preset", f"Delete your preset “{name}”?{extra}", parent=self.root):
            self.delete_preset(name)

    # -------------------------------------------------------------- hotkeys
    def _start_hotkeys(self, global_hotkeys: bool) -> None:
        if global_hotkeys and GlobalHotkeys.supported():
            self.global_hotkeys = GlobalHotkeys(self._hotkey_queue.put)
            if self.global_hotkeys.start():
                self.hotkey_hint.configure(text="Works anywhere, even while Discord or a game is focused.")
                return
            error = self.global_hotkeys.error
            self.global_hotkeys = None
            hint = f"Hotkeys only work while this window is focused ({error})."
        else:
            hint = "On this system, hotkeys only work while the GhostVoice window is focused."
        self.root.bind_all("<Control-Shift-KeyPress>", self._on_tk_key)
        self.hotkey_hint.configure(text=hint)

    def _on_tk_key(self, event):
        digit = digit_from_keysym(event.keysym)
        if digit is None:
            return None
        self._hotkey_queue.put(digit)
        return "break"

    def _hotkey_digit(self) -> int:
        return int(self.hotkey_var.get().split()[-1])

    def assign_hotkey(self, digit: int) -> None:
        if self.preset_name is None:
            return
        self.hotkeys[digit] = (self.effect.key, self.preset_name)
        self._refresh_hotkeys()
        self.save_config()

    def clear_hotkey(self, digit: int) -> None:
        if self.hotkeys.pop(digit, None) is not None:
            self._refresh_hotkeys()
            self.save_config()

    def on_assign(self) -> None:
        self.assign_hotkey(self._hotkey_digit())
        free = next((d for d in list(range(1, 10)) + [0] if d not in self.hotkeys), None)
        if free is not None:
            self.hotkey_var.set(f"Num {free}")  # ready for the next one

    def on_clear_hotkey(self) -> None:
        self.clear_hotkey(self._hotkey_digit())

    def trigger_hotkey(self, digit: int) -> None:
        target = self.hotkeys.get(digit)
        if target is None:
            return
        ek, name = target
        if ek not in EFFECTS or self.preset_controls(ek, name) is None:
            return
        self.set_effect(ek)
        self.select_preset(name)
        self.apply()  # the Live status line shows the new effect and preset

    def _refresh_hotkeys(self) -> None:
        if not self.hotkeys:
            text = "No hotkeys yet. Pick a preset, choose a numpad key and press Assign."
        else:
            text = "\n".join(f"Num {d}   →   {EFFECTS[ek].label}: {name}"
                             for d, (ek, name) in sorted(self.hotkeys.items()))
        self.hotkey_list.configure(text=text)

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
        self._silent_polls = 0
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

    @property
    def mic_silent(self) -> bool:
        return self.engine is not None and self._silent_polls >= SILENT_POLLS

    def _update_status(self) -> None:
        delay = delay_seconds(self.current_settings()) * 1000
        sound = f"{self.effect.label}: {self.preset_name or 'custom'}"
        if self.engine is not None:
            drop = self.engine.dropouts
            text = f"● On   ·   {sound}   ·   voice delay {delay:.0f} ms"
            if drop:
                text += f"   ·   {drop} audio dropouts"
            if self.mic_silent:
                text += "\n⚠ No sound from the microphone. Is it muted (headset mute button)?"
        else:
            text = f"Stopped   ·   {sound}   ·   voice delay will be {delay:.0f} ms"
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
        while True:
            try:
                digit = self._hotkey_queue.get_nowait()
            except queue.Empty:
                break
            self.trigger_hotkey(digit)
        if self.engine is not None:
            levels = self.engine.take_levels()
            self._meter = [max(new, old * 0.8) for new, old in zip(levels, self._meter)]
            was_silent = self.mic_silent
            # A working mic always picks up some noise; a muted one sends (near) digital zero.
            self._silent_polls = self._silent_polls + 1 if levels[0] < SILENT_MIC else 0
            if self.engine.dropouts or self.mic_silent != was_silent:
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

    def play_test(self, with_effect: bool) -> None:
        from .realtime import _sd, opened

        if self.recording is None:
            return
        x, rate = self.recording
        y = render(x, rate, self.current_settings()) if with_effect else x
        try:
            sd = _sd()
            sd.stop()
            with opened(self.listen_device) as dev:
                sd.play(y.astype(np.float32), rate, device=dev)
        except BaseException as e:
            self.test_status.configure(text=f"⚠ Playback failed: {e}")

    # ------------------------------------------------------------- settings
    def _load_config(self) -> dict:
        try:
            cfg = json.loads(config_path().read_text())
            return cfg if isinstance(cfg, dict) else {}
        except Exception:
            return {}

    def _load_state(self) -> None:
        """Effects, presets, favorites and hotkeys from the saved settings (any older format too)."""
        cfg = self.cfg
        last = cfg.get("effects") if isinstance(cfg.get("effects"), dict) else {}
        if "effects" not in cfg and "preset" in cfg:  # settings from before there were several effects
            old = cfg.get("preset")
            last = {"ghost": {"preset": old if old != "custom" else None, "controls": cfg.get("controls")}}
        self.last = {k: v for k, v in last.items() if k in EFFECTS and isinstance(v, dict)}
        custom = cfg.get("custom_presets") if isinstance(cfg.get("custom_presets"), dict) else {}
        for ek, presets in custom.items():
            if ek in EFFECTS and isinstance(presets, dict):
                for name, c in presets.items():
                    if isinstance(name, str) and isinstance(c, dict) and name not in EFFECTS[ek].presets:
                        self.custom[ek][name] = EFFECTS[ek].clean(c)
        favs = cfg.get("favorites") if isinstance(cfg.get("favorites"), list) else []
        self.favorites = [f for f in favs if isinstance(f, str)]
        hk = cfg.get("hotkeys") if isinstance(cfg.get("hotkeys"), dict) else {}
        for d, target in hk.items():
            if (str(d).isdigit() and 0 <= int(d) <= 9 and isinstance(target, list) and len(target) == 2
                    and target[0] in EFFECTS and self.preset_controls(target[0], target[1]) is not None):
                self.hotkeys[int(d)] = (target[0], target[1])
        key = cfg.get("effect") if cfg.get("effect") in EFFECTS else DEFAULT_EFFECT
        self.effect = EFFECTS[key]
        self.effect_var.set(self.effect.label)
        self._build_sliders()
        self._restore(key)
        self._refresh_hotkeys()
        self._update_status()

    def save_config(self) -> None:
        if not self.persist:
            return
        self._remember()
        cfg = {
            "input": self.in_var.get(),
            "output": self.out_var.get(),
            "effect": self.effect.key,
            "effects": self.last,
            "custom_presets": self.custom,
            "favorites": self.favorites,
            "hotkeys": {str(d): list(t) for d, t in sorted(self.hotkeys.items())},
        }
        try:
            path = config_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(cfg, indent=2))
            os.replace(tmp, path)
        except Exception:
            pass

    def close(self) -> None:
        self.root.after_cancel(self._poll_job)
        if self._apply_job is not None:
            self.root.after_cancel(self._apply_job)
        self.save_config()
        self.stop()
        if self.global_hotkeys is not None:
            self.global_hotkeys.stop()
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
        x = np.random.default_rng(0).standard_normal(BLOCK_SIZE * 40) * 0.1
        for e in EFFECTS.values():
            f = make_filter(SAMPLE_RATE, e.settings(e.presets[e.default]), BLOCK_SIZE)
            assert np.isfinite(f.process(x)).all(), e.key
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
        app = App(root, persist=False, global_hotkeys=False)
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
