"""D-Bus Portal Layer for RemoteDesktop and ScreenCast unified session lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import IntFlag
import logging
import os
import threading
import uuid
from typing import Any, Callable

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

logger = logging.getLogger(__name__)

PORTAL_BUS_NAME = "org.freedesktop.portal.Desktop"
PORTAL_OBJECT_PATH = "/org/freedesktop/portal/desktop"
REMOTE_DESKTOP_IFACE = "org.freedesktop.portal.RemoteDesktop"
SCREEN_CAST_IFACE = "org.freedesktop.portal.ScreenCast"
REQUEST_IFACE = "org.freedesktop.portal.Request"
SESSION_IFACE = "org.freedesktop.portal.Session"


class SourceType(IntFlag):
    """ScreenCast source types bitmask."""

    MONITOR = 1
    WINDOW = 2
    VIRTUAL = 4


class CursorMode(IntFlag):
    """ScreenCast cursor mode bitmask."""

    HIDDEN = 1
    EMBEDDED = 2
    METADATA = 4


class DeviceType(IntFlag):
    """RemoteDesktop device types bitmask."""

    KEYBOARD = 1
    POINTER = 2
    TOUCHSCREEN = 4


class PortalError(RuntimeError):
    """Base error for XDG Desktop Portal operations."""


class PortalRejectedError(PortalError):
    """Raised when the user cancels or denies the portal authorization dialog."""


class PortalSessionClosedError(PortalError):
    """Raised when attempting an operation on a closed or revoked Portal Session."""


@dataclass(frozen=True)
class TargetDisplay:
    """Metadata for the Target Display negotiated during the Screen Cast handshake."""

    node_id: int
    width: int | None = None
    height: int | None = None
    position: tuple[int, int] = (0, 0)
    source_type: int | None = None
    mapping_id: str | None = None


@dataclass
class PortalSessionInfo:
    """Active Portal Session state yielded after handshake completion."""

    session_handle: str
    pipewire_fd: int
    target_display: TargetDisplay
    devices: int
    restore_token: str | None = None


def sanitize_sender_name(unique_name: str) -> str:
    """Convert a D-Bus unique name like ':1.123' into '1_123' for portal object paths."""
    cleaned = unique_name.lstrip(":")
    return cleaned.replace(".", "_")


def generate_token(prefix: str = "xdg_mcp") -> str:
    """Generate a D-Bus-safe handle token."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def build_request_path(unique_name: str, handle_token: str) -> str:
    """Construct the expected org.freedesktop.portal.Request object path."""
    sender = sanitize_sender_name(unique_name)
    return f"{PORTAL_OBJECT_PATH}/request/{sender}/{handle_token}"


def build_session_path(unique_name: str, session_token: str) -> str:
    """Construct the expected org.freedesktop.portal.Session object path."""
    sender = sanitize_sender_name(unique_name)
    return f"{PORTAL_OBJECT_PATH}/session/{sender}/{session_token}"


def build_create_session_options(
    handle_token: str, session_handle_token: str
) -> GLib.Variant:
    """Build the a{sv} options dictionary for CreateSession."""
    return GLib.Variant(
        "(a{sv})",
        (
            {
                "handle_token": GLib.Variant("s", handle_token),
                "session_handle_token": GLib.Variant("s", session_handle_token),
            },
        ),
    )


def build_select_devices_options(
    handle_token: str,
    device_types: int = int(DeviceType.KEYBOARD | DeviceType.POINTER),
    restore_token: str | None = None,
    persist_mode: int = 2,
) -> dict[str, GLib.Variant]:
    """Build the a{sv} options dict for RemoteDesktop.SelectDevices."""
    opts: dict[str, GLib.Variant] = {
        "handle_token": GLib.Variant("s", handle_token),
        "types": GLib.Variant("u", int(device_types)),
        "persist_mode": GLib.Variant("u", int(persist_mode)),
    }
    if restore_token:
        opts["restore_token"] = GLib.Variant("s", restore_token)
    return opts


def build_select_sources_options(
    handle_token: str,
    source_types: int = int(SourceType.MONITOR | SourceType.WINDOW),
    multiple: bool = False,
    cursor_mode: int = int(CursorMode.EMBEDDED),
) -> dict[str, GLib.Variant]:
    """Build the a{sv} options dict for ScreenCast.SelectSources (ADR 0003: Embedded cursor)."""
    return {
        "handle_token": GLib.Variant("s", handle_token),
        "types": GLib.Variant("u", int(source_types)),
        "multiple": GLib.Variant("b", bool(multiple)),
        "cursor_mode": GLib.Variant("u", int(cursor_mode)),
    }


def parse_streams_variant(results: dict[str, Any]) -> TargetDisplay:
    """Extract TargetDisplay from the 'streams' result of RemoteDesktop/ScreenCast Start."""
    streams = results.get("streams")
    if not streams:
        raise PortalError(
            "Portal Start succeeded but returned no ScreenCast streams. "
            "Ensure a display or window was selected in the portal dialog."
        )
    first_stream = streams[0]
    node_id = int(first_stream[0])
    props: dict[str, Any] = dict(first_stream[1]) if len(first_stream) > 1 else {}

    size = props.get("size")
    width: int | None = None
    height: int | None = None
    if isinstance(size, (tuple, list)) and len(size) == 2:
        width = int(size[0])
        height = int(size[1])

    pos = props.get("position")
    position = (0, 0)
    if isinstance(pos, (tuple, list)) and len(pos) == 2:
        position = (int(pos[0]), int(pos[1]))

    source_type = props.get("source_type")
    if source_type is not None:
        source_type = int(source_type)

    mapping_id = props.get("mapping_id")
    if mapping_id is not None:
        mapping_id = str(mapping_id)

    return TargetDisplay(
        node_id=node_id,
        width=width,
        height=height,
        position=position,
        source_type=source_type,
        mapping_id=mapping_id,
    )


class _GLibMainLoopThread:
    """Singleton background thread running the default GLib.MainLoop for D-Bus signals."""

    _instance: _GLibMainLoopThread | None = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._context = GLib.MainContext.default()
        self._loop = GLib.MainLoop.new(self._context, False)
        self._thread = threading.Thread(
            target=self._run, name="xdg-portal-glib-loop", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        self._loop.run()

    @classmethod
    def ensure_running(cls) -> _GLibMainLoopThread:
        with cls._lock:
            if cls._instance is None or not cls._instance._thread.is_alive():
                cls._instance = cls()
            return cls._instance


class PortalSession:
    """Manages a persistent combined RemoteDesktop and ScreenCast Portal Session over D-Bus."""

    def __init__(
        self,
        connection: Gio.DBusConnection | None = None,
        on_closed: Callable[[], None] | None = None,
    ) -> None:
        self._conn = connection
        self._on_closed_callbacks: list[Callable[[], None]] = []
        if on_closed is not None:
            self._on_closed_callbacks.append(on_closed)

        self._session_info: PortalSessionInfo | None = None
        self._closed_sub_id: int | None = None
        self._lock = asyncio.Lock()

    @property
    def is_active(self) -> bool:
        """Return True if the Portal Session is currently established and open."""
        return self._session_info is not None

    @property
    def session_info(self) -> PortalSessionInfo:
        """Return the active PortalSessionInfo or raise PortalSessionClosedError."""
        if self._session_info is None:
            raise PortalSessionClosedError(
                "No active Portal Session. Call start_session first or allow lazy initialization."
            )
        return self._session_info

    def add_closed_listener(self, callback: Callable[[], None]) -> None:
        """Register a callback to be invoked if the user or compositor revokes the session."""
        self._on_closed_callbacks.append(callback)

    def _ensure_connection(self) -> Gio.DBusConnection:
        _GLibMainLoopThread.ensure_running()
        if self._conn is None:
            try:
                self._conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            except Exception as exc:
                raise PortalError(
                    f"Failed to connect to D-Bus session bus: {exc}"
                ) from exc
        return self._conn

    async def _call_portal_request(
        self,
        interface_name: str,
        method_name: str,
        parameters: GLib.Variant,
        handle_token: str,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        """Invoke an XDG Portal method that follows the Request/Response pattern."""
        conn = self._ensure_connection()
        unique_name = conn.get_unique_name()
        if not unique_name:
            raise PortalError("D-Bus connection does not have a unique name.")

        expected_request_path = build_request_path(unique_name, handle_token)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[int, dict[str, Any]]] = loop.create_future()

        def _on_response(
            _connection: Gio.DBusConnection,
            _sender_name: str,
            _object_path: str,
            _interface_name: str,
            _signal_name: str,
            params: GLib.Variant,
            *_user_data: Any,
        ) -> None:
            try:
                unpacked = params.unpack()
                response_code = int(unpacked[0])
                results_dict = dict(unpacked[1]) if len(unpacked) > 1 else {}
                if not future.done():
                    loop.call_soon_threadsafe(
                        future.set_result, (response_code, results_dict)
                    )
            except Exception as exc:
                if not future.done():
                    loop.call_soon_threadsafe(future.set_exception, exc)

        sub_id = conn.signal_subscribe(
            PORTAL_BUS_NAME,
            REQUEST_IFACE,
            "Response",
            expected_request_path,
            None,
            Gio.DBusSignalFlags.NO_MATCH_RULE,
            _on_response,
        )
        alt_sub_id: int | None = None

        try:
            reply = conn.call_sync(
                PORTAL_BUS_NAME,
                PORTAL_OBJECT_PATH,
                interface_name,
                method_name,
                parameters,
                GLib.VariantType.new("(o)"),
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
            actual_request_path = reply.unpack()[0]
            if actual_request_path != expected_request_path and not future.done():
                alt_sub_id = conn.signal_subscribe(
                    PORTAL_BUS_NAME,
                    REQUEST_IFACE,
                    "Response",
                    actual_request_path,
                    None,
                    Gio.DBusSignalFlags.NO_MATCH_RULE,
                    _on_response,
                )

            response_code, results = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise PortalError(
                f"Timed out waiting for portal response to {interface_name}.{method_name} "
                f"after {timeout}s. Was the desktop authorization dialog left unanswered?"
            ) from exc
        except GLib.Error as exc:
            raise PortalError(
                f"D-Bus error calling {interface_name}.{method_name}: {exc.message}"
            ) from exc
        finally:
            conn.signal_unsubscribe(sub_id)
            if alt_sub_id is not None:
                conn.signal_unsubscribe(alt_sub_id)

        if response_code == 1:
            raise PortalRejectedError(
                f"User cancelled the desktop portal authorization dialog during {method_name}."
            )
        if response_code != 0:
            raise PortalError(
                f"Portal request {interface_name}.{method_name} failed with response code {response_code}."
            )
        return results

    async def start(
        self,
        source_types: int = int(SourceType.MONITOR | SourceType.WINDOW),
        device_types: int = int(DeviceType.KEYBOARD | DeviceType.POINTER),
        cursor_mode: int = int(CursorMode.EMBEDDED),
        restore_token: str | None = None,
    ) -> PortalSessionInfo:
        """Create and start a combined RemoteDesktop + ScreenCast session."""
        async with self._lock:
            if self._session_info is not None:
                return self._session_info

            conn = self._ensure_connection()

            # 1. CreateSession on RemoteDesktop
            req_token_create = generate_token("req_create")
            ses_token = generate_token("ses")
            create_params = build_create_session_options(req_token_create, ses_token)
            create_results = await self._call_portal_request(
                REMOTE_DESKTOP_IFACE,
                "CreateSession",
                create_params,
                req_token_create,
            )
            session_handle = create_results.get("session_handle")
            if not session_handle:
                unique_name = conn.get_unique_name() or ""
                session_handle = build_session_path(unique_name, ses_token)

            # Subscribe to Session.Closed before starting
            self._subscribe_session_closed(session_handle)

            try:
                # 2. SelectDevices on RemoteDesktop
                req_token_devices = generate_token("req_dev")
                dev_opts = build_select_devices_options(
                    req_token_devices,
                    device_types=device_types,
                    restore_token=restore_token,
                )
                await self._call_portal_request(
                    REMOTE_DESKTOP_IFACE,
                    "SelectDevices",
                    GLib.Variant("(oa{sv})", (session_handle, dev_opts)),
                    req_token_devices,
                )

                # 3. SelectSources on ScreenCast (attached to the same session_handle)
                req_token_sources = generate_token("req_src")
                src_opts = build_select_sources_options(
                    req_token_sources,
                    source_types=source_types,
                    multiple=False,
                    cursor_mode=cursor_mode,
                )
                await self._call_portal_request(
                    SCREEN_CAST_IFACE,
                    "SelectSources",
                    GLib.Variant("(oa{sv})", (session_handle, src_opts)),
                    req_token_sources,
                )

                # 4. Start on RemoteDesktop (triggers unified authorization dialog)
                req_token_start = generate_token("req_start")
                start_opts = {"handle_token": GLib.Variant("s", req_token_start)}
                start_results = await self._call_portal_request(
                    REMOTE_DESKTOP_IFACE,
                    "Start",
                    GLib.Variant("(osa{sv})", (session_handle, "", start_opts)),
                    req_token_start,
                )

                target_display = parse_streams_variant(start_results)
                granted_devices = int(start_results.get("devices", device_types))
                new_restore_token = start_results.get("restore_token")
                if new_restore_token is not None:
                    new_restore_token = str(new_restore_token)

                # 5. OpenPipeWireRemote on ScreenCast to retrieve the PipeWire FD
                pipewire_fd = self._open_pipewire_remote(session_handle)

                self._session_info = PortalSessionInfo(
                    session_handle=session_handle,
                    pipewire_fd=pipewire_fd,
                    target_display=target_display,
                    devices=granted_devices,
                    restore_token=new_restore_token,
                )
                logger.info(
                    "Portal Session established: handle=%s, node_id=%s, size=%sx%s",
                    session_handle,
                    target_display.node_id,
                    target_display.width,
                    target_display.height,
                )
                return self._session_info
            except Exception:
                self._cleanup_session_state(close_dbus=True, session_handle=session_handle)
                raise

    def _open_pipewire_remote(self, session_handle: str) -> int:
        """Call org.freedesktop.portal.ScreenCast.OpenPipeWireRemote and extract the Unix FD."""
        conn = self._ensure_connection()
        try:
            reply, fd_list = conn.call_with_unix_fd_list_sync(
                PORTAL_BUS_NAME,
                PORTAL_OBJECT_PATH,
                SCREEN_CAST_IFACE,
                "OpenPipeWireRemote",
                GLib.Variant("(oa{sv})", (session_handle, {})),
                GLib.VariantType.new("(h)"),
                Gio.DBusCallFlags.NONE,
                -1,
                None,
                None,
            )
            fd_index = reply.unpack()[0]
            if fd_list is None:
                raise PortalError("OpenPipeWireRemote returned no UnixFDList.")
            # fd_list.get() duplicates the file descriptor so Python owns the returned FD
            return int(fd_list.get(fd_index))
        except GLib.Error as exc:
            raise PortalError(
                f"Failed to open PipeWire remote FD: {exc.message}"
            ) from exc

    def _subscribe_session_closed(self, session_handle: str) -> None:
        conn = self._ensure_connection()
        if self._closed_sub_id is not None:
            conn.signal_unsubscribe(self._closed_sub_id)
            self._closed_sub_id = None

        def _on_session_closed(
            _connection: Gio.DBusConnection,
            _sender_name: str,
            _object_path: str,
            _interface_name: str,
            _signal_name: str,
            _params: GLib.Variant,
            *_user_data: Any,
        ) -> None:
            logger.warning("Portal Session %s was closed by the compositor/user.", session_handle)
            self._cleanup_session_state(close_dbus=False)
            self._dispatch_closed_callbacks()

        self._closed_sub_id = conn.signal_subscribe(
            PORTAL_BUS_NAME,
            SESSION_IFACE,
            "Closed",
            session_handle,
            None,
            Gio.DBusSignalFlags.NO_MATCH_RULE,
            _on_session_closed,
        )

    def _dispatch_closed_callbacks(self) -> None:
        """Invoke all registered on_closed callbacks safely."""
        for cb in list(self._on_closed_callbacks):
            try:
                cb()
            except Exception:
                logger.exception("Error in PortalSession on_closed callback")

    def _cleanup_session_state(
        self, close_dbus: bool = False, session_handle: str | None = None
    ) -> None:
        info = self._session_info
        self._session_info = None
        handle = session_handle or (info.session_handle if info else None)

        if self._conn is not None and self._closed_sub_id is not None:
            try:
                self._conn.signal_unsubscribe(self._closed_sub_id)
            except Exception:
                pass
            self._closed_sub_id = None

        if close_dbus and self._conn is not None and handle:
            try:
                self._conn.call_sync(
                    PORTAL_BUS_NAME,
                    handle,
                    SESSION_IFACE,
                    "Close",
                    None,
                    None,
                    Gio.DBusCallFlags.NONE,
                    -1,
                    None,
                )
            except Exception:
                pass

        if info is not None and info.pipewire_fd >= 0:
            try:
                os.close(info.pipewire_fd)
            except OSError:
                pass

    def close(self) -> None:
        """Explicitly close the Portal Session and release its D-Bus handle and FD."""
        self._cleanup_session_state(close_dbus=True)
        self._dispatch_closed_callbacks()

    # --- RemoteDesktop Input Notification Primitives ---

    def _call_remote_desktop_notify(
        self, method_name: str, variant_type: str, payload: tuple[Any, ...]
    ) -> None:
        """Invoke a synchronous RemoteDesktop input notification method."""
        conn = self._ensure_connection()
        try:
            conn.call_sync(
                PORTAL_BUS_NAME,
                PORTAL_OBJECT_PATH,
                REMOTE_DESKTOP_IFACE,
                method_name,
                GLib.Variant(variant_type, payload),
                None,
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
        except GLib.Error as exc:
            raise PortalError(f"{method_name} failed: {exc.message}") from exc

    def notify_pointer_motion_absolute(self, x: float, y: float) -> None:
        """Send NotifyPointerMotionAbsolute for the active Target Display stream."""
        info = self.session_info
        self._call_remote_desktop_notify(
            "NotifyPointerMotionAbsolute",
            "(oa{sv}udd)",
            (
                info.session_handle,
                {},
                int(info.target_display.node_id),
                float(x),
                float(y),
            ),
        )

    def notify_pointer_button(self, button: int, pressed: bool) -> None:
        """Send NotifyPointerButton (evdev button code, state: 1=pressed, 0=released)."""
        info = self.session_info
        state = 1 if pressed else 0
        self._call_remote_desktop_notify(
            "NotifyPointerButton",
            "(oa{sv}iu)",
            (info.session_handle, {}, int(button), int(state)),
        )

    def notify_pointer_axis_discrete(self, axis: int, steps: int) -> None:
        """Send NotifyPointerAxisDiscrete (axis: 0=vertical, 1=horizontal)."""
        info = self.session_info
        self._call_remote_desktop_notify(
            "NotifyPointerAxisDiscrete",
            "(oa{sv}ui)",
            (info.session_handle, {}, int(axis), int(steps)),
        )

    def notify_keyboard_keysym(self, keysym: int, pressed: bool) -> None:
        """Send NotifyKeyboardKeysym (X11 keysym, state: 1=pressed, 0=released)."""
        info = self.session_info
        state = 1 if pressed else 0
        self._call_remote_desktop_notify(
            "NotifyKeyboardKeysym",
            "(oa{sv}iu)",
            (info.session_handle, {}, int(keysym), int(state)),
        )

