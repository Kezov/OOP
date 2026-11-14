"""Ctrl + Shift + Numpad 0-9 hotkeys that work while another app (Discord, a game) is focused.

Windows' RegisterHotKey can't catch Ctrl+Shift+Numpad: with NumLock on,
Shift turns Numpad 1 into End and the keyboard driver fakes a Shift release
around it. So on Windows we watch the physical keys with a low-level
keyboard hook instead: numpad keys are recognised by scan code (they aren't
"extended", unlike the separate arrow/Home/End keys), and the driver's fake
Shift events (which *are* marked extended) are ignored. The matching key
presses are swallowed, so the focused app doesn't also get e.g. Ctrl+Shift+End.

Elsewhere, the app falls back to Tk key bindings, which only work while the
GhostVoice window is focused.
"""

from __future__ import annotations

import sys
import threading
from typing import Callable

HOTKEY_LABEL = "Ctrl + Shift + Numpad"

NUMPAD_SCANCODES = {0x52: 0, 0x4F: 1, 0x50: 2, 0x51: 3, 0x4B: 4, 0x4C: 5, 0x4D: 6, 0x47: 7, 0x48: 8, 0x49: 9}
VK_CTRL = {0x11, 0xA2, 0xA3}  # VK_CONTROL, VK_LCONTROL, VK_RCONTROL
VK_SHIFT = {0x10, 0xA0, 0xA1}  # VK_SHIFT, VK_LSHIFT, VK_RSHIFT
LLKHF_EXTENDED = 0x01

_KEYSYMS = {
    "KP_0": 0, "KP_Insert": 0, "KP_1": 1, "KP_End": 1, "KP_2": 2, "KP_Down": 2, "KP_3": 3, "KP_Next": 3,
    "KP_4": 4, "KP_Left": 4, "KP_5": 5, "KP_Begin": 5, "KP_6": 6, "KP_Right": 6, "KP_7": 7, "KP_Home": 7,
    "KP_8": 8, "KP_Up": 8, "KP_9": 9, "KP_Prior": 9,
}


def digit_from_keysym(keysym: str) -> int | None:
    """Numpad digit for a Tk keysym (with or without NumLock/Shift applied)."""
    return _KEYSYMS.get(keysym)


class ChordTracker:
    """Follows real Ctrl/Shift state from raw key events and spots Ctrl+Shift+Numpad digits."""

    def __init__(self):
        self.ctrl: set[int] = set()
        self.shift: set[int] = set()
        self._swallowed: set[int] = set()  # numpad scan codes whose key-down we swallowed

    def feed(self, vk: int, scan: int, flags: int, down: bool) -> tuple[int | None, bool]:
        """Returns ``(digit or None, swallow)`` for one key event."""
        extended = bool(flags & LLKHF_EXTENDED)
        if vk in VK_CTRL:
            (self.ctrl.add if down else self.ctrl.discard)(vk)
            return None, False
        if vk in VK_SHIFT:
            if not extended:  # extended Shift events are the driver's NumLock fakes
                (self.shift.add if down else self.shift.discard)(scan)
            return None, False
        if scan in NUMPAD_SCANCODES and not extended:
            if down and self.ctrl and self.shift:
                self._swallowed.add(scan)
                return NUMPAD_SCANCODES[scan], True
            if not down and scan in self._swallowed:
                self._swallowed.discard(scan)
                return None, True
        return None, False


class GlobalHotkeys:
    """Calls ``on_digit(d)`` (from a background thread) when Ctrl+Shift+Numpad d is pressed anywhere."""

    def __init__(self, on_digit: Callable[[int], None]):
        self.on_digit = on_digit
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self.active = False
        self.error: str | None = None

    @staticmethod
    def supported() -> bool:
        return sys.platform == "win32"

    def start(self) -> bool:
        if not self.supported():
            self.error = "global hotkeys need Windows"
            return False
        self._thread = threading.Thread(target=self._run, name="ghostvoice-hotkeys", daemon=True)
        self._thread.start()
        self._ready.wait(5)
        return self.active

    def stop(self) -> None:
        if self._thread is None:
            return
        import ctypes

        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)  # WM_QUIT
        self._thread.join(2)
        self._thread = None
        self.active = False

    def _run(self) -> None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        LRESULT = ctypes.c_ssize_t

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.CallNextHookEx.restype = LRESULT
        user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE

        tracker = ChordTracker()
        hook = None

        def proc(n_code, w_param, l_param):
            if n_code == 0:  # HC_ACTION
                try:
                    k = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    down = w_param in (0x0100, 0x0104)  # WM_KEYDOWN, WM_SYSKEYDOWN
                    digit, swallow = tracker.feed(k.vkCode, k.scanCode, k.flags, down)
                    if digit is not None and not user32.GetAsyncKeyState(0x11) & 0x8000:
                        tracker.ctrl.clear()  # we missed Ctrl's release (e.g. after Ctrl+Alt+Del)
                        tracker._swallowed.discard(k.scanCode)
                        digit, swallow = None, False
                    if digit is not None:
                        self.on_digit(digit)
                    if swallow:
                        return 1
                except Exception:
                    pass  # never let an error break the user's keyboard
            return user32.CallNextHookEx(hook, n_code, w_param, l_param)

        callback = HOOKPROC(proc)  # keep a reference for the hook's lifetime
        self._thread_id = kernel32.GetCurrentThreadId()
        hook = user32.SetWindowsHookExW(13, callback, kernel32.GetModuleHandleW(None), 0)  # WH_KEYBOARD_LL
        if not hook:
            self.error = f"SetWindowsHookEx failed ({ctypes.get_last_error()})"
            self._ready.set()
            return
        self.active = True
        self._ready.set()
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                pass
        finally:
            user32.UnhookWindowsHookEx(hook)
            self.active = False
