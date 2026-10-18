"""Unit tests for D-Bus portal request structures, tokens, and session lifecycle."""

from __future__ import annotations

import os
from typing import Any

import gi
import pytest

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from xdg_desktop_mcp.portal import (
    CursorMode,
    DeviceType,
    PortalError,
    PortalRejectedError,
    PortalSession,
    PortalSessionClosedError,
    REQUEST_IFACE,
    SESSION_IFACE,
    SourceType,
    build_create_session_options,
    build_request_path,
    build_select_devices_options,
    build_select_sources_options,
    build_session_path,
    parse_streams_variant,
    sanitize_sender_name,
)


class FakeFDList:
    def __init__(self, fd: int) -> None:
        self._fd = fd

    def get(self, index: int) -> int:
        assert index == 0
        return os.dup(self._fd)


class FakeDBusConnection:
    """Simulated Gio.DBusConnection for unit testing PortalSession without a desktop dialog."""

    def __init__(self, unique_name: str = ":1.99", response_code: int = 0) -> None:
        self._unique_name = unique_name
        self.response_code = response_code
        self.subscriptions: dict[int, dict[str, Any]] = {}
        self._next_sub_id = 1
        self.calls: list[tuple[str, str, Any]] = []
        self._pipe_r, self._pipe_w = os.pipe()

    def close_pipes(self) -> None:
        for fd in (self._pipe_r, self._pipe_w):
            try:
                os.close(fd)
            except OSError:
                pass

    def get_unique_name(self) -> str:
        return self._unique_name

    def signal_subscribe(
        self,
        sender: str,
        interface_name: str,
        member: str,
        object_path: str,
        arg0: Any,
        flags: Any,
        callback: Any,
        *user_data: Any,
    ) -> int:
        sub_id = self._next_sub_id
        self._next_sub_id += 1
        self.subscriptions[sub_id] = {
            "interface": interface_name,
            "member": member,
            "object_path": object_path,
            "callback": callback,
        }
        return sub_id

    def signal_unsubscribe(self, sub_id: int) -> None:
        self.subscriptions.pop(sub_id, None)

    def call_sync(
        self,
        bus_name: str,
        object_path: str,
        interface_name: str,
        method_name: str,
        parameters: GLib.Variant | None,
        reply_type: Any,
        flags: Any,
        timeout_msec: int,
        cancellable: Any,
    ) -> GLib.Variant | None:
        unpacked = parameters.unpack() if parameters is not None else ()
        self.calls.append((interface_name, method_name, unpacked))

        if method_name in ("CreateSession", "SelectDevices", "SelectSources", "Start"):
            opts = unpacked[-1]
            req_token = opts["handle_token"]
            req_path = build_request_path(self._unique_name, req_token)

            results: dict[str, GLib.Variant] = {}
            if method_name == "CreateSession":
                ses_token = opts["session_handle_token"]
                ses_path = build_session_path(self._unique_name, ses_token)
                results["session_handle"] = GLib.Variant("s", ses_path)
            elif method_name == "Start":
                stream_props = {
                    "size": GLib.Variant("(ii)", (1920, 1080)),
                    "position": GLib.Variant("(ii)", (0, 0)),
                    "source_type": GLib.Variant("u", int(SourceType.MONITOR)),
                }
                results["streams"] = GLib.Variant("a(ua{sv})", [(42, stream_props)])
                results["devices"] = GLib.Variant(
                    "u", int(DeviceType.KEYBOARD | DeviceType.POINTER)
                )
                results["restore_token"] = GLib.Variant("s", "tok_restore_123")

            for sub in list(self.subscriptions.values()):
                if (
                    sub["interface"] == REQUEST_IFACE
                    and sub["member"] == "Response"
                    and sub["object_path"] == req_path
                ):
                    signal_params = GLib.Variant(
                        "(ua{sv})", (self.response_code, results)
                    )
                    sub["callback"](
                        self,
                        bus_name,
                        req_path,
                        REQUEST_IFACE,
                        "Response",
                        signal_params,
                    )

            return GLib.Variant("(o)", (req_path,))

        return None

    def call_with_unix_fd_list_sync(
        self,
        bus_name: str,
        object_path: str,
        interface_name: str,
        method_name: str,
        parameters: GLib.Variant,
        reply_type: Any,
        flags: Any,
        timeout_msec: int,
        fd_list: Any,
        cancellable: Any,
    ) -> tuple[GLib.Variant, FakeFDList]:
        self.calls.append((interface_name, method_name, parameters.unpack()))
        return GLib.Variant("(h)", (0,)), FakeFDList(self._pipe_r)

    def emit_session_closed(self, session_handle: str) -> None:
        for sub in list(self.subscriptions.values()):
            if (
                sub["interface"] == SESSION_IFACE
                and sub["member"] == "Closed"
                and sub["object_path"] == session_handle
            ):
                sub["callback"](
                    self,
                    "org.freedesktop.portal.Desktop",
                    session_handle,
                    SESSION_IFACE,
                    "Closed",
                    GLib.Variant("()", ()),
                )


class TestPortalPayloadsAndPaths:
    def test_sanitize_sender_and_build_paths(self) -> None:
        assert sanitize_sender_name(":1.42") == "1_42"
        assert (
            build_request_path(":1.42", "req_abc")
            == "/org/freedesktop/portal/desktop/request/1_42/req_abc"
        )
        assert (
            build_session_path(":1.42", "ses_xyz")
            == "/org/freedesktop/portal/desktop/session/1_42/ses_xyz"
        )

    def test_create_session_and_select_options_variants(self) -> None:
        create_var = build_create_session_options("req_1", "ses_1")
        assert create_var.get_type_string() == "(a{sv})"
        unpacked = create_var.unpack()[0]
        assert unpacked["handle_token"] == "req_1"
        assert unpacked["session_handle_token"] == "ses_1"

        dev_opts = build_select_devices_options(
            "req_2",
            device_types=int(DeviceType.KEYBOARD | DeviceType.POINTER),
            restore_token="saved_tok",
        )
        assert dev_opts["handle_token"].unpack() == "req_2"
        assert dev_opts["types"].unpack() == 3
        assert dev_opts["persist_mode"].unpack() == 2
        assert dev_opts["restore_token"].unpack() == "saved_tok"

        src_opts = build_select_sources_options("req_3")
        assert src_opts["handle_token"].unpack() == "req_3"
        # ADR 0003: Embedded cursor mode (2) must be enabled by default
        assert src_opts["cursor_mode"].unpack() == int(CursorMode.EMBEDDED)
        assert src_opts["multiple"].unpack() is False

    def test_parse_streams_variant(self) -> None:
        td = parse_streams_variant(
            {
                "streams": [
                    (
                        77,
                        {
                            "size": (2560, 1440),
                            "position": (100, 0),
                            "source_type": 1,
                            "mapping_id": "DP-1",
                        },
                    )
                ]
            }
        )
        assert td.node_id == 77
        assert td.width == 2560
        assert td.height == 1440
        assert td.position == (100, 0)
        assert td.source_type == 1
        assert td.mapping_id == "DP-1"

        with pytest.raises(PortalError, match="returned no ScreenCast streams"):
            parse_streams_variant({"streams": []})


class TestPortalSessionLifecycle:
    async def test_full_session_handshake_and_session_closed_signal(self) -> None:
        fake_conn = FakeDBusConnection()
        closed_events: list[str] = []
        session = PortalSession(
            connection=fake_conn,  # type: ignore[arg-type]
            on_closed=lambda: closed_events.append("closed"),
        )
        try:
            info = await session.start()
            assert session.is_active
            assert info.target_display.node_id == 42
            assert info.target_display.width == 1920
            assert info.target_display.height == 1080
            assert info.restore_token == "tok_restore_123"
            assert info.pipewire_fd >= 0

            # Test RemoteDesktop notifications over the active session
            session.notify_pointer_motion_absolute(500.0, 300.0)
            session.notify_pointer_button(0x110, True)
            session.notify_pointer_button(0x110, False)
            session.notify_keyboard_keysym(0xFF0D, True)
            session.notify_keyboard_keysym(0xFF0D, False)

            # Simulate user clicking "Stop Sharing" in the desktop environment (ADR 0003)
            fake_conn.emit_session_closed(info.session_handle)
            assert not session.is_active
            assert closed_events == ["closed"]

            with pytest.raises(PortalSessionClosedError):
                session.notify_pointer_button(0x110, True)
        finally:
            fake_conn.close_pipes()

    async def test_user_rejection_raises_portal_rejected_error(self) -> None:
        fake_conn = FakeDBusConnection(response_code=1)
        session = PortalSession(connection=fake_conn)  # type: ignore[arg-type]
        try:
            with pytest.raises(PortalRejectedError, match="User cancelled"):
                await session.start()
            assert not session.is_active
        finally:
            fake_conn.close_pipes()
