# XDG Desktop MCP

An MCP server enabling AI agents to observe and control Linux desktop GUIs (Wayland & X11) through standard XDG Desktop Portals (`org.freedesktop.portal.RemoteDesktop` and `org.freedesktop.portal.ScreenCast`) over D-Bus.

## Features

- **Persistent Combined Portal Session** ([ADR 0001](./docs/adr/0001-persistent-portal-session-with-pipewire.md)): Negotiates a single unified `RemoteDesktop` + `ScreenCast` session so the user only authorizes the desktop permission dialog once per session.
- **Zero-Accumulation PipeWire Ingestion** ([ADR 0001](./docs/adr/0001-persistent-portal-session-with-pipewire.md)): Consumes live screen streams via GStreamer `pipewiresrc` into an `appsink` configured with `max-buffers=1, drop=true` and downscales/encodes frames to JPEG or PNG on demand.
- **Dual Text Input Strategy** ([ADR 0002](./docs/adr/0002-dual-text-input-strategy.md)): Uses X11 keysym synthesis for single keys, navigation, and keyboard shortcuts while automatically switching to clipboard injection (`wl-copy` / `xclip`) + synthetic `Ctrl+V` for multi-line code or long text.
- **Embedded Cursor & Lifecycle Guardrails** ([ADR 0003](./docs/adr/0003-embedded-cursor-and-session-lifecycle-guardrails.md)): Requests `cursor_mode=Embedded` for visual pointer feedback, listens to `org.freedesktop.portal.Session.Closed` to tear down pipelines cleanly when revoked, guarantees modifier release in a `finally` block, and blocks virtual-terminal switching hotkeys (`Ctrl+Alt+F1..F12`).

## Requirements

- Linux desktop with `xdg-desktop-portal` (GNOME, KDE Plasma, wlroots/Hyprland/Sway)
- System packages: Python 3.11+, PyGObject (`python3-gi`), GStreamer 1.0 with PipeWire & Good plugins (`gir1.2-gst-plugins-base-1.0`, `gstreamer1.0-pipewire`, `gstreamer1.0-plugins-good`), and `wl-clipboard` (Wayland) or `xclip` (X11)

## Installation

Create a virtual environment with `--system-site-packages` (so Python can access system `gi.repository.Gio` and `gi.repository.Gst`) and install the package:

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e ".[dev]"
```

## MCP Client Configuration

Add `xdg-desktop-mcp` to your MCP client configuration:

```json
{
  "mcpServers": {
    "xdg-desktop": {
      "command": "<your workspace>/XDGDesktopMCP/.venv/bin/xdg-desktop-mcp",
      "args": []
    }
  }
}
```

## Exposed MCP Tools

| Tool | Description |
| :--- | :--- |
| `start_session(sources, devices)` | Explicitly starts the unified portal session (also triggered lazily on first tool call). |
| `take_screenshot(format, max_dimension)` | Captures the latest frame (`jpeg` or `png`) with embedded cursor and Target Display metadata. |
| `click(x, y, button)` | Clicks (`left`, `right`, `middle`) at normalized `[0.0, 1.0]` coordinates. |
| `double_click(x, y, button)` | Double-clicks at normalized `[0.0, 1.0]` coordinates. |
| `move_cursor(x, y)` | Moves the pointer to normalized `[0.0, 1.0]` coordinates. |
| `drag_and_drop(start_x, start_y, end_x, end_y, button)` | Smoothly drags from `(start_x, start_y)` to `(end_x, end_y)`. |
| `scroll(direction, amount)` | Scrolls `up`, `down`, `left`, or `right` by discrete steps. |
| `press_key(key)` | Presses and releases a symbolic key or character (`Return`, `BackSpace`, `Escape`, etc.). |
| `press_hotkey(keys)` | Dispatches a key chord (`Ctrl+Shift+T`, `Alt+F4`, `Super+L`) with guaranteed modifier release. |
| `type_text(text, method)` | Enters text via `auto`, `keysym`, or `clipboard` strategy. |
| `get_display_info()` | Returns Target Display resolution, aspect ratio, position, and PipeWire node metadata. |
| `close_session()` | Closes the active Portal Session and stops the GStreamer pipeline. |

## Running Tests

```bash
.venv/bin/pytest -v
```

## Architecture & Documentation

- **Implementation Plan**: [`docs/IMPLEMENTATION_PLAN.md`](./docs/IMPLEMENTATION_PLAN.md)
- **Domain Vocabulary**: [`GLOSSARY.md`](./GLOSSARY.md)
- **Architecture Decision Records**: [`docs/adr/`](./docs/adr/)
- **Agent Guidelines**: [`AGENTS.md`](./AGENTS.md)
