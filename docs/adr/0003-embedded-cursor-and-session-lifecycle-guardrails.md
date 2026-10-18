# Embedded Cursor and Session Lifecycle Guardrails

We configure the ScreenCast portal stream with embedded cursor mode (`cursor_mode=Embedded`) so the agent has visual verification of pointer coordinates in every captured frame. The server actively monitors the D-Bus `Session.Closed` signal to gracefully tear down GStreamer pipelines upon user revocation. All synthetic keyboard inputs enforce automatic modifier release in a `finally` block and reject virtual-terminal switching hotkeys to prevent desktop session lockouts.
