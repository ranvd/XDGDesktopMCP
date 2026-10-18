"""Unit and integration tests for GStreamer stream consumer and MCP server tools."""

from __future__ import annotations

import json

import pytest

from xdg_desktop_mcp.server import mcp
from xdg_desktop_mcp.stream import (
    ScreenCastStream,
    build_pipeline_description,
    compute_scaled_dimensions,
)


class TestPipelineAndScaling:
    def test_build_pipeline_description_adr0001(self) -> None:
        desc = build_pipeline_description(node_id=58, fd=12)
        assert "pipewiresrc path=58 fd=12" in desc
        assert "max-buffers=1" in desc
        assert "drop=true" in desc
        assert "appsink name=sink" in desc

    def test_compute_scaled_dimensions(self) -> None:
        # Already within max_dimension
        assert compute_scaled_dimensions(1920, 1080, 1920) == (1920, 1080)
        assert compute_scaled_dimensions(1280, 720, 1920) == (1280, 720)
        # 4K downscaled to 1920 max dimension preserves 16:9 aspect ratio
        assert compute_scaled_dimensions(3840, 2160, 1920) == (1920, 1080)
        # Portrait monitor downscaled
        assert compute_scaled_dimensions(1440, 2560, 1280) == (720, 1280)
        # None disables downscaling
        assert compute_scaled_dimensions(3840, 2160, None) == (3840, 2160)

        with pytest.raises(ValueError, match="Invalid dimensions"):
            compute_scaled_dimensions(0, 1080, 1920)


class TestScreenCastStreamExtraction:
    def test_live_gst_frame_extraction_jpeg_and_png(self) -> None:
        test_pipeline = (
            "videotestsrc pattern=smpte ! videoconvert ! "
            "video/x-raw,format=RGB,width=1280,height=720,framerate=30/1 ! "
            "appsink name=sink max-buffers=1 drop=true sync=false"
        )
        stream = ScreenCastStream(
            node_id=99,
            pipewire_fd=-1,
            pipeline_description=test_pipeline,
        )
        try:
            stream.start()
            assert stream.is_running

            # Capture JPEG with downscaling to 640
            jpeg_frame = stream.capture_frame(fmt="jpeg", max_dimension=640)
            assert jpeg_frame.format == "jpeg"
            assert jpeg_frame.width == 1280
            assert jpeg_frame.height == 720
            assert jpeg_frame.captured_width == 640
            assert jpeg_frame.captured_height == 360
            assert round(jpeg_frame.aspect_ratio, 4) == round(1280 / 720, 4)
            # Verify valid JPEG SOI marker (0xFFD8)
            assert jpeg_frame.data[:2] == b"\xff\xd8"

            # Capture PNG at full resolution
            png_frame = stream.capture_frame(fmt="png", max_dimension=1920)
            assert png_frame.format == "png"
            assert png_frame.captured_width == 1280
            assert png_frame.captured_height == 720
            # Verify valid PNG magic header
            assert png_frame.data[:8] == b"\x89PNG\r\n\x1a\n"

            meta = stream.get_display_metadata()
            assert meta.to_dict() == {
                "width": 1280,
                "height": 720,
                "aspect_ratio": round(1280 / 720, 4),
                "node_id": 99,
            }
        finally:
            stream.stop()
            assert not stream.is_running


class TestMCPServerToolsRegistration:
    async def test_registered_tools_match_specification(self) -> None:
        tools = await mcp.list_tools()
        tool_names = {t.name for t in tools}
        expected = {
            "start_session",
            "take_screenshot",
            "click",
            "double_click",
            "move_cursor",
            "drag_and_drop",
            "scroll",
            "press_key",
            "press_hotkey",
            "type_text",
            "get_display_info",
            "close_session",
        }
        assert expected.issubset(tool_names)

    async def test_end_to_end_mcp_tool_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tests.test_portal import FakeDBusConnection
        from xdg_desktop_mcp import server
        from xdg_desktop_mcp.input import KeysymSynthesizer
        from xdg_desktop_mcp.portal import PortalSession

        fake_conn = FakeDBusConnection()
        test_portal = PortalSession(connection=fake_conn)  # type: ignore[arg-type]
        clipboard_data: list[str] = []
        test_synth = KeysymSynthesizer(
            test_portal,
            key_delay=0,
            clipboard_setter=lambda s: clipboard_data.append(s),
        )
        test_controller = server.DesktopController(
            portal_session=test_portal,
            synthesizer=test_synth,
        )

        # Use videotestsrc instead of pipewiresrc for deterministic headless CI testing
        orig_init = ScreenCastStream.__init__

        def _patched_stream_init(
            self_stream: ScreenCastStream,
            node_id: int,
            pipewire_fd: int,
            initial_width: int | None = None,
            initial_height: int | None = None,
            pipeline_description: str | None = None,
        ) -> None:
            orig_init(
                self_stream,
                node_id=node_id,
                pipewire_fd=pipewire_fd,
                initial_width=initial_width,
                initial_height=initial_height,
                pipeline_description=(
                    "videotestsrc pattern=ball ! videoconvert ! "
                    "video/x-raw,format=RGB,width=1920,height=1080,framerate=30/1 ! "
                    "appsink name=sink max-buffers=1 drop=true sync=false"
                ),
            )

        monkeypatch.setattr(ScreenCastStream, "__init__", _patched_stream_init)
        monkeypatch.setattr(server, "controller", test_controller)

        try:
            # 1. Lazy session start via take_screenshot
            shot_res = await mcp.call_tool(
                "take_screenshot", {"format": "jpeg", "max_dimension": 960}
            )
            assert not shot_res.is_error
            assert len(shot_res.content) == 2
            assert shot_res.content[0].type == "image"
            assert shot_res.content[0].mime_type == "image/jpeg"
            meta = json.loads(shot_res.content[1].text)
            assert meta["width"] == 1920
            assert meta["height"] == 1080
            assert meta["captured_width"] == 960
            assert meta["captured_height"] == 540

            # 2. Pointer & keyboard tools
            click_res = await mcp.call_tool(
                "click", {"x": 0.5, "y": 0.5, "button": "left"}
            )
            assert not click_res.is_error

            move_res = await mcp.call_tool("move_cursor", {"x": 0.25, "y": 0.75})
            assert not move_res.is_error

            scroll_res = await mcp.call_tool(
                "scroll", {"direction": "down", "amount": 2}
            )
            assert not scroll_res.is_error

            key_res = await mcp.call_tool("press_key", {"key": "Return"})
            assert not key_res.is_error

            hotkey_res = await mcp.call_tool("press_hotkey", {"keys": "Ctrl+Shift+T"})
            assert not hotkey_res.is_error

            type_res = await mcp.call_tool(
                "type_text", {"text": "line1\nline2", "method": "auto"}
            )
            assert not type_res.is_error
            assert clipboard_data == ["line1\nline2"]

            info_res = await mcp.call_tool("get_display_info", {})
            assert not info_res.is_error

            close_res = await mcp.call_tool("close_session", {})
            assert not close_res.is_error
            assert not test_portal.is_active
        finally:
            test_controller.close()
            fake_conn.close_pipes()
