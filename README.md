# OmaCPAP

Your ResMed CPAP history, pulled out of myAir and kept on your own machine — as a native-feeling Omarchy app.

OmaCPAP signs in to ResMed myAir the same way the myAir website does, downloads **every night myAir has for you**, stores it in a private SQLite file, and shows it in a dashboard that follows your Omarchy theme. It keeps syncing twice a day in the background and can drop last night's numbers into Waybar.

> **Unofficial.** ResMed publishes no API. OmaCPAP uses the same undocumented endpoints the myAir website uses (via the community-maintained Home Assistant integration). If ResMed changes them, syncing stops until OmaCPAP is updated — but everything already downloaded stays on disk and stays viewable.

---

## What you get

| | |
|---|---|
| **Full history backfill** | Walks myAir month by month back to your first night, then stops after three empty months. |
| **Nightly auto-sync** | systemd user timer at 09:52 and 14:52; desktop notification with last night's numbers. |
| **Per-night data** | Time on mask, AHI, 95th-percentile mask leak (L/min), mask on/off count, myAir score and its four sub-scores (usage /70, mask seal /20, events /5, on/off /5). |
| **Dashboard** | Year-at-a-glance usage calendar (with a rolling wave animation you can switch off in Settings; paused on hover and under reduced-motion), trend charts (daily for ≤200 nights, weekly averages beyond), compliance %, streaks, best 30-night run, weekday patterns, plain-language observations, per-night notes. |
| **Printable report** | `Report` button or `omacpap report` — a clean HTML page to print or save as PDF for sleep-clinic visits. |
| **Field discovery** | `omacpap probe` tests ~60 candidate field names against ResMed's schema (introspection is disabled) and starts saving any extras your account returns. |
| **Device picture** | The header shows a theme-colored CPAP illustration whose screen displays last night's hours. Swap in your own photo (transparent PNG works best) under Settings → Device picture. |
| **Theme-aware** | Reads `~/.config/omarchy/current/theme/colors.toml`; switching themes recolors the app live. |
| **Keyboard-first** | `1`–`4` range · `s` sync · `r` report · `,` settings · `j`/`k` step through nights · `Esc` close. |
| **Exports** | CSV / JSON of every night (`omacpap export`), plus the raw myAir record for each night in the database. |

### What myAir does *not* have

Your AirSense 10 uploads **nightly summaries** over its cellular modem — that's all myAir stores. Pressure curves, apnea-type breakdowns (obstructive vs. central vs. hypopnea), and breath-by-breath flow are only written to an **SD card**. You don't have one installed, so OmaCPAP is built around myAir. If you ever want the deep detail going forward, any SDHC card in the machine's slot starts recording it; `omacpap import-sd` (or Settings → SD card) merges those files into the same nights. Nothing in the app depends on it.

---

## Install

```bash
git clone <this repo> ~/Projects/omacpap   # or unpack the tarball
cd ~/Projects/omacpap
./install.sh --deps       # --deps runs: sudo pacman -S --needed python libsecret gnome-keyring
```

Requirements: Python 3.11+ (Omarchy ships it). **No pip packages** — the whole app is Python standard library plus a vendored copy of uPlot for charts.

The installer puts:

| Path | What |
|---|---|
| `~/.local/share/omacpap/app/` | the app |
| `~/.local/bin/omacpap` | CLI / launcher |
| `~/.local/share/applications/omacpap.desktop` | shows up in Walker (`Super + Space` → OmaCPAP) |
| `~/.config/systemd/user/omacpap-sync.{service,timer}` | background sync (skip with `--no-timer`) |

Uninstall with `./uninstall.sh` (keeps your data) or `./uninstall.sh --purge`.

## First run

Open OmaCPAP from Walker, or:

```bash
omacpap            # starts the local server if needed and opens it with omarchy-launch-webapp
```

Enter your myAir email, password, and region (Americas/Australia or Europe). If ResMed emails you a verification code, the app asks for it. The first sync downloads your whole history — a minute or two for several years.

Prefer the terminal?

```bash
omacpap login      # prompts for email/password (+ MFA code), then backfills history
omacpap status
```

---

## Command line

```text
omacpap                    open the dashboard (default)
omacpap login              connect myAir and download history
omacpap sync [--full]      fetch new nights (or re-download everything)
omacpap sync --notify      same, with a desktop notification (what the timer runs)
omacpap probe              discover extra myAir fields for your account
omacpap status             connection + last-30-night summary
omacpap report --days 90   write a printable HTML report
omacpap export --format csv|json -o nights.csv
omacpap waybar             last night as Waybar JSON (local data only)
omacpap serve [--port N]   run the server in the foreground
omacpap import-sd [PATH]   (optional) import a ResMed SD card
omacpap logout             forget credentials, keep data
```

## Omarchy extras

**Hyprland keybinding** — add to `~/.config/hypr/bindings.conf`:

```ini
bindd = SUPER SHIFT, Z, OmaCPAP, exec, omacpap open
```

**Waybar module** — add `"custom/omacpap"` to a modules list in `~/.config/waybar/config.jsonc`, then:

```jsonc
"custom/omacpap": {
  "exec": "omacpap waybar",
  "return-type": "json",
  "interval": 1800,
  "on-click": "omacpap open"
}
```

Style it in `~/.config/waybar/style.css` with `#custom-omacpap.low` / `.good` / `.stale`.

---

## Privacy and security

- **Everything stays local.** Data lives in `~/.local/share/omacpap/omacpap.db` (mode 0600). The only network traffic is to ResMed's own sign-in and data endpoints.
- **Password** goes to ResMed only, and is saved in your desktop keyring via `secret-tool` (gnome-keyring). It is never written to a plain file. Without a keyring, OmaCPAP keeps working until the myAir session expires, then asks you to sign in again.
- **Session tokens** and Okta's "remember this device" cookie (which cuts down on MFA prompts) are also kept in the keyring, or a 0600 file if no keyring is available.
- **The dashboard server** listens on `127.0.0.1:8742` only, rejects requests with a non-loopback `Host` header (DNS-rebinding guard), requires a custom header on every state-changing request (CSRF guard), and sends a strict Content-Security-Policy.
- The report and dashboard describe what the machine recorded. They are not medical advice; talk to your clinician about therapy changes.

## How the myAir sync works

1. `GET` Okta authorize endpoint → receives the device (`DT`) cookie.
2. `POST /api/v1/authn` with email + password → `SUCCESS` or `MFA_REQUIRED` (email code, then `/factors/{id}/verify`).
3. OAuth authorization-code exchange with PKCE → access token + ID token (the ID token carries your myAir country, which ResMed's API requires as a header).
4. `POST` ResMed's AppSync GraphQL endpoint: `getPatientWrapper { fgDevices, masks, sleepRecords(startMonth, endMonth) { items { … } } }`, one calendar month at a time.
5. Each night is upserted by date; the last 10 nights are re-checked every sync because myAir occasionally revises them.

Endpoints, client IDs, and the flow come from [prestomation/resmed_myair_sensors](https://github.com/prestomation/resmed_myair_sensors) (MIT), maintained by @prestomation and @Snuffy2. If syncing breaks, check that repo first — fixes usually land there.

**Troubleshooting**

| Message | Fix |
|---|---|
| "account setup is incomplete" | Sign in at myair.resmed.com and accept any new terms (ResMed sometimes blocks API access until you do). |
| "rejected the email/password" | Confirm them on the myAir website; then `omacpap login`. |
| Background sync notification says it failed | Usually an expired session with no keyring password: `omacpap login`. |
| Nothing for last night yet | The machine uploads after mask-off; myAir can take a few hours. |

---

## Development

```bash
python3 -m unittest discover -s tests -v     # 17 tests, all against a local mock of ResMed
python3 -m tests.mock_myair 9999 &           # fake myAir with ~3.5 years of synthetic nights
OMACPAP_MYAIR_MOCK=http://127.0.0.1:9999 OMACPAP_DATA_DIR=/tmp/oc omacpap login   # password: demo (or "mfa", code 123456)
```

```text
omacpap/
  myair.py        Okta + PKCE + AppSync client (urllib only)
  sync.py         client ↔ keyring ↔ database
  db.py           SQLite schema and upserts
  analysis.py     compliance, weighted AHI, streaks, insights
  report.py       printable HTML report
  server.py       local HTTP server + JSON API
  theme.py        Omarchy colors.toml → CSS variables
  secrets_store.py  secret-tool keyring + config
  edf.py, sdcard.py optional SD-card (EDF) importer
  web/            dashboard (vanilla JS + uPlot 1.6.32, MIT)
tests/            mock myAir server, synthetic EDF writer, test suite
share/            .desktop entry, systemd units
```

**JSON API** (for OmaHealth or anything else local): `GET /api/nights`, `GET /api/summary`, `GET /api/state`, `GET /api/export.csv`.

## License

MIT. The myAir auth flow is adapted from `resmed_myair_sensors` (MIT, © prestomation). uPlot © Leon Sorokin (MIT). "ResMed", "myAir" and "AirSense" are trademarks of ResMed; this project is not affiliated with or endorsed by ResMed.
