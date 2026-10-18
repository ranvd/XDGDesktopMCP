# Agent Guidelines for XDG Desktop MCP

## Project Overview
**XDG Desktop MCP** is an MCP server enabling AI agents to observe and control the Linux desktop GUI (Wayland & X11) through standard XDG Desktop Portals (`org.freedesktop.portal.RemoteDesktop` and `org.freedesktop.portal.ScreenCast`) over D-Bus.

## Status: Design Complete $\to$ Ready for Implementation
The specification, architecture, and interface design were fully aligned and settled through rigorous grilling and domain modeling. **Do not re-open settled architecture or re-interview the user.**

When starting a session:
1. Read [`docs/IMPLEMENTATION_PLAN.md`](./docs/IMPLEMENTATION_PLAN.md) — This is the single source of truth for module architecture, interfaces, and the 6-phase implementation roadmap.
2. Read [`GLOSSARY.md`](./GLOSSARY.md) — Follow canonical project terminology.
3. Review the ADRs in [`docs/adr/`](./docs/adr/):
   - `0001-persistent-portal-session-with-pipewire.md`
   - `0002-dual-text-input-strategy.md`
   - `0003-embedded-cursor-and-session-lifecycle-guardrails.md`

## Next Action
Proceed directly to **Phase 1: Environment & Tooling Setup** in [`docs/IMPLEMENTATION_PLAN.md`](./docs/IMPLEMENTATION_PLAN.md).

## Critical Environment Facts (Pre-Verified)
- **OS / Desktop**: Linux x86_64, Ubuntu 24.04 with GNOME on Wayland (`XDG_SESSION_TYPE=wayland`).
- **Portals**: `org.freedesktop.portal.Desktop` is active.
- **Multimedia**: PipeWire is running; GStreamer 1.28.2 with `pipewiresrc` plugin is verified.
- **Python Runtime**: Python 3.14. PyGObject (`gi.repository.Gio` and `gi.repository.Gst`) is installed in system site-packages.
- **Virtual Environment**: When creating `.venv`, use `--system-site-packages` so Python can access system `gi` (PyGObject). Passwordless `sudo` is available if system packages (e.g. `python3-venv` / `python3.14-venv`) need installation.
