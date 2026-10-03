# Changelog

All notable changes to CallGlance are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/).

## [0.1.0] - 2026-10-03

First release.

### Added

- Continuous, lightweight checks of latency, jitter and packet loss to your router, your ISP's
  first router (found traceroute-style) and public targets (1.1.1.1 and 8.8.8.8), plus DNS lookup
  times. Probes are sent in voice-like bursts.
- A plain-language verdict: *Good for calls*, *Your Wi-Fi is the problem* or *Your ISP is the
  problem*, judged by where loss and jitter start, with a reason and tips.
- Wi-Fi details from NetworkManager: network, signal in dBm, band and link speed.
- GNOME Shell extension (GNOME 45–51): a coloured dot in the top bar, and a popover with the
  path from you to the internet, live numbers and a one-hour graph you can hover.
- Pinnable desktop widget in light and dark style, kept on top or on the desktop.
- Notifications when quality drops below call grade (thresholds from Zoom, Meet and Teams
  guidance), and when it recovers.
- On-demand speed test with latency under load and a bufferbloat grade.
- Start at login, on by default, with a toggle.
- Tray icon, details window and widget for other desktops (KDE Plasma, XFCE, Cinnamon, MATE,
  Budgie), and on GNOME until the next login after installing.
- No root needed: ICMP when the system allows it, otherwise TCP, UDP and DNS probes, with DNS
  interception detection.
- `callglance status`, `speedtest`, `doctor`, `autostart`, `enable-icmp` and `quit` terminal
  commands, and a demo mode (`callglance --demo`).
- `.deb` and AppImage (x86_64 and aarch64) packages built by GitHub Actions on release.

[0.1.0]: https://github.com/rafay-ah/callglance/releases/tag/v0.1.0
