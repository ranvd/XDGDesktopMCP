"""Input coordinate normalization, mouse actions, and Keysym Synthesizer (ADR 0002 & 0003)."""

from __future__ import annotations

from collections.abc import Callable
import logging
import shutil
import subprocess
import time
from typing import Protocol

logger = logging.getLogger(__name__)

# Linux evdev pointer button codes (<linux/input-event-codes.h>)
BTN_LEFT = 0x110  # 272
BTN_RIGHT = 0x111  # 273
BTN_MIDDLE = 0x112  # 274

BUTTON_MAP: dict[str, int] = {
    "left": BTN_LEFT,
    "right": BTN_RIGHT,
    "middle": BTN_MIDDLE,
}

# Portal discrete axis identifiers
AXIS_VERTICAL = 0
AXIS_HORIZONTAL = 1

# X11 Modifier keysyms
XK_SHIFT_L = 0xFFE1
XK_SHIFT_R = 0xFFE2
XK_CONTROL_L = 0xFFE3
XK_CONTROL_R = 0xFFE4
XK_CAPS_LOCK = 0xFFE5
XK_META_L = 0xFFE7
XK_META_R = 0xFFE8
XK_ALT_L = 0xFFE9
XK_ALT_R = 0xFFEA
XK_SUPER_L = 0xFFEB
XK_SUPER_R = 0xFFEC
XK_ISO_LEVEL3_SHIFT = 0xFE03

MODIFIER_ALIASES: dict[str, int] = {
    "shift": XK_SHIFT_L,
    "shift_l": XK_SHIFT_L,
    "shift_r": XK_SHIFT_R,
    "ctrl": XK_CONTROL_L,
    "control": XK_CONTROL_L,
    "control_l": XK_CONTROL_L,
    "control_r": XK_CONTROL_R,
    "alt": XK_ALT_L,
    "alt_l": XK_ALT_L,
    "alt_r": XK_ALT_R,
    "meta": XK_SUPER_L,
    "super": XK_SUPER_L,
    "super_l": XK_SUPER_L,
    "super_r": XK_SUPER_R,
    "win": XK_SUPER_L,
    "cmd": XK_SUPER_L,
    "altgr": XK_ISO_LEVEL3_SHIFT,
}

MODIFIER_KEYSYMS: frozenset[int] = frozenset(MODIFIER_ALIASES.values())

# X11 Function & Navigation keysyms
NAMED_KEYSYMS: dict[str, int] = {
    "backspace": 0xFF08,
    "tab": 0xFF09,
    "linefeed": 0xFF0A,
    "clear": 0xFF0B,
    "return": 0xFF0D,
    "enter": 0xFF0D,
    "pause": 0xFF13,
    "scroll_lock": 0xFF14,
    "sys_req": 0xFF15,
    "escape": 0xFF1B,
    "esc": 0xFF1B,
    "delete": 0xFFFF,
    "del": 0xFFFF,
    "home": 0xFF50,
    "left": 0xFF51,
    "up": 0xFF52,
    "right": 0xFF53,
    "down": 0xFF54,
    "prior": 0xFF55,
    "page_up": 0xFF55,
    "pageup": 0xFF55,
    "pgup": 0xFF55,
    "next": 0xFF56,
    "page_down": 0xFF56,
    "pagedown": 0xFF56,
    "pgdn": 0xFF56,
    "end": 0xFF57,
    "begin": 0xFF58,
    "insert": 0xFF63,
    "ins": 0xFF63,
    "menu": 0xFF67,
    "print": 0xFF61,
    "num_lock": 0xFF7F,
    "caps_lock": XK_CAPS_LOCK,
    "space": 0x0020,
    "kp_enter": 0xFF8D,
    "iso_left_tab": 0xFE20,
    # Function keys F1..F12
    "f1": 0xFFBE,
    "f2": 0xFFBF,
    "f3": 0xFFC0,
    "f4": 0xFFC1,
    "f5": 0xFFC2,
    "f6": 0xFFC3,
    "f7": 0xFFC4,
    "f8": 0xFFC5,
    "f9": 0xFFC6,
    "f10": 0xFFC7,
    "f11": 0xFFC8,
    "f12": 0xFFC9,
    # Modifiers also addressable as standalone keys
    **MODIFIER_ALIASES,
}

FUNCTION_KEYSYMS_F1_TO_F12: frozenset[int] = frozenset(range(0xFFBE, 0xFFCA))
VT_SWITCH_KEYSYMS: frozenset[int] = frozenset(range(0x1008FE01, 0x1008FE0D))

#Threshold in characters above which "auto" mode uses clipboard pasting (ADR 0002)
AUTO_CLIPBOARD_THRESHOLD = 32


class UnsafeInputError(ValueError):
    """Raised when an input sequence violates session lifecycle guardrails (ADR 0003)."""


class InputBackend(Protocol):
    """Protocol implemented by PortalSession for dispatching RemoteDesktop events."""

    def notify_pointer_motion_absolute(self, x: float, y: float) -> None: ...
    def notify_pointer_button(self, button: int, pressed: bool) -> None: ...
    def notify_pointer_axis_discrete(self, axis: int, steps: int) -> None: ...
    def notify_pointer_axis(self, dx: float, dy: float, finish: bool = True) -> None: ...
    def notify_keyboard_keysym(self, keysym: int, pressed: bool) -> None: ...


def normalize_to_display(
    x: float, y: float, width: int, height: int
) -> tuple[float, float]:
    """Translate normalized [0.0, 1.0] Coordinate Space onto Target Display pixel dimensions."""
    if not (0.0 <= x <= 1.0) or not (0.0 <= y <= 1.0):
        raise ValueError(
            f"Normalized coordinates must be within [0.0, 1.0], got ({x}, {y})."
        )
    if width <= 0 or height <= 0:
        raise ValueError(
            f"Target Display dimensions must be positive, got {width}x{height}."
        )
    # Clamp the upper edge slightly below width/height only if needed by callers,
    # or return exact proportional coordinates within [0.0, width] x [0.0, height].
    px = round(float(x) * float(width), 4)
    py = round(float(y) * float(height), 4)
    return px, py


def resolve_button(button: str | int) -> int:
    """Resolve a button name ('left', 'right', 'middle') or evdev code to an integer code."""
    if isinstance(button, int):
        return button
    key = button.strip().lower()
    if key not in BUTTON_MAP:
        raise ValueError(
            f"Unsupported mouse button '{button}'. Expected one of: {', '.join(BUTTON_MAP)}."
        )
    return BUTTON_MAP[key]


def char_to_keysym(ch: str) -> int:
    """Convert a single Unicode character to its X11 keysym identifier."""
    if len(ch) != 1:
        raise ValueError(f"Expected a single character, got {ch!r}.")
    if ch == "\n" or ch == "\r":
        return NAMED_KEYSYMS["return"]
    if ch == "\t":
        return NAMED_KEYSYMS["tab"]
    if ch == "\b":
        return NAMED_KEYSYMS["backspace"]

    code = ord(ch)
    # Printable ASCII (0x20..0x7E) and Latin-1 (0xA0..0xFF) map 1:1 in X11 keysyms
    if 0x20 <= code <= 0x7E or 0xA0 <= code <= 0xFF:
        return code
    # Standard X11 Unicode keysym encoding: 0x01000000 | codepoint
    if code >= 0x100:
        return 0x01000000 | code
    raise ValueError(f"Unsupported control character {ch!r} (U+{code:04X}).")


def resolve_keysym(key: str) -> int:
    """Resolve a symbolic key name or single character into an X11 keysym."""
    if not key:
        raise ValueError("Key name cannot be empty.")

    # Single space character
    if key == " ":
        return NAMED_KEYSYMS["space"]

    stripped = key.strip()
    if not stripped:
        raise ValueError("Key name cannot be empty.")

    lower = stripped.lower()
    if lower in NAMED_KEYSYMS:
        return NAMED_KEYSYMS[lower]

    if len(stripped) == 1:
        return char_to_keysym(stripped)

    # Support hex keysym notation e.g. "0xff0d" for advanced callers
    if lower.startswith("0x"):
        try:
            sym = int(lower, 16)
            if sym in VT_SWITCH_KEYSYMS:
                raise UnsafeInputError(
                    f"Blocked virtual-terminal switching keysym {key} (ADR 0003)."
                )
            return sym
        except ValueError as exc:
            if isinstance(exc, UnsafeInputError):
                raise
            pass

    raise ValueError(f"Unknown key name '{key}'.")


def parse_hotkey(keys: str | list[str] | tuple[str, ...]) -> list[int]:
    """Parse a hotkey chord like 'Ctrl+Shift+T' or ['Ctrl', 'c'] into ordered X11 keysyms.

    Enforces ADR 0003 safety guardrails against virtual-terminal switching (Ctrl+Alt+F1..F12).
    """
    if isinstance(keys, str):
        parts = [p.strip() for p in keys.split("+") if p.strip()]
    else:
        parts = [str(p).strip() for p in keys if str(p).strip()]

    if not parts:
        raise ValueError("Hotkey specification cannot be empty.")

    has_modifier = any(p.lower() in MODIFIER_ALIASES for p in parts[:-1]) or (
        len(parts) > 1 and any(p.lower() in MODIFIER_ALIASES for p in parts)
    )

    keysyms: list[int] = []
    for part in parts:
        lower = part.lower()
        if lower in MODIFIER_ALIASES:
            keysyms.append(MODIFIER_ALIASES[lower])
        elif len(part) == 1 and part.isalpha() and has_modifier:
            # Normalize single ASCII letters in chords (e.g. "Ctrl+V", "Ctrl+Shift+T")
            # to lowercase base keysym so "Ctrl+V" does not act like "Ctrl+Shift+V".
            keysyms.append(ord(lower))
        else:
            keysyms.append(resolve_keysym(part))

    validate_keysym_sequence_safety(keysyms)
    return keysyms


def validate_keysym_sequence_safety(keysyms: list[int]) -> None:
    """Reject virtual-terminal switching hotkeys (Ctrl+Alt+F1..F12) per ADR 0003."""
    for sym in keysyms:
        if sym in VT_SWITCH_KEYSYMS:
            raise UnsafeInputError(
                "Virtual-terminal switching keysym is blocked for session safety (ADR 0003)."
            )

    has_ctrl = XK_CONTROL_L in keysyms or XK_CONTROL_R in keysyms
    has_alt = XK_ALT_L in keysyms or XK_ALT_R in keysyms
    has_fn = any(sym in FUNCTION_KEYSYMS_F1_TO_F12 for sym in keysyms)
    if has_ctrl and has_alt and has_fn:
        raise UnsafeInputError(
            "Blocked Ctrl+Alt+F1..F12 virtual-terminal switching hotkey to prevent desktop lockout (ADR 0003)."
        )


def set_clipboard_text(text: str, timeout: float = 3.0) -> None:
    """Copy text into the system clipboard using Gtk/Gdk, wl-copy, xclip, or xsel."""
    errors: list[str] = []

    # 1. Try PyGObject Gtk/Gdk clipboard directly
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gdk, Gtk

        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(text, -1)
        clipboard.store()
        return
    except Exception as exc:
        errors.append(f"Gtk.Clipboard failed: {exc}")

    encoded = text.encode("utf-8")

    # 2. Try Wayland wl-copy CLI
    if shutil.which("wl-copy"):
        try:
            # Note: wl-copy forks into the background to serve clipboard requests;
            # closing stdout/stderr pipes prevents subprocess.run from hanging on child FDs.
            proc = subprocess.Popen(
                ["wl-copy"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            assert proc.stdin is not None
            proc.stdin.write(encoded)
            proc.stdin.close()
            ret = proc.wait(timeout=timeout)
            if ret == 0:
                return
            stderr_out = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
            errors.append(f"wl-copy exited with {ret}: {stderr_out.strip()}")
        except Exception as exc:
            errors.append(f"wl-copy failed: {exc}")

    # 3. Try X11 xclip CLI
    if shutil.which("xclip"):
        try:
            proc = subprocess.Popen(
                ["xclip", "-selection", "clipboard", "-i"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            assert proc.stdin is not None
            proc.stdin.write(encoded)
            proc.stdin.close()
            ret = proc.wait(timeout=timeout)
            if ret == 0:
                return
            errors.append(f"xclip exited with {ret}")
        except Exception as exc:
            errors.append(f"xclip failed: {exc}")

    # 4. Try xsel CLI
    if shutil.which("xsel"):
        try:
            subprocess.run(
                ["xsel", "--clipboard", "--input"],
                input=encoded,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=True,
            )
            return
        except Exception as exc:
            errors.append(f"xsel failed: {exc}")

    raise RuntimeError(
        "Unable to set system clipboard (tried Gtk.Clipboard, wl-copy, xclip, xsel): "
        + ("; ".join(errors) if errors else "no clipboard provider available.")
    )


class KeysymSynthesizer:
    """Dispatches pointer and keyboard input over a PortalSession with safety guardrails."""

    def __init__(
        self,
        backend: InputBackend,
        key_delay: float = 0.008,
        clipboard_setter: Callable[[str], None] | None = None,
    ) -> None:
        self._backend = backend
        self._key_delay = key_delay
        self._clipboard_setter = clipboard_setter or set_clipboard_text
        self._active_modifiers: list[int] = []

    @property
    def active_modifiers(self) -> tuple[int, ...]:
        """Currently held modifier keysyms (guaranteed empty outside active calls)."""
        return tuple(self._active_modifiers)

    def _press_key_raw(self, keysym: int) -> None:
        if keysym in MODIFIER_KEYSYMS and keysym not in self._active_modifiers:
            self._active_modifiers.append(keysym)
        self._backend.notify_keyboard_keysym(keysym, True)

    def _release_key_raw(self, keysym: int) -> None:
        try:
            self._backend.notify_keyboard_keysym(keysym, False)
        finally:
            if keysym in self._active_modifiers:
                self._active_modifiers.remove(keysym)

    def release_all_modifiers(self) -> None:
        """Release all tracked modifiers in reverse order (ADR 0003 finally guardrail)."""
        for mod in reversed(list(self._active_modifiers)):
            try:
                self._backend.notify_keyboard_keysym(mod, False)
            except Exception:
                logger.exception("Failed to release modifier keysym 0x%x", mod)
            finally:
                if mod in self._active_modifiers:
                    self._active_modifiers.remove(mod)

    # --- Pointer Actions ---

    def move_cursor(
        self, x: float, y: float, width: int, height: int
    ) -> tuple[float, float]:
        """Move pointer to normalized (x, y) on Target Display of size (width, height)."""
        px, py = normalize_to_display(x, y, width, height)
        self._backend.notify_pointer_motion_absolute(px, py)
        return px, py

    def click(
        self,
        x: float,
        y: float,
        width: int,
        height: int,
        button: str | int = "left",
    ) -> tuple[float, float]:
        """Move to normalized (x, y) and perform a single mouse button click."""
        btn_code = resolve_button(button)
        px, py = self.move_cursor(x, y, width, height)
        self._backend.notify_pointer_button(btn_code, True)
        try:
            if self._key_delay > 0:
                time.sleep(self._key_delay)
        finally:
            self._backend.notify_pointer_button(btn_code, False)
        return px, py

    def double_click(
        self,
        x: float,
        y: float,
        width: int,
        height: int,
        button: str | int = "left",
    ) -> tuple[float, float]:
        """Move to normalized (x, y) and perform a double click."""
        btn_code = resolve_button(button)
        px, py = self.move_cursor(x, y, width, height)
        for i in range(2):
            self._backend.notify_pointer_button(btn_code, True)
            try:
                if self._key_delay > 0:
                    time.sleep(self._key_delay)
            finally:
                self._backend.notify_pointer_button(btn_code, False)
            if i == 0 and self._key_delay > 0:
                time.sleep(min(0.05, self._key_delay * 4))
        return px, py

    def drag_and_drop(
        self,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        width: int,
        height: int,
        button: str | int = "left",
        steps: int = 10,
    ) -> tuple[tuple[float, float], tuple[float, float]]:
        """Drag from normalized (start_x, start_y) to (end_x, end_y) with smooth interpolation."""
        btn_code = resolve_button(button)
        s_px, s_py = normalize_to_display(start_x, start_y, width, height)
        e_px, e_py = normalize_to_display(end_x, end_y, width, height)
        num_steps = max(1, int(steps))

        self._backend.notify_pointer_motion_absolute(s_px, s_py)
        self._backend.notify_pointer_button(btn_code, True)
        try:
            for step in range(1, num_steps + 1):
                t = step / float(num_steps)
                ix = round(s_px + (e_px - s_px) * t, 4)
                iy = round(s_py + (e_py - s_py) * t, 4)
                self._backend.notify_pointer_motion_absolute(ix, iy)
                if self._key_delay > 0:
                    time.sleep(self._key_delay)
        finally:
            self._backend.notify_pointer_button(btn_code, False)

        return (s_px, s_py), (e_px, e_py)

    def scroll(self, direction: str = "down", amount: int = 3) -> None:
        """Dispatch discrete scroll steps in the given direction ('up', 'down', 'left', 'right')."""
        dir_norm = direction.strip().lower()
        steps = int(amount)
        if steps <= 0:
            raise ValueError(f"Scroll amount must be positive, got {amount}.")

        if dir_norm == "down":
            axis, signed_steps = AXIS_VERTICAL, steps
        elif dir_norm == "up":
            axis, signed_steps = AXIS_VERTICAL, -steps
        elif dir_norm == "right":
            axis, signed_steps = AXIS_HORIZONTAL, steps
        elif dir_norm == "left":
            axis, signed_steps = AXIS_HORIZONTAL, -steps
        else:
            raise ValueError(
                f"Invalid scroll direction '{direction}'. Expected 'up', 'down', 'left', or 'right'."
            )

        self._backend.notify_pointer_axis_discrete(axis, signed_steps)

    # --- Keyboard & Text Synthesis ---

    def press_key(self, key: str) -> int:
        """Press and release a single symbolic key or character."""
        if "+" in key and len(key) > 1:
            keysyms = self.press_hotkey(key)
            return keysyms[-1]

        keysym = resolve_keysym(key)
        self.press_hotkey([key])
        return keysym

    def press_hotkey(self, keys: str | list[str] | tuple[str, ...]) -> list[int]:
        """Press a hotkey chord (e.g. 'Ctrl+Shift+T') and guarantee modifier release."""
        keysyms = parse_hotkey(keys)
        pressed_sequence: list[int] = []
        try:
            for sym in keysyms:
                self._press_key_raw(sym)
                pressed_sequence.append(sym)
                if self._key_delay > 0:
                    time.sleep(self._key_delay)
            # Release keys in reverse order on normal completion
            while pressed_sequence:
                sym = pressed_sequence.pop()
                self._release_key_raw(sym)
        finally:
            # Guarantee release of any keys still held if an error occurred
            while pressed_sequence:
                sym = pressed_sequence.pop()
                try:
                    self._release_key_raw(sym)
                except Exception:
                    logger.exception("Error releasing keysym 0x%x in cleanup", sym)
            self.release_all_modifiers()
        return keysyms

    def type_text_keysyms(self, text: str) -> int:
        """Type text character-by-character using X11 keysyms."""
        count = 0
        try:
            for ch in text:
                is_upper = ch.isupper()
                if is_upper:
                    self._backend.notify_keyboard_keysym(XK_SHIFT_L, True)
                try:
                    sym = char_to_keysym(ch)
                    self._backend.notify_keyboard_keysym(sym, True)
                    if self._key_delay > 0:
                        time.sleep(self._key_delay)
                    self._backend.notify_keyboard_keysym(sym, False)
                finally:
                    if is_upper:
                        self._backend.notify_keyboard_keysym(XK_SHIFT_L, False)
                count += 1
        finally:
            self.release_all_modifiers()
        return count

    def type_text_clipboard(self, text: str) -> None:
        """Inject text into the system clipboard and dispatch synthetic Ctrl+V (ADR 0002)."""
        self._clipboard_setter(text)
        if self._key_delay > 0:
            time.sleep(min(0.05, self._key_delay * 2))
        self.press_hotkey("Ctrl+v")

    def type_text(self, text: str, method: str = "auto") -> str:
        """Type text using the dual strategy (ADR 0002).

        Returns the actual method used ('keysym' or 'clipboard').
        """
        if not text:
            return "keysym"

        mode = method.strip().lower()
        if mode not in ("auto", "keysym", "clipboard"):
            raise ValueError(
                f"Invalid type_text method '{method}'. Expected 'auto', 'keysym', or 'clipboard'."
            )

        if mode == "keysym":
            self.type_text_keysyms(text)
            return "keysym"

        if mode == "clipboard":
            self.type_text_clipboard(text)
            return "clipboard"

        # Auto strategy: short single-line printable ASCII uses keysyms;
        # bulk text, multi-line code, or non-ASCII uses clipboard + Ctrl+V.
        is_short_ascii = (
            len(text) <= AUTO_CLIPBOARD_THRESHOLD
            and "\n" not in text
            and "\r" not in text
            and "\t" not in text
            and all(0x20 <= ord(c) <= 0x7E for c in text)
        )
        if is_short_ascii:
            self.type_text_keysyms(text)
            return "keysym"

        try:
            self.type_text_clipboard(text)
            return "clipboard"
        except Exception as exc:
            logger.warning(
                "Clipboard paste failed (%s); falling back to keysym typing.", exc
            )
            self.type_text_keysyms(text)
            return "keysym"
