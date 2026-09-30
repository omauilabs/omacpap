#!/usr/bin/env bash
# OmaCPAP installer for Omarchy (Arch + Hyprland). No sudo unless you pass --deps.
#
#   ./install.sh           install for the current user
#   ./install.sh --deps    also `sudo pacman -S --needed python libsecret gnome-keyring`
#   ./install.sh --no-timer  skip the twice-daily background sync
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/omacpap/app"
BIN_DIR="$HOME/.local/bin"
APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
WITH_TIMER=1

for arg in "$@"; do
  case "$arg" in
    --deps) sudo pacman -S --needed --noconfirm python libsecret gnome-keyring ;;
    --no-timer) WITH_TIMER=0 ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\033[1m%s\033[0m\n' "$*"; }

command -v python3 >/dev/null || { echo "python3 is required (sudo pacman -S python)" >&2; exit 1; }
python3 - <<'PY' || { echo "Python 3.11+ is required" >&2; exit 1; }
import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY

say "Installing OmaCPAP to $APP_DIR"
mkdir -p "$APP_DIR" "$BIN_DIR" "$APPS_DIR" "$ICON_DIR"
rm -rf "$APP_DIR/omacpap"
cp -r "$SRC/omacpap" "$APP_DIR/"
find "$APP_DIR" -name '__pycache__' -prune -exec rm -rf {} +
install -Dm 755 "$SRC/bin/omacpap" "$APP_DIR/bin/omacpap"
ln -sfn "$APP_DIR/bin/omacpap" "$BIN_DIR/omacpap"

install -m 644 "$SRC/omacpap/web/icon.svg" "$ICON_DIR/omacpap.svg"
# Absolute path: app launchers don't always have ~/.local/bin on PATH
sed "s|^Exec=omacpap |Exec=$BIN_DIR/omacpap |" "$SRC/share/omacpap.desktop" > "$APPS_DIR/omacpap.desktop"
chmod 644 "$APPS_DIR/omacpap.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database -q "$APPS_DIR" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q "$(dirname "$(dirname "$ICON_DIR")")" 2>/dev/null || true

if [[ $WITH_TIMER == 1 ]] && command -v systemctl >/dev/null; then
  say "Enabling background sync (09:52 and 14:52 daily)"
  mkdir -p "$UNIT_DIR"
  install -m 644 "$SRC/share/omacpap-sync.service" "$SRC/share/omacpap-sync.timer" "$UNIT_DIR/"
  systemctl --user daemon-reload
  systemctl --user enable --now omacpap-sync.timer >/dev/null
fi

if ! command -v secret-tool >/dev/null; then
  echo
  echo "  Note: secret-tool not found. Without it OmaCPAP can't save your myAir password,"
  echo "  so background sync stops working once the myAir session expires."
  echo "  Fix: sudo pacman -S libsecret gnome-keyring   (or rerun with --deps)"
fi

case ":$PATH:" in *":$BIN_DIR:"*) ;; *) echo; echo "  Add $BIN_DIR to your PATH." ;; esac

echo
say "Done. Launch OmaCPAP from the app launcher (Super + Space → OmaCPAP) or run: omacpap"
echo "  First run asks for your myAir email + password and downloads your full history."
