"""FastMCP / MCPServer exposing high-level Linux desktop observation and control tools."""

from __future__ import annotations

import json
import logging
from typing import Any

try:
    from mcp.server.mcpserver import Image, MCPServer as FastMCP
except ImportError:  # pragma: no cover - fallback for mcp<2.0
    from mcp.server.fastmcp import FastMCP, Image  # type: ignore[no-redef]

from .input import KeysymSynthesizer, UnsafeInputError
from .portal import (
    CursorMode,
    DeviceType,
    PortalError,
    PortalRejectedError,
    PortalSession,
    PortalSessionInfo,
    SourceType,
)
from .stream import DisplayMetadata, ScreenCastStream, StreamError

logger = logging.getLogger(__name__)


def _parse_flags_from_string(
    text: str,
    token_map: dict[str, int],
    default_mask: int,
    entity_name: str,
    valid_hint: str,
) -> int:
    mask = 0
    for part in text.lower().replace("|", ",").split(","):
        token = part.strip()
        if not token:
            continue
        if token in token_map:
            mask |= token_map[token]
        else:
            raise ValueError(f"Unknown {entity_name} '{token}'. Valid values: {valid_hint}.")
    return mask or default_mask


def _parse_source_types(sources: str | int) -> int:
    if isinstance(sources, int):
        return sources
    source_map = {
        "monitor": int(SourceType.MONITOR),
        "screen": int(SourceType.MONITOR),
        "display": int(SourceType.MONITOR),
        "window": int(SourceType.WINDOW),
        "virtual": int(SourceType.VIRTUAL),
        "all": int(SourceType.MONITOR | SourceType.WINDOW),
    }
    return _parse_flags_from_string(
        sources,
        source_map,
        int(SourceType.MONITOR | SourceType.WINDOW),
        "source type",
        "'monitor', 'window', 'virtual'",
    )


def _parse_device_types(devices: str | int) -> int:
    if isinstance(devices, int):
        return devices
    device_map = {
        "keyboard": int(DeviceType.KEYBOARD),
        "keys": int(DeviceType.KEYBOARD),
        "pointer": int(DeviceType.POINTER),
        "mouse": int(DeviceType.POINTER),
        "touchscreen": int(DeviceType.TOUCHSCREEN),
        "touch": int(DeviceType.TOUCHSCREEN),
        "all": int(DeviceType.KEYBOARD | DeviceType.POINTER | DeviceType.TOUCHSCREEN),
    }
    return _parse_flags_from_string(
        devices,
        device_map,
        int(DeviceType.KEYBOARD | DeviceType.POINTER),
        "device type",
        "'keyboard', 'pointer', 'touchscreen'",
    )


class DesktopController:
    """Coordinates PortalSession, ScreenCastStream, and KeysymSynthesizer lifecycle."""

    def __init__(
        self,
        portal_session: PortalSession | None = None,
        synthesizer: KeysymSynthesizer | None = None,
    ) -> None:
        self.portal = portal_session or PortalSession()
        self.portal.add_closed_listener(self._on_portal_closed)
        self.input = synthesizer or KeysymSynthesizer(self.portal)
        self._stream: ScreenCastStream | None = None
        self._restore_token: str | None = None

    def _on_portal_closed(self) -> None:
        """Tear down GStreamer pipeline when the Portal Session closes (ADR 0003)."""
        if self._stream is not None:
            self._stream.stop()
            self._stream = None

    async def ensure_session(
        self,
        sources: str | int = "monitor,window",
        devices: str | int = "keyboard,pointer",
    ) -> PortalSessionInfo:
        """Ensure an active Portal Session and PipeWire stream exist (lazy initialization)."""
        if self.portal.is_active and self._stream is not None and self._stream.is_running:
            return self.portal.session_info

        src_mask = _parse_source_types(sources)
        dev_mask = _parse_device_types(devices)

        info = await self.portal.start(
            source_types=src_mask,
            device_types=dev_mask,
            cursor_mode=int(CursorMode.EMBEDDED),
            restore_token=self._restore_token,
        )
        if info.restore_token:
            self._restore_token = info.restore_token

        if self._stream is None or not self._stream.is_running:
            self._stream = ScreenCastStream(
                node_id=info.target_display.node_id,
                pipewire_fd=info.pipewire_fd,
                initial_width=info.target_display.width,
                initial_height=info.target_display.height,
            )
            self._stream.start()

        return info

    async def get_dimensions(self) -> tuple[int, int]:
        """Return (width, height) of the active Target Display."""
        info = await self.ensure_session()
        if info.target_display.width and info.target_display.height:
            return info.target_display.width, info.target_display.height
        assert self._stream is not None
        meta = self._stream.get_display_metadata()
        return meta.width, meta.height

    async def get_display_metadata(self) -> DisplayMetadata:
        """Return full DisplayMetadata for the active Target Display."""
        info = await self.ensure_session()
        assert self._stream is not None
        return self._stream.get_display_metadata()

    def close(self) -> None:
        """Close the active stream and Portal Session."""
        if self._stream is not None:
            self._stream.stop()
            self._stream = None
        self.portal.close()


controller = DesktopController()
mcp = FastMCP("xdg-desktop-mcp")


@mcp.tool()
async def start_session(
    sources: str = "monitor,window",
    devices: str = "keyboard,pointer",
) -> dict[str, Any]:
    """Initialize or reuse the persistent XDG Desktop Portal session (ScreenCast + RemoteDesktop).

    Args:
        sources: Comma-separated capture sources ('monitor', 'window', or 'monitor,window').
        devices: Comma-separated input devices ('keyboard', 'pointer', or 'keyboard,pointer').
    """
    try:
        info = await controller.ensure_session(sources=sources, devices=devices)
        meta = await controller.get_display_metadata()
        return {
            "status": "active",
            "session_handle": info.session_handle,
            "node_id": info.target_display.node_id,
            "width": meta.width,
            "height": meta.height,
            "aspect_ratio": round(meta.aspect_ratio, 4),
            "cursor_mode": "embedded",
        }
    except PortalRejectedError as exc:
        raise RuntimeError(
            f"Portal Session authorization was denied by the user: {exc}"
        ) from exc
    except (PortalError, StreamError) as exc:
        raise RuntimeError(f"Failed to start Portal Session: {exc}") from exc


@mcp.tool(structured_output=False)
async def take_screenshot(
    format: str = "jpeg",
    max_dimension: int = 1920,
) -> list[Any]:
    """Capture the latest frame from the active Target Display with embedded cursor.

    Args:
        format: Image encoding format ('jpeg' or 'png'). Defaults to 'jpeg'.
        max_dimension: Maximum width/height in pixels for downscaling (defaults to 1920).
    """
    info = await controller.ensure_session()
    assert controller._stream is not None
    frame = controller._stream.capture_frame(fmt=format, max_dimension=max_dimension)
    metadata_json = json.dumps(frame.metadata_dict(node_id=info.target_display.node_id))
    return [
        Image(data=frame.data, format=frame.format),
        metadata_json,
    ]


@mcp.tool()
async def click(
    x: float,
    y: float,
    button: str = "left",
) -> dict[str, Any]:
    """Click a mouse button at normalized [0.0, 1.0] coordinates on the Target Display.

    Args:
        x: Normalized horizontal coordinate from 0.0 (left) to 1.0 (right).
        y: Normalized vertical coordinate from 0.0 (top) to 1.0 (bottom).
        button: Mouse button ('left', 'right', or 'middle').
    """
    width, height = await controller.get_dimensions()
    px, py = controller.input.click(x, y, width, height, button=button)
    return {
        "action": "click",
        "button": button,
        "normalized": {"x": x, "y": y},
        "target_display_coords": {"x": px, "y": py},
    }


@mcp.tool()
async def double_click(
    x: float,
    y: float,
    button: str = "left",
) -> dict[str, Any]:
    """Double-click at normalized [0.0, 1.0] coordinates on the Target Display.

    Args:
        x: Normalized horizontal coordinate from 0.0 (left) to 1.0 (right).
        y: Normalized vertical coordinate from 0.0 (top) to 1.0 (bottom).
        button: Mouse button ('left', 'right', or 'middle').
    """
    width, height = await controller.get_dimensions()
    px, py = controller.input.double_click(x, y, width, height, button=button)
    return {
        "action": "double_click",
        "button": button,
        "normalized": {"x": x, "y": y},
        "target_display_coords": {"x": px, "y": py},
    }


@mcp.tool()
async def move_cursor(x: float, y: float) -> dict[str, Any]:
    """Move the mouse pointer to normalized [0.0, 1.0] coordinates on the Target Display.

    Args:
        x: Normalized horizontal coordinate from 0.0 (left) to 1.0 (right).
        y: Normalized vertical coordinate from 0.0 (top) to 1.0 (bottom).
    """
    width, height = await controller.get_dimensions()
    px, py = controller.input.move_cursor(x, y, width, height)
    return {
        "action": "move_cursor",
        "normalized": {"x": x, "y": y},
        "target_display_coords": {"x": px, "y": py},
    }


@mcp.tool()
async def drag_and_drop(
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
    button: str = "left",
) -> dict[str, Any]:
    """Drag from normalized (start_x, start_y) to (end_x, end_y) on the Target Display.

    Args:
        start_x: Starting normalized X coordinate [0.0, 1.0].
        start_y: Starting normalized Y coordinate [0.0, 1.0].
        end_x: Ending normalized X coordinate [0.0, 1.0].
        end_y: Ending normalized Y coordinate [0.0, 1.0].
        button: Mouse button to hold during drag ('left', 'right', or 'middle').
    """
    width, height = await controller.get_dimensions()
    start_px, end_px = controller.input.drag_and_drop(
        start_x, start_y, end_x, end_y, width, height, button=button
    )
    return {
        "action": "drag_and_drop",
        "button": button,
        "start_target_coords": {"x": start_px[0], "y": start_px[1]},
        "end_target_coords": {"x": end_px[0], "y": end_px[1]},
    }


@mcp.tool()
async def scroll(
    direction: str = "down",
    amount: int = 3,
) -> dict[str, Any]:
    """Scroll the mouse wheel along the vertical or horizontal axis.

    Args:
        direction: Scroll direction ('up', 'down', 'left', or 'right').
        amount: Number of discrete scroll steps (positive integer).
    """
    await controller.ensure_session()
    controller.input.scroll(direction=direction, amount=amount)
    return {
        "action": "scroll",
        "direction": direction,
        "amount": amount,
    }


@mcp.tool()
async def press_key(key: str) -> dict[str, Any]:
    """Press and release a single key (e.g. 'Return', 'BackSpace', 'Tab', 'Escape', 'a').

    Args:
        key: Symbolic X11 key name or single character.
    """
    await controller.ensure_session()
    try:
        keysym = controller.input.press_key(key)
    except UnsafeInputError as exc:
        raise ValueError(str(exc)) from exc
    return {
        "action": "press_key",
        "key": key,
        "keysym": hex(keysym),
    }


@mcp.tool()
async def press_hotkey(keys: str) -> dict[str, Any]:
    """Press and release a key combination (e.g. 'Ctrl+Shift+T', 'Alt+F4', 'Ctrl+c').

    Args:
        keys: Plus-separated key chord string (e.g. 'Ctrl+Shift+T').
    """
    await controller.ensure_session()
    try:
        keysyms = controller.input.press_hotkey(keys)
    except UnsafeInputError as exc:
        raise ValueError(str(exc)) from exc
    return {
        "action": "press_hotkey",
        "keys": keys,
        "keysyms": [hex(k) for k in keysyms],
    }


@mcp.tool()
async def type_text(
    text: str,
    method: str = "auto",
) -> dict[str, Any]:
    """Enter text using the dual input strategy (keysyms for short strings, clipboard for bulk text).

    Args:
        text: Text content to type or paste.
        method: Input strategy: 'auto' (default), 'keysym', or 'clipboard'.
    """
    await controller.ensure_session()
    used_method = controller.input.type_text(text, method=method)
    return {
        "action": "type_text",
        "length": len(text),
        "method_used": used_method,
    }


@mcp.tool()
async def get_display_info() -> dict[str, Any]:
    """Return active Target Display resolution, aspect ratio, and portal stream metadata."""
    info = await controller.ensure_session()
    meta = await controller.get_display_metadata()
    return {
        **meta.to_dict(),
        "session_handle": info.session_handle,
        "position": {
            "x": info.target_display.position[0],
            "y": info.target_display.position[1],
        },
        "source_type": info.target_display.source_type,
        "cursor_mode": "embedded",
    }


@mcp.tool()
async def close_session() -> dict[str, str]:
    """Manually close the active Portal Session and stop the PipeWire GStreamer pipeline."""
    controller.close()
    return {"status": "closed"}
