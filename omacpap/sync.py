"""Glue between the myAir client, the keyring, and the database."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Callable

from . import db, secrets_store
from .myair import MyAirClient, MyAirError, SessionState, get_region

log = logging.getLogger("omacpap.sync")

# myAir sometimes revises the last few nights (late uploads, rescoring).
REVISION_WINDOW_DAYS = 10


class NotConfigured(MyAirError):
    pass


def make_client(cfg: dict[str, Any] | None = None, password: str | None = None) -> MyAirClient:
    cfg = cfg or secrets_store.load_config()
    user = cfg.get("username")
    if not user:
        raise NotConfigured("myAir isn't connected yet. Run `omacpap login` or use Connect in the app.")
    state = SessionState.from_json(secrets_store.get_session(user))
    return MyAirClient(
        username=user,
        password=password or secrets_store.get_password(user),
        region=get_region(cfg.get("region", "NA")),
        state=state,
    )


def persist_session(client: MyAirClient) -> None:
    secrets_store.save_session(client.username, client.state.to_json())


def run_sync(*, full: bool = False, client: MyAirClient | None = None,
             progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    cfg = secrets_store.load_config()
    client = client or make_client(cfg)
    with db.session() as con:
        log_id = db.log_start(con, "myair-full" if full else "myair")
        try:
            last = con.execute("SELECT MAX(date) AS d FROM nights").fetchone()["d"]
            since = None
            if last and not full:
                since = dt.date.fromisoformat(last) - dt.timedelta(days=REVISION_WINDOW_DAYS)
            device, mask, first_name = client.patient_and_device()
            if device:
                db.upsert_device(con, device, mask)
            if first_name:
                db.meta_set(con, "patient_first_name", first_name)
            records = client.history(since=since, extra_fields=cfg.get("extra_fields") or [], progress=progress)
            new, changed = db.upsert_myair_records(con, records)
            db.meta_set(con, "last_sync", db.now_iso())
            persist_session(client)
            msg = f"{len(records)} nights fetched, {new} new, {changed} updated"
            db.log_finish(con, log_id, "ok", new, changed, msg)
            return {"ok": True, "fetched": len(records), "new": new, "updated": changed,
                    "full": since is None, "message": msg}
        except Exception as e:
            persist_session(client)  # keep DT cookie even on failure
            db.log_finish(con, log_id, "error", message=str(e))
            raise
