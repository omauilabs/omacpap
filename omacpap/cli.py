"""omacpap command line."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

from . import APP_NAME, __version__, analysis, db, paths, secrets_store
from .myair import MFARequired, MyAirClient, MyAirError, SessionState, get_region


def _notify(summary: str, body: str = "", urgent: bool = False) -> None:
    if shutil.which("notify-send"):
        args = ["notify-send", "-a", APP_NAME, "-i", "omacpap"]
        if urgent:
            args += ["-u", "critical"]
        subprocess.run(args + [summary, body], check=False)


def _port(args) -> int:
    return int(getattr(args, "port", None) or secrets_store.load_config().get("port") or 8742)


def _server_up(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=1.5) as r:
            return r.status == 200
    except OSError:
        return False


# ------------------------------------------------------------------ commands
def cmd_login(args) -> int:
    cfg = secrets_store.load_config()
    user = args.username or input(f"myAir email [{cfg.get('username') or ''}]: ").strip() or cfg.get("username")
    if not user:
        print("An email is required.")
        return 2
    region = (args.region or cfg.get("region") or "NA").upper()
    pw = os.environ.get("OMACPAP_MYAIR_PASSWORD") or getpass.getpass("myAir password: ")
    prior = SessionState.from_json(secrets_store.get_session(user))
    client = MyAirClient(username=user, password=pw, region=get_region(region),
                         state=SessionState(device_token=prior.device_token))
    try:
        client.login()
    except MFARequired as e:
        print(e)
        client.verify_mfa(input("Verification code: "))
    cfg.update(username=user, region=region)
    secrets_store.save_config(cfg)
    secrets_store.save_session(user, client.state.to_json())
    if secrets_store.save_password(user, pw):
        print("Signed in. Password saved to your keyring for automatic nightly syncs.")
    else:
        print("Signed in. No keyring found (install `libsecret` + gnome-keyring) — nightly sync will "
              "work until the session expires, then you'll need to log in again.")
    if not args.no_sync:
        from .sync import run_sync
        print("Downloading your full myAir history (month by month)…")
        res = run_sync(client=client, progress=lambda m: print("  " + m))
        print(res["message"])
    return 0


def cmd_logout(_args) -> int:
    cfg = secrets_store.load_config()
    if cfg.get("username"):
        secrets_store.forget(cfg["username"])
        cfg["username"] = None
        secrets_store.save_config(cfg)
    print("Signed out of myAir and removed saved credentials. Your local data is untouched.")
    return 0


def cmd_sync(args) -> int:
    from .sync import run_sync
    try:
        res = run_sync(full=args.full, progress=None if args.quiet else (lambda m: print("  " + m)))
    except MyAirError as e:
        print(f"Sync failed: {e}", file=sys.stderr)
        if args.notify:
            _notify("OmaCPAP couldn't sync myAir", str(e), urgent=True)
        return 1
    if not args.quiet:
        print(res["message"])
    if args.notify and res["new"]:
        with db.session() as con:
            last = con.execute("SELECT * FROM nights ORDER BY date DESC LIMIT 1").fetchone()
        if last:
            h = (last["usage_min"] or 0) / 60
            _notify(f"Last night: {h:.1f} h · AHI {last['ahi'] if last['ahi'] is not None else '—'}",
                    f"Leak {last['leak_lpm'] if last['leak_lpm'] is not None else '—'} L/min · "
                    f"myAir score {last['sleep_score'] if last['sleep_score'] is not None else '—'}")
    return 0


def cmd_probe(_args) -> int:
    from .sync import make_client, persist_session, run_sync
    client = make_client()
    print("Testing extra SleepRecord fields against ResMed's schema…")
    found = client.probe_fields(progress=lambda m: print("  " + m))
    persist_session(client)
    cfg = secrets_store.load_config()
    cfg["extra_fields"] = found
    secrets_store.save_config(cfg)
    if found:
        print(f"\nAccepted: {', '.join(found)}\nThese are now included in every sync. Re-fetching history…")
        print(run_sync(full=True, client=client)["message"])
    else:
        print("\nNo additional fields beyond the known set.")
    return 0


def cmd_status(_args) -> int:
    cfg = secrets_store.load_config()
    with db.session() as con:
        c = con.execute("SELECT COUNT(*) n, MIN(date) a, MAX(date) b FROM nights").fetchone()
        dev = con.execute("SELECT * FROM devices LIMIT 1").fetchone()
        last = db.meta_get(con, "last_sync")
        rows = db.nights(con)
    print(f"{APP_NAME} {__version__}")
    print(f"  Database:   {paths.db_path()}")
    print(f"  myAir:      {cfg.get('username') or 'not connected'} ({cfg.get('region')})")
    print(f"  Keyring:    {'yes' if secrets_store.keyring_available() else 'not available'}")
    print(f"  Device:     {dev['name'] if dev else '—'}")
    print(f"  Nights:     {c['n']} ({c['a'] or '—'} → {c['b'] or '—'})")
    print(f"  Last sync:  {last or 'never'}")
    if rows:
        s = analysis.build_summary(rows, float(cfg["usage_goal_hours"]), float(cfg["leak_reference"]))
        w = s["windows"].get("30d", {})
        print(f"  Last 30 days: {w.get('compliance_pct')}% nights ≥{cfg['usage_goal_hours']:g}h · "
              f"avg {w.get('usage_avg_h')} h · AHI {w.get('ahi_weighted')} · leak {w.get('leak_median')} L/min")
    return 0


def cmd_export(args) -> int:
    from .server import export_csv
    with db.session() as con:
        rows = db.nights(con)
    data = json.dumps(rows, indent=2, default=str) if args.format == "json" else export_csv()
    if args.output:
        Path(args.output).write_text(data)
        print(f"Wrote {len(rows)} nights to {args.output}")
    else:
        sys.stdout.write(data)
    return 0


def cmd_report(args) -> int:
    from . import report
    out = Path(args.output or f"omacpap-report-{time.strftime('%Y-%m-%d')}.html")
    out.write_text(report.render(days=args.days))
    print(f"Report written to {out.resolve()}")
    return 0


def cmd_import_sd(args) -> int:
    from . import sdcard
    src = Path(args.path) if args.path else next(iter(sdcard.find_cards()), None)
    if not src:
        print("No ResMed SD card found. Pass the card's mount path.")
        return 1
    res = sdcard.import_card(src)
    print(f"Imported {res['days']} days and {res['events']} events from {src}.")
    return 0


def cmd_waybar(_args) -> int:
    """JSON for a Waybar custom module — reads the local database only, never the network."""
    try:
        with db.session() as con:
            last = con.execute("SELECT * FROM nights ORDER BY date DESC LIMIT 1").fetchone()
            last_sync = db.meta_get(con, "last_sync")
    except Exception:  # noqa: BLE001 - never break the bar
        last = None
    if not last:
        print(json.dumps({"text": "", "tooltip": "OmaCPAP: no data yet", "class": "empty"}))
        return 0
    goal = float(secrets_store.load_config()["usage_goal_hours"])
    h = (last["usage_min"] or 0) / 60
    stale = (time.time() - time.mktime(time.strptime(last["date"], "%Y-%m-%d"))) > 2.5 * 86400
    cls = "stale" if stale else ("good" if h >= goal else "low")
    tip = (f"Night of {last['date']}\nUsage {h:.1f} h · AHI {last['ahi']}\n"
           f"Leak {last['leak_lpm']} L/min · score {last['sleep_score']}\nSynced {last_sync or 'never'}")
    print(json.dumps({"text": f"󰒲 {h:.1f}h", "tooltip": tip, "class": cls, "alt": cls}, ensure_ascii=False))
    return 0


def cmd_serve(args) -> int:
    from .server import serve
    port = _port(args)
    httpd = serve(port)
    url = f"http://127.0.0.1:{port}/"
    print(f"{APP_NAME} running at {url}  (Ctrl+C to stop)")
    if args.open:
        _launch(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def _launch(url: str) -> None:
    if shutil.which("omarchy-launch-webapp"):
        subprocess.Popen(["omarchy-launch-webapp", url], start_new_session=True)
    else:
        webbrowser.open(url)


def cmd_open(args) -> int:
    """Start the server in the background if needed, then open the app window."""
    port = _port(args)
    if not _server_up(port):
        log_file = paths.state_dir() / "server.log"
        with open(log_file, "ab") as lf:
            subprocess.Popen([sys.executable, "-m", "omacpap", "serve", "--port", str(port)],
                             stdout=lf, stderr=lf, start_new_session=True,
                             env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)})
        for _ in range(40):
            if _server_up(port):
                break
            time.sleep(0.15)
        else:
            print(f"Server didn't start — see {log_file}", file=sys.stderr)
            return 1
    _launch(f"http://127.0.0.1:{port}/")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="omacpap", description=f"{APP_NAME} — CPAP data analyzer for Omarchy")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("open", help="open the dashboard (default)")
    s.add_argument("--port", type=int)
    s.set_defaults(fn=cmd_open)

    s = sub.add_parser("serve", help="run the dashboard server in the foreground")
    s.add_argument("--port", type=int)
    s.add_argument("--open", action="store_true", help="also open the app window")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("login", help="connect your ResMed myAir account")
    s.add_argument("--username")
    s.add_argument("--region", choices=["NA", "EU"], help="NA = Americas/Australia, EU = Europe")
    s.add_argument("--no-sync", action="store_true")
    s.set_defaults(fn=cmd_login)

    sub.add_parser("logout", help="forget myAir credentials (keeps your data)").set_defaults(fn=cmd_logout)

    s = sub.add_parser("sync", help="fetch new nights from myAir")
    s.add_argument("--full", action="store_true", help="re-download entire history")
    s.add_argument("--quiet", action="store_true")
    s.add_argument("--notify", action="store_true", help="desktop notification with last night's numbers")
    s.set_defaults(fn=cmd_sync)

    sub.add_parser("probe", help="discover extra myAir fields for your account").set_defaults(fn=cmd_probe)
    sub.add_parser("status", help="show connection and data summary").set_defaults(fn=cmd_status)
    sub.add_parser("waybar", help="print last night as Waybar JSON").set_defaults(fn=cmd_waybar)

    s = sub.add_parser("export", help="export nights as CSV or JSON")
    s.add_argument("--format", choices=["csv", "json"], default="csv")
    s.add_argument("-o", "--output")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("report", help="write a printable HTML report")
    s.add_argument("--days", type=int, default=90)
    s.add_argument("-o", "--output")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("import-sd", help="(optional) import a ResMed SD card")
    s.add_argument("path", nargs="?")
    s.set_defaults(fn=cmd_import_sd)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    fn = getattr(args, "fn", cmd_open)
    try:
        return fn(args)
    except MyAirError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
