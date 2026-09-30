"""Minimal, dependency-free EDF / EDF+ reader tuned for ResMed CPAP files.

Handles the quirks seen on AirSense 10/11 SD cards:
  * files that are still growing (record count -1 or larger than the file),
  * EDF+ annotation channels (EVE / CSL event files),
  * per-signal physical/digital scaling.
"""

from __future__ import annotations

import datetime as dt
import re
import sys
from array import array
from dataclasses import dataclass, field
from pathlib import Path


class EDFError(ValueError):
    pass


@dataclass
class Signal:
    label: str
    transducer: str
    unit: str
    phys_min: float
    phys_max: float
    dig_min: int
    dig_max: int
    prefilter: str
    samples_per_record: int

    @property
    def gain(self) -> float:
        span = self.dig_max - self.dig_min
        return (self.phys_max - self.phys_min) / span if span else 1.0

    @property
    def offset(self) -> float:
        return self.phys_min - self.gain * self.dig_min

    @property
    def is_annotation(self) -> bool:
        return self.label.strip() == "EDF Annotations"


@dataclass
class Annotation:
    onset: float           # seconds from file start
    duration: float | None
    text: str


@dataclass
class EDFFile:
    path: Path
    patient: str
    recording: str
    start: dt.datetime
    n_records: int
    record_duration: float
    signals: list[Signal]
    edf_plus: bool
    _raw: bytes = field(repr=False, default=b"")
    _header_bytes: int = 0

    # ---------------------------------------------------------------- lookup
    def labels(self) -> list[str]:
        return [s.label for s in self.signals]

    def find(self, *candidates: str) -> int | None:
        """Index of the first signal whose label equals (or starts with) a candidate."""
        labels = [s.label.lower() for s in self.signals]
        for c in candidates:
            c = c.lower()
            if c in labels:
                return labels.index(c)
        for c in candidates:
            c = c.lower()
            for i, lab in enumerate(labels):
                if lab.startswith(c):
                    return i
        return None

    @property
    def duration_s(self) -> float:
        return self.n_records * self.record_duration

    def sample_interval(self, idx: int) -> float:
        spr = self.signals[idx].samples_per_record
        return self.record_duration / spr if spr else 0.0

    # ------------------------------------------------------------------ data
    def _record_size(self) -> int:
        return sum(s.samples_per_record for s in self.signals) * 2

    def _signal_offset(self, idx: int) -> int:
        return sum(s.samples_per_record for s in self.signals[:idx]) * 2

    def digital(self, idx: int) -> array:
        """Raw int16 samples for one signal across all records."""
        sig = self.signals[idx]
        rs, off, n = self._record_size(), self._signal_offset(idx), sig.samples_per_record * 2
        out = array("h")
        base = self._header_bytes
        for r in range(self.n_records):
            start = base + r * rs + off
            out.frombytes(self._raw[start:start + n])
        if sys.byteorder == "big":
            out.byteswap()
        return out

    def physical(self, idx: int) -> list[float]:
        sig = self.signals[idx]
        g, o = sig.gain, sig.offset
        return [v * g + o for v in self.digital(idx)]

    def per_record_value(self, idx: int) -> list[float]:
        """First physical sample of each record — how STR.edf stores one value per day."""
        sig = self.signals[idx]
        vals = self.physical(idx)
        spr = max(sig.samples_per_record, 1)
        return vals[::spr]

    def per_record_samples(self, idx: int) -> list[list[float]]:
        sig = self.signals[idx]
        vals = self.physical(idx)
        spr = max(sig.samples_per_record, 1)
        return [vals[i:i + spr] for i in range(0, len(vals), spr)]

    def annotations(self) -> list[Annotation]:
        out: list[Annotation] = []
        for idx, sig in enumerate(self.signals):
            if not sig.is_annotation:
                continue
            rs, off, n = self._record_size(), self._signal_offset(idx), sig.samples_per_record * 2
            for r in range(self.n_records):
                start = self._header_bytes + r * rs + off
                out.extend(_parse_tals(self._raw[start:start + n]))
        # drop the per-record timekeeping TALs (empty text)
        return [a for a in out if a.text]


_TAL = re.compile(rb"([+-]\d+(?:\.\d+)?)(?:\x15(\d+(?:\.\d+)?))?\x14(.*?)\x14\x00", re.S)


def _parse_tals(chunk: bytes) -> list[Annotation]:
    out: list[Annotation] = []
    for m in _TAL.finditer(chunk):
        onset = float(m.group(1))
        dur = float(m.group(2)) if m.group(2) else None
        texts = [t for t in m.group(3).split(b"\x14") if t]
        if not texts:
            out.append(Annotation(onset, dur, ""))
        for t in texts:
            out.append(Annotation(onset, dur, t.decode("latin-1").strip()))
    return out


def _f(b: bytes, default: float = 0.0) -> float:
    s = b.decode("ascii", "replace").strip()
    try:
        return float(s)
    except ValueError:
        return default


def _parse_start(date_b: bytes, time_b: bytes) -> dt.datetime:
    d = date_b.decode("ascii", "replace").strip()
    t = time_b.decode("ascii", "replace").strip()
    try:
        dd, mm, yy = (int(x) for x in re.split(r"[.:\-/ ]", d)[:3])
        hh, mi, ss = (int(x) for x in re.split(r"[.:\-/ ]", t)[:3])
    except ValueError as e:
        raise EDFError(f"bad start date/time {d!r} {t!r}") from e
    year = 1900 + yy if yy >= 85 else 2000 + yy
    return dt.datetime(year, mm, dd, hh, mi, ss)


def read_edf(path: str | Path) -> EDFFile:
    path = Path(path)
    raw = path.read_bytes()
    if len(raw) < 256:
        raise EDFError(f"{path.name}: too short to be EDF")
    h = raw[:256]
    header_bytes = int(_f(h[184:192]))
    reserved = h[192:236].decode("ascii", "replace")
    n_records = int(_f(h[236:244], -1))
    record_duration = _f(h[244:252], 1.0)
    ns = int(_f(h[252:256]))
    if ns <= 0 or header_bytes != 256 + ns * 256:
        raise EDFError(f"{path.name}: inconsistent header ({ns} signals, {header_bytes} bytes)")

    sh = raw[256:header_bytes]

    def field_(width: int, pos: int) -> list[bytes]:
        start = pos * ns
        return [sh[start + i * width:start + (i + 1) * width] for i in range(ns)]

    # field widths in order and their cumulative position multipliers
    widths = [16, 80, 8, 8, 8, 8, 8, 80, 8, 32]
    cols, pos = [], 0
    for w in widths:
        cols.append([sh[pos + i * w:pos + (i + 1) * w] for i in range(ns)])
        pos += w * ns
    labels, trans, units, pmin, pmax, dmin, dmax, pref, spr, _ = cols

    signals = [
        Signal(
            label=labels[i].decode("latin-1").strip(),
            transducer=trans[i].decode("latin-1").strip(),
            unit=units[i].decode("latin-1").strip(),
            phys_min=_f(pmin[i]),
            phys_max=_f(pmax[i]),
            dig_min=int(_f(dmin[i], -32768)),
            dig_max=int(_f(dmax[i], 32767)),
            prefilter=pref[i].decode("latin-1").strip(),
            samples_per_record=int(_f(spr[i])),
        )
        for i in range(ns)
    ]
    rec_size = sum(s.samples_per_record for s in signals) * 2
    available = (len(raw) - header_bytes) // rec_size if rec_size else 0
    if n_records < 0 or n_records > available:
        n_records = available  # growing / truncated file

    return EDFFile(
        path=path,
        patient=h[8:88].decode("latin-1").strip(),
        recording=h[88:168].decode("latin-1").strip(),
        start=_parse_start(h[168:176], h[176:184]),
        n_records=n_records,
        record_duration=record_duration,
        signals=signals,
        edf_plus=reserved.startswith("EDF+"),
        _raw=raw,
        _header_bytes=header_bytes,
    )
