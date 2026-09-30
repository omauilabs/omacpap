"""Optional ResMed SD-card importer (AirSense 10/11).

Not needed for myAir-only setups. If a card is ever inserted, the machine
writes STR.edf (one record per day: usage, AHI split by apnea type, leak and
pressure percentiles, settings) and DATALOG/ event files. This importer copies
the card into OmaCPAP's archive and loads the daily summaries + events so they
sit alongside the myAir nights.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path
from typing import Any

from . import db, paths
from .edf import EDFError, read_edf

# column -> candidate STR.edf labels (first match wins)
STR_MAP: dict[str, tuple[str, ...]] = {
    "usage_min": ("Duration",),
    "ahi": ("AHI",), "oai": ("OAI",), "cai": ("CAI",), "hi": ("HI",), "uai": ("UAI",),
    "rin": ("RIN",), "csr": ("CSR",),
    "leak50_lpm": ("Leak.50",), "leak95_lpm": ("Leak.95",), "leakmax_lpm": ("Leak.Max",),
    "press50": ("MaskPress.50", "TgtIPAP.50"), "press95": ("MaskPress.95", "TgtIPAP.95"),
    "pressmax": ("MaskPress.Max", "TgtIPAP.Max"),
    "resp_rate50": ("RespRate.50",), "tidvol50": ("TidVol.50",), "minvent50": ("MinVent.50",),
    "spo2_50": ("SpO2.50",), "mask_events": ("MaskEvents",),
}
EVENT_TYPES = {"obstructive apnea", "central apnea", "hypopnea", "apnea", "arousal", "rera",
               "csr start", "csr end", "unclassified apnea"}


def find_cards() -> list[Path]:
    """Mounted volumes that look like a ResMed card (STR.edf + DATALOG)."""
    roots = [Path("/run/media"), Path("/media"), Path("/mnt")]
    found = []
    for root in roots:
        if not root.exists():
            continue
        for p in list(root.glob("*")) + list(root.glob("*/*")):
            if (p / "STR.edf").exists() or (p / "STR.EDF").exists():
                found.append(p)
    return found


def _is_lps(unit: str) -> bool:
    u = unit.lower().replace(" ", "")
    return u in {"l/s", "l/sec"}


def parse_str(path: Path) -> list[dict[str, Any]]:
    edf = read_edf(path)
    cols: dict[str, list[float]] = {}
    for col, labels in STR_MAP.items():
        idx = edf.find(*labels)
        if idx is None:
            continue
        vals = edf.per_record_value(idx)
        if col.endswith("_lpm") and _is_lps(edf.signals[idx].unit):
            vals = [v * 60 for v in vals]
        cols[col] = vals
    on_i, off_i = edf.find("MaskOn"), edf.find("MaskOff")
    ons = edf.per_record_samples(on_i) if on_i is not None else []
    offs = edf.per_record_samples(off_i) if off_i is not None else []
    settings_idx = [i for i, s in enumerate(edf.signals) if s.label.startswith("S.") or s.label == "Mode"]

    days = []
    for r in range(edf.n_records):
        noon = edf.start + dt.timedelta(seconds=r * edf.record_duration)
        row: dict[str, Any] = {"date": noon.date().isoformat()}
        for col, vals in cols.items():
            v = vals[r] if r < len(vals) else None
            row[col] = None if v is None or v < 0 else round(v, 3)
        if not row.get("usage_min"):
            continue
        sess = []
        if r < len(ons):
            for a, b in zip(ons[r], offs[r] if r < len(offs) else []):
                a, b = round(a), round(b)  # ResMed stores whole minutes after noon
                if a >= 0 and b > a:
                    sess.append([(noon + dt.timedelta(minutes=a)).isoformat(timespec="minutes"),
                                 (noon + dt.timedelta(minutes=b)).isoformat(timespec="minutes")])
        row["sessions"] = json.dumps(sess)
        row["settings"] = json.dumps({edf.signals[i].label: round(edf.per_record_value(i)[r], 2)
                                      for i in settings_idx})
        days.append(row)
    return days


def parse_events(datalog: Path) -> list[tuple[str, str, float | None, str]]:
    out = []
    for f in sorted(datalog.glob("*/*_EVE.edf")) + sorted(datalog.glob("*/*_CSL.edf")):
        try:
            edf = read_edf(f)
        except EDFError:
            continue
        day = dt.datetime.strptime(f.parent.name, "%Y%m%d").date().isoformat()
        for a in edf.annotations():
            if a.text.lower() in EVENT_TYPES:
                ts = (edf.start + dt.timedelta(seconds=a.onset)).isoformat(timespec="seconds")
                out.append((day, ts, a.duration, a.text))
    return out


def import_card(src: Path, archive: bool = True) -> dict[str, Any]:
    src = Path(src)
    str_file = next((p for p in (src / "STR.edf", src / "STR.EDF") if p.exists()), None)
    if not str_file:
        raise FileNotFoundError(f"No STR.edf in {src} — is this the root of a ResMed SD card?")
    if archive:
        dest = paths.sd_archive_dir()
        shutil.copytree(src, dest, dirs_exist_ok=True)  # accumulate; never delete archived nights
        src = dest
        str_file = src / str_file.name
    days = parse_str(str_file)
    events = parse_events(src / "DATALOG") if (src / "DATALOG").exists() else []
    ts = db.now_iso()
    with db.session() as con:
        for d in days:
            cols = list(d)
            con.execute(
                f"""INSERT INTO sd_days({', '.join(cols)}, updated_at) VALUES({', '.join('?' * len(cols))}, ?)
                    ON CONFLICT(date) DO UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in cols)},
                    updated_at=excluded.updated_at""",
                (*d.values(), ts))
        con.executemany("INSERT OR IGNORE INTO sd_events(date, ts, duration_s, type) VALUES(?,?,?,?)", events)
        db.meta_set(con, "last_sd_import", ts)
    return {"days": len(days), "events": len(events), "archived_to": str(src) if archive else None}
