"""XDG-compliant locations. Everything OmaCPAP writes stays under $HOME, 0700/0600."""

from __future__ import annotations

import os
from pathlib import Path

from . import APP_ID


def _xdg(var: str, fallback: str) -> Path:
    base = os.environ.get(var) or str(Path.home() / fallback)
    return Path(base) / APP_ID


def data_dir() -> Path:
    """~/.local/share/omacpap — database and SD-card archive."""
    return _private(Path(os.environ.get("OMACPAP_DATA_DIR") or _xdg("XDG_DATA_HOME", ".local/share")))


def state_dir() -> Path:
    """~/.local/state/omacpap — logs and cached session tokens."""
    return _private(Path(os.environ.get("OMACPAP_STATE_DIR") or _xdg("XDG_STATE_HOME", ".local/state")))


def config_dir() -> Path:
    """~/.config/omacpap — non-secret settings (region, username, port)."""
    return _private(Path(os.environ.get("OMACPAP_CONFIG_DIR") or _xdg("XDG_CONFIG_HOME", ".config")))


def db_path() -> Path:
    return data_dir() / "omacpap.db"


def sd_archive_dir() -> Path:
    """Mirror of every SD card import. ResMed cards overwrite old detail data,
    so OmaCPAP keeps its own copy (same idea as OSCAR's backup folder)."""
    return _private(data_dir() / "sdcard-archive")


def omarchy_theme_dir() -> Path:
    return Path.home() / ".config" / "omarchy" / "current" / "theme"


def _private(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p, 0o700)
    except OSError:
        pass
    return p
