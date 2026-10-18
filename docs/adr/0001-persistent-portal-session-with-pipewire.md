# Persistent Combined Portal Session with PipeWire Stream Consumer

We use a long-lived combined `org.freedesktop.portal.RemoteDesktop` and `ScreenCast` portal session over D-Bus, extracting frames on-demand via a GStreamer PipeWire consumer (`pipewiresrc`) with a single-frame buffer (`max-buffers=1, drop=true`). This avoids triggering Wayland authorization dialogs on every tool invocation while ensuring frame capture does not continuously accumulate memory or saturate CPU/GPU resources when idle.
