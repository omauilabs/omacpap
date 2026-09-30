"""Credential storage.

Preferred: the desktop keyring via `secret-tool` (libsecret → gnome-keyring,
which Omarchy runs for Chromium). Fallback: a 0600 JSON file in the state dir,
used only for the session tokens — the password itself is never written to a
plain file; without a keyring it must come from the environment or a prompt.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import APP_ID, paths

ATTRS = ["service", APP_ID]


def keyring_available() -> bool:
    if not shutil.which("secret-tool"):
        return False
    try:
        r = subprocess.run(["secret-tool", "search", *ATTRS, "kind", "probe"],
                           capture_output=True, timeout=5)
        return r.returncode in (0, 1)
    except (OSError, subprocess.TimeoutExpired):
        return False


def _store(kind: str, account: str, secret: str, label: str) -> bool:
    if not keyring_available():
        return False
    r = subprocess.run(["secret-tool", "store", f"--label={label}", *ATTRS, "kind", kind, "account", account],
                       input=secret.encode(), capture_output=True, timeout=10)
    return r.returncode == 0


def _lookup(kind: str, account: str) -> str | None:
    if not keyring_available():
        return None
    r = subprocess.run(["secret-tool", "lookup", *ATTRS, "kind", kind, "account", account],
                       capture_output=True, timeout=10)
    return r.stdout.decode() if r.returncode == 0 and r.stdout else None


def _clear(kind: str, account: str) -> None:
    if keyring_available():
        subprocess.run(["secret-tool", "clear", *ATTRS, "kind", kind, "account", account],
                       capture_output=True, timeout=10)


# ------------------------------------------------------------------ password
def save_password(account: str, password: str) -> bool:
    return _store("password", account, password, "OmaCPAP — ResMed myAir password")


def get_password(account: str) -> str | None:
    return os.environ.get("OMACPAP_MYAIR_PASSWORD") or _lookup("password", account)


# ------------------------------------------------------------------ session tokens
def _session_file() -> Path:
    return paths.state_dir() / "myair-session.json"


def save_session(account: str, blob: str) -> None:
    if _store("session", account, blob, "OmaCPAP — myAir session"):
        _session_file().unlink(missing_ok=True)
        return
    f = _session_file()
    f.touch(mode=0o600, exist_ok=True)
    os.chmod(f, 0o600)
    f.write_text(blob)


def get_session(account: str) -> str | None:
    s = _lookup("session", account)
    if s:
        return s
    f = _session_file()
    return f.read_text() if f.exists() else None


def forget(account: str) -> None:
    _clear("password", account)
    _clear("session", account)
    _session_file().unlink(missing_ok=True)


# ------------------------------------------------------------------ config (non-secret)
DEFAULT_CONFIG: dict[str, Any] = {
    "username": None,
    "region": "NA",
    "port": 8742,
    "extra_fields": [],       # SleepRecord fields discovered by `omacpap probe`
    "usage_goal_hours": 4.0,  # compliance threshold used in charts and reports
    "ahi_reference": 5.0,     # reference line on the AHI chart
    "leak_reference": 24.0,   # ResMed's large-leak line, L/min
}


def config_path() -> Path:
    return paths.config_dir() / "config.json"


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    p = config_path()
    if p.exists():
        try:
            cfg.update(json.loads(p.read_text()))
        except json.JSONDecodeError:
            pass
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    p = config_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(p)
