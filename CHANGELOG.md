# Changelog

## 0.1.0 — 2026-10-05

First release.

- Sign in to ResMed myAir (including email verification codes) and download your full nightly history into a private SQLite database.
- Background sync twice a day via a systemd user timer, with a desktop notification for last night's numbers.
- Theme-aware dashboard: year-at-a-glance usage calendar with an animated greeting, trend charts, compliance, streaks, weekday patterns, observations and per-night notes.
- Printable HTML report, CSV/JSON export, Waybar module, field discovery (`omacpap probe`) and optional SD-card import.
- Device illustration in the header, with an option to use your own photo.
- Credentials kept in the desktop keyring; the local server only accepts requests from this machine.
