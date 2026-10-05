#!/usr/bin/env bash
# Removes the OmaCPAP app, launcher, and timer. Keeps your data unless --purge.
set -euo pipefail
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/omacpap"
if [[ "${1:-}" == "--purge" ]]; then
  # sign out while the app is still installed, so it can clear its own keyring entries
  command -v omacpap >/dev/null && omacpap logout >/dev/null || true
fi
systemctl --user disable --now omacpap-sync.timer 2>/dev/null || true
rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/omacpap-sync."{service,timer}
systemctl --user daemon-reload 2>/dev/null || true
pkill -f "python3 -m omacpap serve" 2>/dev/null || true
rm -f "$HOME/.local/bin/omacpap" \
      "${XDG_DATA_HOME:-$HOME/.local/share}/applications/omacpap.desktop" \
      "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps/omacpap.svg"
rm -rf "$DATA/app"
if [[ "${1:-}" == "--purge" ]]; then
  secret-tool clear service omacpap 2>/dev/null || true
  rm -rf "$DATA" "${XDG_STATE_HOME:-$HOME/.local/state}/omacpap" "${XDG_CONFIG_HOME:-$HOME/.config}/omacpap"
  echo "OmaCPAP removed, including your downloaded CPAP data and saved credentials."
else
  echo "OmaCPAP removed. Your data is still in $DATA (run with --purge to delete it)."
fi
