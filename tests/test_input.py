"""Unit tests for coordinate translation, mouse operations, and KeysymSynthesizer."""

from __future__ import annotations

import pytest

from xdg_desktop_mcp.input import (
    AXIS_HORIZONTAL,
    AXIS_VERTICAL,
    BTN_LEFT,
    BTN_MIDDLE,
    BTN_RIGHT,
    KeysymSynthesizer,
    UnsafeInputError,
    XK_ALT_L,
    XK_CONTROL_L,
    XK_SHIFT_L,
    XK_SUPER_L,
    char_to_keysym,
    normalize_to_display,
    parse_hotkey,
    resolve_button,
    resolve_keysym,
)


class FakeInputBackend:
    """In-memory recorder for RemoteDesktop input calls."""

    def __init__(self, fail_on_keysym: int | None = None) -> None:
        self.events: list[tuple] = []
        self.fail_on_keysym = fail_on_keysym

    def notify_pointer_motion_absolute(self, x: float, y: float) -> None:
        self.events.append(("motion", x, y))

    def notify_pointer_button(self, button: int, pressed: bool) -> None:
        self.events.append(("button", button, pressed))

    def notify_pointer_axis_discrete(self, axis: int, steps: int) -> None:
        self.events.append(("axis_discrete", axis, steps))

    def notify_pointer_axis(self, dx: float, dy: float, finish: bool = True) -> None:
        self.events.append(("axis", dx, dy, finish))

    def notify_keyboard_keysym(self, keysym: int, pressed: bool) -> None:
        if pressed and self.fail_on_keysym is not None and keysym == self.fail_on_keysym:
            raise RuntimeError(f"Simulated D-Bus failure on keysym 0x{keysym:x}")
        self.events.append(("key", keysym, pressed))


class TestCoordinateNormalization:
    def test_normalize_center_and_corners(self) -> None:
        assert normalize_to_display(0.0, 0.0, 1920, 1080) == (0.0, 0.0)
        assert normalize_to_display(0.5, 0.5, 1920, 1080) == (960.0, 540.0)
        assert normalize_to_display(1.0, 1.0, 1920, 1080) == (1920.0, 1080.0)
        assert normalize_to_display(0.25, 0.75, 2560, 1440) == (640.0, 1080.0)

    @pytest.mark.parametrize(
        "x,y",
        [
            (-0.01, 0.5),
            (1.01, 0.5),
            (0.5, -0.1),
            (0.5, 1.5),
        ],
    )
    def test_out_of_bounds_coordinates_rejected(self, x: float, y: float) -> None:
        with pytest.raises(ValueError, match="Normalized coordinates must be within"):
            normalize_to_display(x, y, 1920, 1080)

    def test_invalid_display_dimensions_rejected(self) -> None:
        with pytest.raises(ValueError, match="Target Display dimensions must be positive"):
            normalize_to_display(0.5, 0.5, 0, 1080)
        with pytest.raises(ValueError, match="Target Display dimensions must be positive"):
            normalize_to_display(0.5, 0.5, 1920, -10)


class TestMouseOperations:
    def test_button_resolution(self) -> None:
        assert resolve_button("left") == BTN_LEFT
        assert resolve_button("RIGHT") == BTN_RIGHT
        assert resolve_button(" middle ") == BTN_MIDDLE
        assert resolve_button(272) == 272
        with pytest.raises(ValueError, match="Unsupported mouse button"):
            resolve_button("thumb")

    def test_click_and_double_click(self) -> None:
        backend = FakeInputBackend()
        synth = KeysymSynthesizer(backend, key_delay=0)

        px, py = synth.click(0.5, 0.5, 1000, 800, button="right")
        assert (px, py) == (500.0, 400.0)
        assert backend.events == [
            ("motion", 500.0, 400.0),
            ("button", BTN_RIGHT, True),
            ("button", BTN_RIGHT, False),
        ]

        backend.events.clear()
        synth.double_click(0.1, 0.2, 1000, 800)
        assert backend.events == [
            ("motion", 100.0, 160.0),
            ("button", BTN_LEFT, True),
            ("button", BTN_LEFT, False),
            ("button", BTN_LEFT, True),
            ("button", BTN_LEFT, False),
        ]

    def test_drag_and_drop_interpolates_and_releases(self) -> None:
        backend = FakeInputBackend()
        synth = KeysymSynthesizer(backend, key_delay=0)

        start_pt, end_pt = synth.drag_and_drop(
            0.0, 0.0, 1.0, 1.0, 100, 200, button="left", steps=4
        )
        assert start_pt == (0.0, 0.0)
        assert end_pt == (100.0, 200.0)
        assert backend.events[0] == ("motion", 0.0, 0.0)
        assert backend.events[1] == ("button", BTN_LEFT, True)
        assert backend.events[-1] == ("button", BTN_LEFT, False)
        motion_events = [e for e in backend.events if e[0] == "motion"]
        assert len(motion_events) == 5  # initial + 4 interpolated steps
        assert motion_events[-1] == ("motion", 100.0, 200.0)

    def test_scroll_directions(self) -> None:
        backend = FakeInputBackend()
        synth = KeysymSynthesizer(backend, key_delay=0)

        synth.scroll("down", 3)
        synth.scroll("up", 2)
        synth.scroll("right", 4)
        synth.scroll("left", 1)

        assert backend.events == [
            ("axis_discrete", AXIS_VERTICAL, 3),
            ("axis_discrete", AXIS_VERTICAL, -2),
            ("axis_discrete", AXIS_HORIZONTAL, 4),
            ("axis_discrete", AXIS_HORIZONTAL, -1),
        ]

        with pytest.raises(ValueError, match="Invalid scroll direction"):
            synth.scroll("diagonal", 1)
        with pytest.raises(ValueError, match="Scroll amount must be positive"):
            synth.scroll("down", 0)


class TestKeysymSynthesizerAndSafety:
    def test_resolve_keysym_and_unicode(self) -> None:
        assert resolve_keysym("Return") == 0xFF0D
        assert resolve_keysym("enter") == 0xFF0D
        assert resolve_keysym("BackSpace") == 0xFF08
        assert resolve_keysym("Escape") == 0xFF1B
        assert resolve_keysym(" ") == 0x0020
        assert resolve_keysym("a") == ord("a")
        assert resolve_keysym("A") == ord("A")
        assert char_to_keysym("€") == (0x01000000 | ord("€"))

    def test_parse_hotkey_normalizes_chord_letters(self) -> None:
        # "Ctrl+Shift+T" should press Control_L, Shift_L, and 't'
        syms = parse_hotkey("Ctrl+Shift+T")
        assert syms == [XK_CONTROL_L, XK_SHIFT_L, ord("t")]

        # "Super+L" should press Super_L and 'l'
        assert parse_hotkey("Super+L") == [XK_SUPER_L, ord("l")]

    @pytest.mark.parametrize(
        "unsafe_combo",
        [
            "Ctrl+Alt+F1",
            "Control+Alt+F2",
            "Ctrl+Shift+Alt+F7",
            "Ctrl+Alt+F12",
            ["Ctrl", "Alt", "F3"],
        ],
    )
    def test_blocks_vt_switching_hotkeys_adr0003(self, unsafe_combo: str | list[str]) -> None:
        backend = FakeInputBackend()
        synth = KeysymSynthesizer(backend, key_delay=0)
        with pytest.raises(UnsafeInputError, match="virtual-terminal switching"):
            synth.press_hotkey(unsafe_combo)
        assert backend.events == []

    def test_modifier_auto_release_on_exception_adr0003(self) -> None:
        # Fail when 't' is pressed after Ctrl and Shift are already held down
        backend = FakeInputBackend(fail_on_keysym=ord("t"))
        synth = KeysymSynthesizer(backend, key_delay=0)

        with pytest.raises(RuntimeError, match="Simulated D-Bus failure"):
            synth.press_hotkey("Ctrl+Shift+T")

        # Both Shift and Ctrl MUST be released in reverse order despite the exception
        assert synth.active_modifiers == ()
        assert backend.events == [
            ("key", XK_CONTROL_L, True),
            ("key", XK_SHIFT_L, True),
            ("key", XK_SHIFT_L, False),
            ("key", XK_CONTROL_L, False),
        ]

    def test_dual_text_input_strategy_adr0002(self) -> None:
        clipboard_box: list[str] = []
        backend = FakeInputBackend()
        synth = KeysymSynthesizer(
            backend,
            key_delay=0,
            clipboard_setter=lambda s: clipboard_box.append(s),
        )

        # 1. Short single-line ASCII -> keysym strategy
        method1 = synth.type_text("hi", method="auto")
        assert method1 == "keysym"
        assert clipboard_box == []
        assert backend.events == [
            ("key", ord("h"), True),
            ("key", ord("h"), False),
            ("key", ord("i"), True),
            ("key", ord("i"), False),
        ]

        # 2. Multi-line or long text -> clipboard + Ctrl+V strategy
        backend.events.clear()
        long_code = "def hello():\n    return 42\n"
        method2 = synth.type_text(long_code, method="auto")
        assert method2 == "clipboard"
        assert clipboard_box == [long_code]
        assert backend.events == [
            ("key", XK_CONTROL_L, True),
            ("key", ord("v"), True),
            ("key", ord("v"), False),
            ("key", XK_CONTROL_L, False),
        ]

    def test_press_key_single_and_combo(self) -> None:
        backend = FakeInputBackend()
        synth = KeysymSynthesizer(backend, key_delay=0)

        sym = synth.press_key("Return")
        assert sym == 0xFF0D
        assert backend.events == [
            ("key", 0xFF0D, True),
            ("key", 0xFF0D, False),
        ]

        backend.events.clear()
        sym2 = synth.press_key("Ctrl+c")
        assert sym2 == ord("c")
        assert backend.events == [
            ("key", XK_CONTROL_L, True),
            ("key", ord("c"), True),
            ("key", ord("c"), False),
            ("key", XK_CONTROL_L, False),
        ]

    def test_type_text_keysyms_uppercase_shift(self) -> None:
        backend = FakeInputBackend()
        synth = KeysymSynthesizer(backend, key_delay=0)

        count = synth.type_text_keysyms("Hi")
        assert count == 2
        assert backend.events == [
            ("key", XK_SHIFT_L, True),
            ("key", ord("H"), True),
            ("key", ord("H"), False),
            ("key", XK_SHIFT_L, False),
            ("key", ord("i"), True),
            ("key", ord("i"), False),
        ]

        # Verify Shift is tracked in active_modifiers and released if char press fails
        fail_backend = FakeInputBackend(fail_on_keysym=ord("H"))
        fail_synth = KeysymSynthesizer(fail_backend, key_delay=0)
        with pytest.raises(RuntimeError, match="Simulated D-Bus failure"):
            fail_synth.type_text_keysyms("Hi")
        assert fail_synth.active_modifiers == ()
        assert fail_backend.events == [
            ("key", XK_SHIFT_L, True),
            ("key", XK_SHIFT_L, False),
        ]

