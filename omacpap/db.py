"""SQLite storage. One file, WAL mode, 0600 permissions. All upserts are idempotent
so a full re-sync or re-import never duplicates a night."""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import paths

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- One row per sleep day as reported by myAir (the night's start date).
CREATE TABLE IF NOT EXISTS nights (
    date          TEXT PRIMARY KEY,           -- YYYY-MM-DD
    usage_min     INTEGER,                    -- totalUsage
    ahi           REAL,                       -- events / hour
    leak_lpm      REAL,                       -- leakPercentile (95th pct leak, L/min)
    mask_pairs    INTEGER,                    -- maskPairCount (mask on/off)
    sleep_score   INTEGER,                    -- total myAir score (0-100)
    usage_score   INTEGER,                    -- 0-70
    ahi_score     INTEGER,                    -- 0-5
    mask_score    INTEGER,                    -- 0-5
    leak_score    INTEGER,                    -- 0-20
    extra         TEXT,                       -- JSON: extra fields found by `omacpap probe`
    raw           TEXT,                       -- JSON: the untouched myAir record
    first_seen    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    serial        TEXT PRIMARY KEY,
    name          TEXT,
    series        TEXT,
    family        TEXT,
    manufacturer  TEXT,
    mask_code     TEXT,
    last_report   TEXT,
    raw           TEXT,
    updated_at    TEXT NOT NULL
);

-- Optional: daily summaries from an SD card's STR.edf (only if a card is ever used).
CREATE TABLE IF NOT EXISTS sd_days (
    date          TEXT PRIMARY KEY,
    usage_min     REAL,
    ahi REAL, oai REAL, cai REAL, hi REAL, uai REAL, rin REAL, csr REAL,
    leak50_lpm REAL, leak95_lpm REAL, leakmax_lpm REAL,
    press50 REAL, press95 REAL, pressmax REAL,
    resp_rate50 REAL, tidvol50 REAL, minvent50 REAL, spo2_50 REAL,
    mask_events   INTEGER,
    sessions      TEXT,                       -- JSON [[on_iso, off_iso], ...]
    settings      TEXT,                       -- JSON of S.* machine settings
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sd_events (
    date          TEXT NOT NULL,
    ts            TEXT NOT NULL,
    duration_s    REAL,
    type          TEXT NOT NULL,
    PRIMARY KEY (ts, type)
);
CREATE INDEX IF NOT EXISTS sd_events_date ON sd_events(date);

CREATE TABLE IF NOT EXISTS sync_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT,
    nights_new    INTEGER DEFAULT 0,
    nights_updated INTEGER DEFAULT 0,
    message       TEXT
);

CREATE TABLE IF NOT EXISTS notes (
    date          TEXT PRIMARY KEY,
    text          TEXT NOT NULL,
    tags          TEXT,
    updated_at    TEXT NOT NULL
);
"""

NIGHT_FIELDS = {
    # myAir key -> column
    "totalUsage": "usage_min",
    "ahi": "ahi",
    "leakPercentile": "leak_lpm",
    "maskPairCount": "mask_pairs",
    "sleepScore": "sleep_score",
    "usageScore": "usage_score",
    "ahiScore": "ahi_score",
    "maskScore": "mask_score",
    "leakScore": "leak_score",
}
INT_COLS = {"usage_min", "mask_pairs", "sleep_score", "usage_score", "ahi_score", "mask_score", "leak_score"}


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = Path(path or paths.db_path())
    new = not path.exists()
    con = sqlite3.connect(path, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    con.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
    con.commit()
    if new:
        for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
            try:
                os.chmod(p, 0o600)
            except OSError:
                pass
    return con


@contextmanager
def session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    con = connect(path)
    try:
        yield con
        con.commit()
    finally:
        con.close()


# ------------------------------------------------------------------ helpers
def _num(v: Any, as_int: bool) -> float | int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if as_int else f


def meta_get(con: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value))


# ------------------------------------------------------------------ nights
def upsert_myair_records(con: sqlite3.Connection, records: Iterable[dict[str, Any]]) -> tuple[int, int]:
    """Insert/update myAir sleep records. Returns (new, changed)."""
    new = changed = 0
    ts = now_iso()
    for rec in records:
        date = str(rec.get("startDate") or "")[:10]
        if len(date) != 10:
            continue
        cols = {col: _num(rec.get(key), col in INT_COLS) for key, col in NIGHT_FIELDS.items()}
        known = set(NIGHT_FIELDS) | {"startDate", "__typename", "sleepRecordPatientId"}
        extra = {k: v for k, v in rec.items() if k not in known}
        raw = json.dumps(rec, sort_keys=True)
        prev = con.execute("SELECT raw FROM nights WHERE date=?", (date,)).fetchone()
        if prev is None:
            new += 1
        elif prev["raw"] == raw:
            continue
        else:
            changed += 1
        con.execute(
            f"""INSERT INTO nights(date, {', '.join(cols)}, extra, raw, first_seen, updated_at)
                VALUES (?, {', '.join('?' * len(cols))}, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                {', '.join(f'{c}=excluded.{c}' for c in cols)},
                extra=excluded.extra, raw=excluded.raw, updated_at=excluded.updated_at""",
            (date, *cols.values(), json.dumps(extra, sort_keys=True) if extra else None, raw, ts, ts),
        )
    return new, changed


def upsert_device(con: sqlite3.Connection, dev: dict[str, Any], mask_code: str | None) -> None:
    serial = dev.get("serialNumber") or "unknown"
    con.execute(
        """INSERT INTO devices(serial,name,series,family,manufacturer,mask_code,last_report,raw,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(serial) DO UPDATE SET name=excluded.name, series=excluded.series,
             family=excluded.family, manufacturer=excluded.manufacturer, mask_code=excluded.mask_code,
             last_report=excluded.last_report, raw=excluded.raw, updated_at=excluded.updated_at""",
        (serial, dev.get("localizedName"), dev.get("deviceSeries"), dev.get("deviceFamily"),
         dev.get("fgDeviceManufacturerName"), mask_code, dev.get("lastSleepDataReportTime"),
         json.dumps(dev, sort_keys=True), now_iso()),
    )


def nights(con: sqlite3.Connection, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
    """Nights joined with any SD-card detail and user notes, oldest first."""
    q = """SELECT n.date, n.usage_min, n.ahi, n.leak_lpm, n.mask_pairs, n.sleep_score, n.usage_score,
                  n.ahi_score, n.mask_score, n.leak_score, n.extra, 'myair' AS source,
                  s.oai, s.cai, s.hi, s.uai, s.press50, s.press95,
                  t.text AS note, t.tags AS note_tags
           FROM nights n
           LEFT JOIN sd_days s ON s.date = n.date
           LEFT JOIN notes t ON t.date = n.date
           WHERE (?1 IS NULL OR n.date >= ?1) AND (?2 IS NULL OR n.date <= ?2)
           UNION ALL
           SELECT s.date, CAST(ROUND(s.usage_min) AS INTEGER), s.ahi, s.leak95_lpm, s.mask_events,
                  NULL, NULL, NULL, NULL, NULL, NULL, 'sd',
                  s.oai, s.cai, s.hi, s.uai, s.press50, s.press95,
                  t.text, t.tags
           FROM sd_days s LEFT JOIN notes t ON t.date = s.date
           WHERE s.date NOT IN (SELECT date FROM nights)
             AND (?1 IS NULL OR s.date >= ?1) AND (?2 IS NULL OR s.date <= ?2)
           ORDER BY 1"""
    out = []
    for r in con.execute(q, (start, end)).fetchall():
        d = dict(r)
        d["extra"] = json.loads(d["extra"]) if d.get("extra") else None
        out.append(d)
    return out


def set_note(con: sqlite3.Connection, date: str, text: str, tags: str | None = None) -> None:
    if not text.strip():
        con.execute("DELETE FROM notes WHERE date=?", (date,))
        return
    con.execute(
        """INSERT INTO notes(date,text,tags,updated_at) VALUES(?,?,?,?)
           ON CONFLICT(date) DO UPDATE SET text=excluded.text, tags=excluded.tags, updated_at=excluded.updated_at""",
        (date, text.strip(), tags, now_iso()),
    )


# ------------------------------------------------------------------ sync log
def log_start(con: sqlite3.Connection, source: str) -> int:
    cur = con.execute("INSERT INTO sync_log(source, started_at, status) VALUES(?,?,'running')", (source, now_iso()))
    con.commit()
    return int(cur.lastrowid)


def log_finish(con: sqlite3.Connection, log_id: int, status: str, new: int = 0, updated: int = 0,
               message: str | None = None) -> None:
    con.execute(
        "UPDATE sync_log SET finished_at=?, status=?, nights_new=?, nights_updated=?, message=? WHERE id=?",
        (now_iso(), status, new, updated, message, log_id),
    )
    con.commit()


def last_syncs(con: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    return [dict(r) for r in con.execute("SELECT * FROM sync_log ORDER BY id DESC LIMIT ?", (limit,))]
