# Dual Text Input Strategy

We use a dual strategy for text entry: single-key and shortcut navigation are simulated via X11 keysym events over `org.freedesktop.portal.RemoteDesktop`, while long text and code blocks use clipboard injection (`wl-copy` or Wayland clipboard) followed by a synthetic `Ctrl+V` keystroke. Simulating keypresses character-by-character for large strings over D-Bus introduces latency, dropped keystrokes, and keyboard layout mismatches; clipboard pasting delivers instant and lossless text insertion while preserving granular keyboard control for shortcuts.
