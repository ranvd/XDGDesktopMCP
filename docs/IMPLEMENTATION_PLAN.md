# XDG Desktop MCP: Implementation Plan & Architecture

This document preserves the agreed architecture, module seams, and step-by-step execution plan for the **XDG Desktop MCP** server. Any future session or agent can pick up directly from this roadmap.

---

## 1. Domain Context & Architectural Decisions

* **Domain Model**: See [`GLOSSARY.md`](../GLOSSARY.md) for canonical terms (`Portal Session`, `Screen Cast`, `Remote Desktop`, `Target Display`, `Coordinate Space`, `Keysym Synthesizer`).
* **ADR 0001**: [`0001-persistent-portal-session-with-pipewire.md`](./adr/0001-persistent-portal-session-with-pipewire.md) – Persistent combined session via D-Bus; GStreamer `pipewiresrc` consumer with `max-buffers=1, drop=true`.
* **ADR 0002**: [`0002-dual-text-input-strategy.md`](./adr/0002-dual-text-input-strategy.md) – Dual text strategy (keysym synthesis for shortcuts/navigation + clipboard pasting for bulk text).
* **ADR 0003**: [`0003-embedded-cursor-and-session-lifecycle-guardrails.md`](./adr/0003-embedded-cursor-and-session-lifecycle-guardrails.md) – Embedded cursor in stream; `Session.Closed` signal handling; modifier auto-cleanup.

---

## 2. Target Project Structure

```
XDGDesktopMCP/
├── GLOSSARY.md
├── pyproject.toml                     # Modern package metadata, dependencies & CLI script
├── README.md                          # User documentation & MCP client configuration
├── docs/
│   ├── IMPLEMENTATION_PLAN.md         # This document
│   └── adr/
│       ├── 0001-persistent-portal-session-with-pipewire.md
│       ├── 0002-dual-text-input-strategy.md
│       └── 0003-embedded-cursor-and-session-lifecycle-guardrails.md
├── src/
│   └── xdg_desktop_mcp/
│       ├── __init__.py                # Package exports & version
│       ├── __main__.py                # CLI entry point runner
│       ├── portal.py                  # D-Bus Gio client: RemoteDesktop + ScreenCast portal lifecycle
│       ├── stream.py                  # GStreamer pipeline: PipeWire consumer & JPEG/PNG frame grabber
│       ├── input.py                   # Normalized coordinates, mouse actions & KeysymSynthesizer
│       └── server.py                  # FastMCP server exposing high-level tools
└── tests/
    ├── __init__.py
    ├── test_input.py                  # Unit tests for coordinate translation & keysym mapping
    ├── test_portal.py                 # Unit tests for D-Bus request structures & tokens
    └── test_stream.py                 # Pipeline construction & frame extraction logic tests
```

---

## 3. Module Responsibilities & Seams

### `portal.py` (D-Bus Portal Layer)
* Wraps `org.freedesktop.portal.Desktop` using `Gio.DBusConnection` (system PyGObject).
* Manages session creation (`CreateSession`), requesting both `ScreenCast` and `RemoteDesktop` devices in a unified session.
* Starts session on-demand (lazy initialization), yielding the PipeWire remote file descriptor and stream node ID.
* Subscribes to `org.freedesktop.portal.Session.Closed` to handle user-revocation gracefully and clean up state.

### `stream.py` (PipeWire Video Ingestion)
* Constructs and manages the GStreamer pipeline:
  `pipewiresrc path=<node_id> fd=<fd> ! videoconvert ! videoscale ! appsink`
* Configures `appsink` with `drop=True, max-buffers=1` to guarantee zero memory accumulation and instant latest-frame retrieval.
* Encodes frames to JPEG (with downscaling to ~1080p max dimension by default for LLM token efficiency) or uncompressed PNG on demand.
* Reports display metadata (`width`, `height`, `aspect_ratio`).

### `input.py` (Input & Keysym Synthesizer)
* **Coordinate Mapping**: Converts normalized `[0.0, 1.0]` coordinates to portal absolute motion events (`NotifyPointerMotionAbsolute`).
* **Mouse Operations**: `click`, `double_click`, `move_cursor`, `drag_and_drop`, `scroll`.
* **Keyboard & Shortcut Synthesizer**: Translates strings (e.g. `"Ctrl+Shift+T"`, `"Return"`, `"BackSpace"`, characters) into X11 keysyms and dispatches `NotifyKeyboardKeysym` events.
* **Clipboard Acceleration**: Facilitates bulk text and code pasting using system clipboard (`Gdk` / `xclip`) followed by synthetic `Ctrl+V`.
* **Safety Guard**: Ensures all pressed modifiers (`Shift`, `Ctrl`, `Alt`) are automatically released in a `finally` block; blocks dangerous VT-switching combinations (`Ctrl+Alt+F1` through `F12`).

### `server.py` (FastMCP Server)
* Exposes clean, agent-friendly tools:
  - `start_session(sources, devices)`: Explicit connection (also auto-triggered on first tool call).
  - `take_screenshot(format="jpeg", max_dimension=1920)`: Captures screen with display metadata.
  - `click(x, y, button="left")`: Clicks at normalized coordinates.
  - `double_click(x, y)`: Double-clicks at normalized coordinates.
  - `move_cursor(x, y)`: Moves mouse pointer.
  - `drag_and_drop(start_x, start_y, end_x, end_y)`: Drags from start to end coordinates.
  - `scroll(direction="down", amount=3)`: Scrolls vertical or horizontal axis.
  - `press_key(key)` / `press_hotkey(keys)`: Dispatches keypress or key combinations.
  - `type_text(text, method="auto")`: Types short text directly or pastes bulk text via clipboard.
  - `get_display_info()`: Returns current active resolution and stream metadata.
  - `close_session()`: Manually closes portal session and releases pipelines.

---

## 4. Phased Implementation Roadmap

- [x] **Phase 1: Environment & Tooling Setup**
  - Install `python3-venv` / configure virtual environment with system site-packages (for `gi` GStreamer and Gio access).
  - Install `mcp` SDK and test framework (`pytest`).
- [x] **Phase 2: D-Bus Portal Wrapper (`portal.py`)**
  - Implement async D-Bus session creation, source selection, and start request.
  - Subscribe to portal response signals and `Session.Closed`.
- [x] **Phase 3: GStreamer Stream Consumer (`stream.py`)**
  - Construct `pipewiresrc` pipeline from PipeWire file descriptor and stream node ID.
  - Implement buffer extraction from `appsink` and JPEG/PNG encoding with metadata.
- [x] **Phase 4: Input Translation & Keysym Synthesizer (`input.py`)**
  - Implement coordinate normalization and mouse pointer/button dispatching.
  - Implement X11 keysym lookup table and modifier state tracker with auto-release cleanup.
  - Implement clipboard insertion fallback.
- [x] **Phase 5: MCP Server Wiring (`server.py` & `__main__.py`)**
  - Define FastMCP tools and integrate with portal, stream, and input modules.
  - Implement lazy connection trigger and clear error recovery messages.
- [x] **Phase 6: Packaging, Testing & Verification**
  - Create `pyproject.toml` with console script `xdg-desktop-mcp`.
  - Write and run unit tests for input mappings and D-Bus payloads.
  - Validate live execution and documentation.
