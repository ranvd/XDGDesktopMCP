"""GStreamer PipeWire consumer and on-demand JPEG/PNG frame grabber."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import math
import os
import threading

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

Gst.init(None)

logger = logging.getLogger(__name__)


class StreamError(RuntimeError):
    """Raised when the GStreamer Screen Cast pipeline fails or cannot grab a frame."""


@dataclass(frozen=True)
class DisplayMetadata:
    """Physical dimensions and aspect ratio of the Target Display stream."""

    width: int
    height: int
    aspect_ratio: float
    node_id: int

    def to_dict(self) -> dict[str, int | float]:
        return {
            "width": self.width,
            "height": self.height,
            "aspect_ratio": round(self.aspect_ratio, 4),
            "node_id": self.node_id,
        }


@dataclass(frozen=True)
class FrameResult:
    """Encoded screenshot frame and associated Target Display metadata."""

    data: bytes
    format: str
    width: int
    height: int
    captured_width: int
    captured_height: int
    aspect_ratio: float

    def metadata_dict(self, node_id: int | None = None) -> dict[str, int | float | str]:
        meta: dict[str, int | float | str] = {
            "format": self.format,
            "width": self.width,
            "height": self.height,
            "captured_width": self.captured_width,
            "captured_height": self.captured_height,
            "aspect_ratio": round(self.aspect_ratio, 4),
        }
        if node_id is not None:
            meta["node_id"] = node_id
        return meta


def compute_scaled_dimensions(
    width: int, height: int, max_dimension: int | None
) -> tuple[int, int]:
    """Compute target (width, height) scaled down to fit within max_dimension."""
    if width <= 0 or height <= 0:
        raise ValueError(f"Invalid dimensions: {width}x{height}")
    if max_dimension is None or max_dimension <= 0:
        return width, height
    largest = max(width, height)
    if largest <= max_dimension:
        return width, height
    scale = max_dimension / float(largest)
    scaled_w = max(1, int(round(width * scale)))
    scaled_h = max(1, int(round(height * scale)))
    return scaled_w, scaled_h


def build_pipeline_description(node_id: int, fd: int) -> str:
    """Build the GStreamer launch string for PipeWire Screen Cast consumption (ADR 0001)."""
    return (
        f"pipewiresrc path={int(node_id)} fd={int(fd)} "
        "do-timestamp=true keepalive-time=100 resend-last=true ! "
        "videoconvert ! videoscale ! "
        "video/x-raw,format=RGB ! "
        "appsink name=sink max-buffers=1 drop=true sync=false emit-signals=false"
    )


def extract_sample_dimensions(sample: Gst.Sample) -> tuple[int, int]:
    """Extract (width, height) from a Gst.Sample's caps."""
    caps = sample.get_caps()
    if caps is None or caps.get_size() == 0:
        raise StreamError("GStreamer sample has no caps.")
    struct = caps.get_structure(0)
    ok_w, width = struct.get_int("width")
    ok_h, height = struct.get_int("height")
    if not (ok_w and ok_h) or width <= 0 or height <= 0:
        raise StreamError(f"Could not extract valid dimensions from caps: {caps.to_string()}")
    return int(width), int(height)


def encode_sample(
    sample: Gst.Sample,
    fmt: str = "jpeg",
    max_dimension: int | None = 1920,
    jpeg_quality: int = 85,
) -> FrameResult:
    """Scale and encode a raw Gst.Sample into JPEG or PNG bytes."""
    normalized_fmt = fmt.strip().lower()
    if normalized_fmt == "jpg":
        normalized_fmt = "jpeg"
    if normalized_fmt not in ("jpeg", "png"):
        raise ValueError(f"Unsupported image format '{fmt}'. Expected 'jpeg' or 'png'.")

    orig_w, orig_h = extract_sample_dimensions(sample)
    cap_w, cap_h = compute_scaled_dimensions(orig_w, orig_h, max_dimension)

    encoder_elem = (
        f"jpegenc quality={int(jpeg_quality)}"
        if normalized_fmt == "jpeg"
        else "pngenc compression-level=6"
    )
    launch_str = (
        "appsrc name=src ! videoconvert ! videoscale ! "
        f"video/x-raw,width={cap_w},height={cap_h} ! "
        f"{encoder_elem} ! appsink name=out max-buffers=1 drop=true sync=false"
    )

    enc_pipeline = Gst.parse_launch(launch_str)
    if enc_pipeline is None:
        raise StreamError("Failed to create GStreamer encoding pipeline.")

    try:
        ret = enc_pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise StreamError("Failed to transition encoding pipeline to PLAYING.")

        src = enc_pipeline.get_by_name("src")
        out_sink = enc_pipeline.get_by_name("out")
        if src is None or out_sink is None:
            raise StreamError("Encoding pipeline missing appsrc or appsink element.")

        src.set_property("caps", sample.get_caps())
        raw_buffer = sample.get_buffer()
        if raw_buffer is None:
            raise StreamError("Input Gst.Sample contains no buffer.")

        buf_copy = raw_buffer.copy_deep()
        src.emit("push-buffer", buf_copy)
        src.emit("end-of-stream")

        out_sample = out_sink.emit("try-pull-sample", 5 * Gst.SECOND)
        if out_sample is None:
            raise StreamError(f"Timed out encoding frame to {normalized_fmt}.")

        out_buffer = out_sample.get_buffer()
        if out_buffer is None:
            raise StreamError("Encoded Gst.Sample contains no buffer.")

        ok, map_info = out_buffer.map(Gst.MapFlags.READ)
        if not ok:
            raise StreamError("Failed to map encoded GStreamer buffer.")
        try:
            encoded_bytes = bytes(map_info.data)
        finally:
            out_buffer.unmap(map_info)

        return FrameResult(
            data=encoded_bytes,
            format=normalized_fmt,
            width=orig_w,
            height=orig_h,
            captured_width=cap_w,
            captured_height=cap_h,
            aspect_ratio=orig_w / float(orig_h),
        )
    finally:
        enc_pipeline.set_state(Gst.State.NULL)


class ScreenCastStream:
    """Manages the persistent GStreamer PipeWire consumer pipeline (ADR 0001)."""

    def __init__(
        self,
        node_id: int,
        pipewire_fd: int,
        initial_width: int | None = None,
        initial_height: int | None = None,
        pipeline_description: str | None = None,
    ) -> None:
        self._node_id = int(node_id)
        self._source_fd = int(pipewire_fd)
        self._dup_fd: int | None = None
        self._width = initial_width
        self._height = initial_height
        self._custom_pipeline_desc = pipeline_description

        self._pipeline: Gst.Element | None = None
        self._appsink: Gst.Element | None = None
        self._last_sample: Gst.Sample | None = None
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        return self._pipeline is not None

    def start(self) -> None:
        """Construct and start the GStreamer pipeline."""
        with self._lock:
            if self._pipeline is not None:
                return

            if self._custom_pipeline_desc is not None:
                launch_str = self._custom_pipeline_desc
            else:
                self._dup_fd = os.dup(self._source_fd)
                launch_str = build_pipeline_description(self._node_id, self._dup_fd)

            try:
                pipeline = Gst.parse_launch(launch_str)
            except Exception as exc:
                self._close_dup_fd()
                raise StreamError(
                    f"Failed to parse GStreamer pipeline '{launch_str}': {exc}"
                ) from exc

            if pipeline is None:
                self._close_dup_fd()
                raise StreamError("Gst.parse_launch returned None.")

            sink = pipeline.get_by_name("sink")
            if sink is None:
                pipeline.set_state(Gst.State.NULL)
                self._close_dup_fd()
                raise StreamError("GStreamer pipeline is missing 'sink' appsink element.")

            sink.set_property("max-buffers", 1)
            sink.set_property("drop", True)
            sink.set_property("sync", False)
            sink.set_property("emit-signals", False)

            ret = pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                pipeline.set_state(Gst.State.NULL)
                self._close_dup_fd()
                raise StreamError(
                    "Failed to set GStreamer Screen Cast pipeline to PLAYING state."
                )

            self._pipeline = pipeline
            self._appsink = sink
            logger.info("ScreenCastStream started for PipeWire node %s", self._node_id)

    def _close_dup_fd(self) -> None:
        if self._dup_fd is not None and self._dup_fd >= 0:
            try:
                os.close(self._dup_fd)
            except OSError:
                pass
            self._dup_fd = None

    def _pull_latest_sample_locked(self, timeout: float = 5.0) -> Gst.Sample:
        if self._pipeline is None or self._appsink is None:
            raise StreamError("ScreenCastStream is not running.")

        bus = self._pipeline.get_bus()
        if bus is not None:
            err_msg = bus.pop_filtered(Gst.MessageType.ERROR)
            if err_msg is not None:
                gerr, debug = err_msg.parse_error()
                raise StreamError(
                    f"GStreamer pipeline error: {gerr.message} ({debug})"
                )

        if self._last_sample is None:
            timeout_ns = int(max(0.1, timeout) * Gst.SECOND)
            sample = self._appsink.emit("try-pull-sample", timeout_ns)
            if sample is None:
                raise StreamError(
                    f"Timed out after {timeout}s waiting for initial frame from PipeWire node {self._node_id}."
                )
            self._last_sample = sample
        else:
            # Poll briefly for a newly rendered frame; fall back to cached frame if static
            fresh = self._appsink.emit("try-pull-sample", int(0.08 * Gst.SECOND))
            while fresh is not None:
                self._last_sample = fresh
                # Drain any immediate buffer in case another arrived
                fresh = self._appsink.emit("try-pull-sample", 0)

        w, h = extract_sample_dimensions(self._last_sample)
        self._width = w
        self._height = h
        return self._last_sample

    def capture_frame(
        self,
        fmt: str = "jpeg",
        max_dimension: int | None = 1920,
        timeout: float = 5.0,
    ) -> FrameResult:
        """Pull the latest video frame from the PipeWire stream and encode it."""
        with self._lock:
            if self._pipeline is None:
                self.start()
            sample = self._pull_latest_sample_locked(timeout=timeout)
            return encode_sample(sample, fmt=fmt, max_dimension=max_dimension)

    def get_display_metadata(self, timeout: float = 5.0) -> DisplayMetadata:
        """Return the physical resolution and aspect ratio of the Target Display."""
        with self._lock:
            if self._width is None or self._height is None or self._width <= 0 or self._height <= 0:
                if self._pipeline is None:
                    self.start()
                self._pull_latest_sample_locked(timeout=timeout)

            assert self._width is not None and self._height is not None
            return DisplayMetadata(
                width=self._width,
                height=self._height,
                aspect_ratio=self._width / float(self._height),
                node_id=self._node_id,
            )

    def stop(self) -> None:
        """Tear down the GStreamer pipeline and release buffered samples."""
        with self._lock:
            self._last_sample = None
            self._appsink = None
            if self._pipeline is not None:
                try:
                    self._pipeline.set_state(Gst.State.NULL)
                except Exception:
                    logger.exception("Error stopping GStreamer pipeline")
                self._pipeline = None
            self._close_dup_fd()
            logger.info("ScreenCastStream stopped for PipeWire node %s", self._node_id)
