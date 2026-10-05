# Changelog

## 0.1.0 — 2026-10-05

First release.

- Sign in to ResMed myAir (including email verification codes) and download your full nightly history into a private SQLite database.
- Background sync twice a day via a systemd user timer, with a desktop notification for last night's numbers.
- Theme-aware dashboard: year-at-a-glance usage calendar with an animated greeting, trend charts, compliance, streaks, weekday patterns, observations and per-night notes.
- Printable HTML report, CSV/JSON export, Waybar module, field discovery (`omacpap probe`) and optional SD-card import.
- Device illustration in the header, with an option to use your own photo.
- Credentials kept in the desktop keyring; the local server only accepts requests from this machine.

### Fixes before release

- The dashboard checks the keyring far less often, so a locked keyring no longer triggers repeated unlock prompts.
- A rejected device-photo upload no longer deletes the picture you already had.
- If ResMed asks for a verification code but doesn't say how to send it, you now get a clear message instead of a failed request.
- A report link with an invalid number of days shows a clear error instead of a server error.
- If another task is running when you sign in, the app now tells you to press Sync instead of skipping the first sync without a word.
- `uninstall.sh --purge` now signs you out of myAir before removing the app.
