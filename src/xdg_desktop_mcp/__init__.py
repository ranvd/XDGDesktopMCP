"""XDG Desktop MCP — Linux desktop observation and control via XDG Desktop Portals."""

from .input import KeysymSynthesizer, UnsafeInputError, normalize_to_display
from .portal import (
    CursorMode,
    DeviceType,
    PortalError,
    PortalRejectedError,
    PortalSession,
    PortalSessionClosedError,
    PortalSessionInfo,
    SourceType,
    TargetDisplay,
)
from .stream import DisplayMetadata, FrameResult, ScreenCastStream, StreamError

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "CursorMode",
    "DeviceType",
    "DisplayMetadata",
    "FrameResult",
    "KeysymSynthesizer",
    "PortalError",
    "PortalRejectedError",
    "PortalSession",
    "PortalSessionClosedError",
    "PortalSessionInfo",
    "ScreenCastStream",
    "SourceType",
    "StreamError",
    "TargetDisplay",
    "UnsafeInputError",
    "normalize_to_display",
]
