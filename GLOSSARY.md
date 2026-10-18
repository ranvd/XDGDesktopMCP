# XDG Desktop Control

Exposes Linux desktop GUI observation and interaction capabilities to AI agents via standard XDG Desktop Portal interfaces over D-Bus.

## Language

**Portal Session**:
An active, stateful handle negotiated with the desktop portal broker that manages user-granted permissions for desktop resources.
_Avoid_: Connection, handle, token

**Screen Cast**:
The mechanism for capturing screen or window frames via PipeWire streams initiated through the portal.
_Avoid_: Screenshot grabber, display capture, screen record

**Remote Desktop**:
The interface used to simulate user input events such as pointer movements, button clicks, and keystrokes.
_Avoid_: Virtual keyboard, input injection, mouse automation

**Target Display**:
The specific monitor or window boundary selected by the user during the portal authorization handshake.
_Avoid_: Screen, active monitor, display surface

**Coordinate Space**:
The normalized [0.0, 1.0] coordinate system used to map agent actions onto the physical pixel grid of the Target Display.
_Avoid_: Raw pixels, screen coordinates, window bounds

**Keysym Synthesizer**:
The translation layer that converts symbolic key names and Unicode characters into X11 keysym identifiers and modifier sequences.
_Avoid_: Key mapper, layout translator, keycode table
