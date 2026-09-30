"""Tiny EDF/EDF+ writer for building synthetic ResMed SD cards in tests."""

from __future__ import annotations

import datetime as dt
import struct
from pathlib import Path


def _pad(s: str, n: int) -> bytes:
    return s.encode("latin-1")[:n].ljust(n)


def write_edf(path: Path, start: dt.datetime, record_duration: float,
              signals: list[dict], n_records: int, edf_plus: bool = False) -> None:
    """signals: [{label, unit, pmin, pmax, dmin, dmax, spr, values(list of physical floats) | tal(bytes per record)}]"""
    ns = len(signals)
    hdr = b"".join([
        _pad("0", 8), _pad("X X X X", 80), _pad("Startdate X X X X", 80),
        _pad(start.strftime("%d.%m.%y"), 8), _pad(start.strftime("%H.%M.%S"), 8),
        _pad(str(256 + 256 * ns), 8), _pad("EDF+C" if edf_plus else "", 44),
        _pad(str(n_records), 8), _pad(f"{record_duration:g}", 8), _pad(str(ns), 4),
    ])
    cols = [
        ("label", 16), ("transducer", 80), ("unit", 8), ("pmin", 8), ("pmax", 8),
        ("dmin", 8), ("dmax", 8), ("prefilter", 80), ("spr", 8), ("reserved", 32),
    ]
    for key, w in cols:
        for s in signals:
            v = s.get(key, "")
            hdr += _pad(f"{v:g}" if isinstance(v, float) else str(v), w)
    body = bytearray()
    for r in range(n_records):
        for s in signals:
            spr = s["spr"]
            if "tal" in s:
                chunk = s["tal"][r] if r < len(s["tal"]) else b""
                body += chunk[:spr * 2].ljust(spr * 2, b"\x00")
                continue
            gain = (s["pmax"] - s["pmin"]) / (s["dmax"] - s["dmin"])
            vals = s["values"][r * spr:(r + 1) * spr]
            for v in vals:
                d = round((v - s["pmin"]) / gain + s["dmin"])
                body += struct.pack("<h", max(s["dmin"], min(s["dmax"], d)))
    path.write_bytes(hdr + bytes(body))


def sig(label: str, values: list[float], *, unit: str = "", pmin: float = -1, pmax: float = 1000,
        spr: int = 1) -> dict:
    return {"label": label, "unit": unit, "pmin": float(pmin), "pmax": float(pmax),
            "dmin": -32768, "dmax": 32767, "spr": spr, "values": values}


def make_card(root: Path, nights: int = 12, end: dt.date | None = None) -> Path:
    """Write STR.edf + DATALOG EVE files resembling an AirSense 10 AutoSet card."""
    end = end or dt.date.today() - dt.timedelta(days=1)
    start_day = end - dt.timedelta(days=nights - 1)
    root.mkdir(parents=True, exist_ok=True)
    (root / "Identification.tgt").write_text("#SRN 23161234567\n#PNA AirSense_10_AutoSet\n#PCD 37028\n")
    days = range(nights)
    dur = [420.0 + 10 * (i % 5) for i in days]
    on = [[60 * 10.5 + 0.0] + [-1] * 9 for _ in days]        # 22:30 → minutes after noon
    off = [[60 * 10.5 + dur[i]] + [-1] * 9 for i in days]
    signals = [
        sig("Duration", dur, unit="min", pmax=1440),
        sig("MaskOn", [v for row in on for v in row], unit="min", pmax=1440, spr=10),
        sig("MaskOff", [v for row in off for v in row], unit="min", pmax=1440, spr=10),
        sig("AHI", [1.2 + i * 0.1 for i in days], pmax=100),
        sig("OAI", [0.4 for _ in days], pmax=100),
        sig("CAI", [0.3 for _ in days], pmax=100),
        sig("HI", [0.5 + i * 0.1 for i in days], pmax=100),
        sig("Leak.50", [0.05 for _ in days], unit="L/s", pmin=0, pmax=2),
        sig("Leak.95", [0.2 for _ in days], unit="L/s", pmin=0, pmax=2),
        sig("MaskPress.50", [9.4 for _ in days], unit="cmH2O", pmin=0, pmax=30),
        sig("MaskPress.95", [11.8 for _ in days], unit="cmH2O", pmin=0, pmax=30),
        sig("S.AS.MinPress", [7.0 for _ in days], unit="cmH2O", pmin=0, pmax=30),
        sig("S.AS.MaxPress", [15.0 for _ in days], unit="cmH2O", pmin=0, pmax=30),
        sig("S.EPR.Level", [2.0 for _ in days], pmin=0, pmax=10),
    ]
    write_edf(root / "STR.edf", dt.datetime.combine(start_day, dt.time(12)), 86400, signals, nights)

    for i in days[-3:]:
        day = start_day + dt.timedelta(days=i)
        folder = root / "DATALOG" / day.strftime("%Y%m%d")
        folder.mkdir(parents=True, exist_ok=True)
        t0 = dt.datetime.combine(day, dt.time(22, 30))
        tal = (b"+0\x14\x14\x00" + b"+0\x14Recording starts\x14\x00" + b"+3600\x1512\x14Obstructive Apnea\x14\x00"
               + b"+7200\x1515\x14Hypopnea\x14\x00" + b"+9000\x1510\x14Central Apnea\x14\x00")
        write_edf(folder / f"{t0:%Y%m%d_%H%M%S}_EVE.edf", t0, 0.0 or 1.0,
                  [{"label": "EDF Annotations", "unit": "", "pmin": -1.0, "pmax": 1.0, "dmin": -32768,
                    "dmax": 32767, "spr": 60, "tal": [tal]}], 1, edf_plus=True)
    return root
