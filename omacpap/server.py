"""Local web server for the dashboard. Binds to 127.0.0.1 only and rejects
requests whose Host header isn't the loopback address (DNS-rebinding guard).
State-changing calls must be JSON POSTs carrying an `X-OmaCPAP` header, which
a cross-site form or image tag cannot send."""

from __future__ import annotations

import base64
import binascii
import csv
import io
import json
import os
import logging
import mimetypes
import threading
import traceback
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import __version__, analysis, db, paths, report, secrets_store, theme
from .myair import MFARequired, MyAirClient, MyAirError, SessionState, get_region
from .sync import make_client, persist_session, run_sync

log = logging.getLogger("omacpap.server")
WEB = Path(__file__).parent / "web"


# ------------------------------------------------------------------ background jobs
class Job:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.kind: str | None = None
        self.progress: list[str] = []
        self.result: dict[str, Any] | None = None
        self.error: str | None = None

    def start(self, kind: str, fn) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running, self.kind, self.progress, self.result, self.error = True, kind, [], None, None

        def run() -> None:
            try:
                self.result = fn(self.progress.append)
            except MyAirError as e:
                self.error = str(e)
            except Exception as e:  # noqa: BLE001
                log.error("job %s failed\n%s", kind, traceback.format_exc())
                self.error = f"{type(e).__name__}: {e}"
            finally:
                self.running = False

        threading.Thread(target=run, daemon=True, name=f"omacpap-{kind}").start()
        return True

    def as_dict(self) -> dict[str, Any]:
        return {"running": self.running, "kind": self.kind, "progress": self.progress[-8:],
                "result": self.result, "error": self.error}


JOB = Job()
_pending_login: dict[str, Any] = {}  # holds a MyAirClient mid-MFA


def _mask(email: str | None) -> str | None:
    if not email or "@" not in email:
        return email
    name, domain = email.split("@", 1)
    return f"{name[:2]}{'•' * max(len(name) - 2, 1)}@{domain}"


# ------------------------------------------------------------------ API handlers
def api_state() -> dict[str, Any]:
    cfg = secrets_store.load_config()
    with db.session() as con:
        dev = con.execute("SELECT * FROM devices ORDER BY updated_at DESC LIMIT 1").fetchone()
        count = con.execute("SELECT COUNT(*) AS n, MIN(date) AS a, MAX(date) AS b FROM nights").fetchone()
        sd = con.execute("SELECT COUNT(*) AS n FROM sd_days").fetchone()["n"]
        return {
            "version": __version__,
            "configured": bool(cfg.get("username")),
            "username": _mask(cfg.get("username")),
            "region": cfg.get("region"),
            "keyring": secrets_store.keyring_available(),
            "has_password": bool(cfg.get("username") and secrets_store.has_password(cfg["username"])),
            "last_sync": db.meta_get(con, "last_sync"),
            "first_name": db.meta_get(con, "patient_first_name"),
            "device": dict(dev) if dev else None,
            "nights": {"count": count["n"], "first": count["a"], "last": count["b"]},
            "sd_days": sd,
            "extra_fields": cfg.get("extra_fields") or [],
            "settings": {k: cfg[k] for k in ("usage_goal_hours", "ahi_reference", "leak_reference")},
            "mfa_pending": bool(_pending_login),
            "device_image": _device_image_version(),
            "job": JOB.as_dict(),
            "syncs": db.last_syncs(con, 5),
            "theme": theme.signature(),
        }


def api_nights(q: dict[str, str]) -> dict[str, Any]:
    with db.session() as con:
        rows = db.nights(con, q.get("from"), q.get("to"))
    return {"nights": analysis.calendar_fill(rows) if rows else []}


def api_summary() -> dict[str, Any]:
    cfg = secrets_store.load_config()
    with db.session() as con:
        rows = db.nights(con)
    return analysis.build_summary(rows, float(cfg["usage_goal_hours"]), float(cfg["leak_reference"]))


def api_login(body: dict[str, Any]) -> dict[str, Any]:
    user = (body.get("username") or "").strip()
    pw = body.get("password") or ""
    region = (body.get("region") or "NA").upper()
    if not user or not pw:
        raise MyAirError("Enter your myAir email and password.")
    cfg = secrets_store.load_config()
    prior = SessionState.from_json(secrets_store.get_session(user))
    client = MyAirClient(username=user, password=pw, region=get_region(region),
                         state=SessionState(device_token=prior.device_token))
    _pending_login.clear()
    try:
        client.login()
    except MFARequired as e:
        _pending_login.update(client=client, remember=bool(body.get("remember", True)), region=region)
        return {"status": "MFA_REQUIRED", "message": str(e)}
    return _finish_login(client, region, bool(body.get("remember", True)), cfg)


def api_mfa(body: dict[str, Any]) -> dict[str, Any]:
    if not _pending_login:
        raise MyAirError("No sign-in is waiting for a code. Start again.")
    client: MyAirClient = _pending_login["client"]
    client.verify_mfa(str(body.get("code", "")))
    result = _finish_login(client, _pending_login["region"], _pending_login["remember"], secrets_store.load_config())
    _pending_login.clear()
    return result


def _finish_login(client: MyAirClient, region: str, remember: bool, cfg: dict[str, Any]) -> dict[str, Any]:
    cfg.update(username=client.username, region=region)
    secrets_store.save_config(cfg)
    persist_session(client)
    stored = remember and client.password and secrets_store.save_password(client.username, client.password)
    # kick off the first sync right away with the live client (works even without a keyring)
    started = JOB.start("sync", lambda p: run_sync(client=client, progress=p))
    if not stored:
        log.info("password not stored in keyring; nightly background sync will need `omacpap login`")
    if not started:
        log.warning("signed in, but %s is still running; first sync not started", JOB.kind)
        return {"status": "SUCCESS", "sync_started": False,
                "notice": "Signed in. Another task is still running — press Sync when it finishes."}
    return {"status": "SUCCESS", "sync_started": True}


def api_sync(body: dict[str, Any]) -> dict[str, Any]:
    full = bool(body.get("full"))
    client = make_client()
    if not client.password and not client.token_valid:
        raise MyAirError("Your myAir session expired and no saved password was found. Connect again.")
    started = JOB.start("sync-full" if full else "sync", lambda p: run_sync(full=full, client=client, progress=p))
    return {"started": started}


def api_probe(_: dict[str, Any]) -> dict[str, Any]:
    client = make_client()

    def work(p):
        found = client.probe_fields(progress=p)
        cfg = secrets_store.load_config()
        cfg["extra_fields"] = found
        secrets_store.save_config(cfg)
        persist_session(client)
        if found:
            p(f"Found {len(found)} extra field(s): {', '.join(found)}. Re-syncing full history…")
            return {"found": found, **run_sync(full=True, client=client, progress=p)}
        return {"found": []}

    return {"started": JOB.start("probe", work)}


def api_note(body: dict[str, Any]) -> dict[str, Any]:
    date = str(body.get("date", ""))[:10]
    if len(date) != 10:
        raise MyAirError("A note needs a date.")
    with db.session() as con:
        db.set_note(con, date, str(body.get("text", "")), body.get("tags"))
    return {"ok": True}


def api_settings(body: dict[str, Any]) -> dict[str, Any]:
    cfg = secrets_store.load_config()
    for k, lo, hi in (("usage_goal_hours", 1, 12), ("ahi_reference", 1, 30), ("leak_reference", 5, 60)):
        if k in body:
            cfg[k] = max(lo, min(hi, float(body[k])))
    secrets_store.save_config(cfg)
    return {"ok": True}


def api_logout(_: dict[str, Any]) -> dict[str, Any]:
    cfg = secrets_store.load_config()
    if cfg.get("username"):
        secrets_store.forget(cfg["username"])
    cfg["username"] = None
    secrets_store.save_config(cfg)
    _pending_login.clear()
    return {"ok": True}


def api_import_sd(body: dict[str, Any]) -> dict[str, Any]:
    from . import sdcard
    path = body.get("path")
    cards = [Path(path)] if path else sdcard.find_cards()
    if not cards:
        raise MyAirError("No ResMed SD card found. Insert and mount it, or type its path.")
    return {"started": JOB.start("sd-import", lambda p: sdcard.import_card(cards[0]))}


DEVICE_IMAGE_TYPES = {b"\x89PNG\r\n\x1a\n": ("png", "image/png"), b"\xff\xd8\xff": ("jpg", "image/jpeg"),
                      b"RIFF": ("webp", "image/webp")}
DEVICE_IMAGE_MAX = 6 * 1024 * 1024


def _device_image() -> tuple[Path, str] | None:
    for ext, ctype in (("png", "image/png"), ("webp", "image/webp"), ("jpg", "image/jpeg")):
        p = paths.data_dir() / f"device-image.{ext}"
        if p.exists():
            return p, ctype
    return None


def _device_image_version() -> str | None:
    found = _device_image()
    return str(found[0].stat().st_mtime_ns) if found else None


def _remove_device_images() -> None:
    for old in paths.data_dir().glob("device-image.*"):
        old.unlink()


def api_device_image(body: dict[str, Any]) -> dict[str, Any]:
    """Save (or clear) the user's own picture of their machine, shown in the dashboard header."""
    if body.get("clear"):
        _remove_device_images()
        return {"ok": True, "device_image": None}
    data = str(body.get("data", ""))
    if "," in data:
        data = data.split(",", 1)[1]
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as e:
        raise MyAirError("That file couldn't be read as an image.") from e
    if len(raw) > DEVICE_IMAGE_MAX:
        raise MyAirError("Image is larger than 6 MB — try a smaller PNG.")
    kind = next((v for magic, v in DEVICE_IMAGE_TYPES.items() if raw.startswith(magic)), None)
    if not kind or (kind[0] == "webp" and raw[8:12] != b"WEBP"):
        raise MyAirError("Use a PNG, WebP or JPEG image.")
    # only replace the current picture once the new one has passed validation
    _remove_device_images()
    dest = paths.data_dir() / f"device-image.{kind[0]}"
    dest.write_bytes(raw)
    os.chmod(dest, 0o600)
    return {"ok": True, "device_image": _device_image_version()}


def export_csv() -> str:
    with db.session() as con:
        rows = db.nights(con)
    buf = io.StringIO()
    cols = ["date", "usage_min", "ahi", "leak_lpm", "mask_pairs", "sleep_score", "usage_score",
            "ahi_score", "mask_score", "leak_score", "oai", "cai", "hi", "press50", "press95", "note"]
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow([r.get(c) if r.get(c) is not None else "" for c in cols])
    return buf.getvalue()


POST_ROUTES = {
    "/api/login": api_login, "/api/mfa": api_mfa, "/api/sync": api_sync, "/api/probe": api_probe,
    "/api/note": api_note, "/api/settings": api_settings, "/api/logout": api_logout,
    "/api/import-sd": api_import_sd, "/api/device-image": api_device_image,
}
BODY_LIMITS = {"/api/device-image": DEVICE_IMAGE_MAX * 4 // 3 + 4096}


# ------------------------------------------------------------------ HTTP plumbing
class Handler(BaseHTTPRequestHandler):
    server_version = f"OmaCPAP/{__version__}"
    port = 8742

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        return host in {f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}"}

    def _send(self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                         "connect-src 'self'; frame-ancestors 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj: Any, status: int = 200) -> None:
        self._send(status, json.dumps(obj, default=str).encode(), "application/json")

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            return self._send(HTTPStatus.FORBIDDEN, b"forbidden host", "text/plain")
        url = urllib.parse.urlparse(self.path)
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
        try:
            if url.path in ("/", "/index.html"):
                return self._file(WEB / "index.html")
            if url.path.startswith("/static/"):
                return self._file(WEB / url.path.removeprefix("/static/"))
            if url.path == "/device-image":
                found = _device_image()
                if not found:
                    return self._send(404, b"no image", "text/plain")
                return self._send(200, found[0].read_bytes(), found[1])
            if url.path == "/theme.css":
                return self._send(200, theme.css().encode(), "text/css")
            if url.path == "/api/state":
                return self._json(api_state())
            if url.path == "/api/nights":
                return self._json(api_nights(q))
            if url.path == "/api/summary":
                return self._json(api_summary())
            if url.path == "/api/export.csv":
                return self._send(200, export_csv().encode(), "text/csv",
                                  {"Content-Disposition": 'attachment; filename="omacpap-nights.csv"'})
            if url.path == "/report":
                try:
                    days = max(1, min(int(q.get("days", 90)), 36500))
                except ValueError:
                    return self._send(400, b"days must be a whole number", "text/plain")
                return self._send(200, report.render(days=days).encode(), "text/html; charset=utf-8")
            self._send(404, b"not found", "text/plain")
        except Exception as e:  # noqa: BLE001
            log.error("GET %s failed\n%s", url.path, traceback.format_exc())
            self._json({"error": str(e)}, 500)

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok() or self.headers.get("X-OmaCPAP") != "1":
            return self._send(HTTPStatus.FORBIDDEN, b"forbidden", "text/plain")
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._send(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, b"json only", "text/plain")
        path = urllib.parse.urlparse(self.path).path
        route = POST_ROUTES.get(path)
        if not route:
            return self._send(404, b"not found", "text/plain")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > BODY_LIMITS.get(path, 64_000):
                return self._json({"error": "Request too large."}, 413)
            body = json.loads(self.rfile.read(length) or b"{}")
            self._json(route(body))
        except MyAirError as e:
            self._json({"error": str(e)}, 400)
        except (ValueError, json.JSONDecodeError) as e:
            self._json({"error": f"Bad request: {e}"}, 400)
        except Exception as e:  # noqa: BLE001
            log.error("POST failed\n%s", traceback.format_exc())
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _file(self, path: Path) -> None:
        path = path.resolve()
        if WEB.resolve() not in path.parents and path != (WEB / "index.html").resolve() or not path.is_file():
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self._send(200, path.read_bytes(), ctype)


def serve(port: int = 8742) -> ThreadingHTTPServer:
    Handler.port = port
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    return httpd
