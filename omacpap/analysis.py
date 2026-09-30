"""Derived statistics. Purely descriptive — OmaCPAP reports what the machine
recorded; it does not diagnose or recommend therapy changes."""

from __future__ import annotations

import datetime as dt
import statistics
from collections import defaultdict
from typing import Any

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _d(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def calendar_fill(nights: list[dict[str, Any]], start: dt.date | None = None,
                  end: dt.date | None = None) -> list[dict[str, Any]]:
    """One entry per calendar day; days with no record become zero-usage placeholders."""
    if not nights and not (start and end):
        return []
    by_date = {n["date"]: n for n in nights}
    start = start or _d(nights[0]["date"])
    end = end or _d(nights[-1]["date"])
    out, day = [], start
    while day <= end:
        key = day.isoformat()
        out.append(by_date.get(key) or {"date": key, "usage_min": 0, "ahi": None, "leak_lpm": None,
                                        "mask_pairs": None, "sleep_score": None, "missing": True})
        day += dt.timedelta(days=1)
    return out


def _used(n: dict[str, Any]) -> bool:
    return (n.get("usage_min") or 0) > 0


def _weighted_ahi(nights: list[dict[str, Any]]) -> float | None:
    num = sum((n["ahi"] or 0) * n["usage_min"] for n in nights if _used(n) and n.get("ahi") is not None)
    den = sum(n["usage_min"] for n in nights if _used(n) and n.get("ahi") is not None)
    return round(num / den, 2) if den else None


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 2)


def streaks(days: list[dict[str, Any]], goal_min: float) -> tuple[int, int]:
    """(current, longest) run of consecutive calendar days meeting the goal."""
    cur = longest = 0
    for n in days:
        if (n.get("usage_min") or 0) >= goal_min:
            cur += 1
            longest = max(longest, cur)
        else:
            cur = 0
    # a missing "tonight" (today not yet recorded) shouldn't break the current streak
    return cur, longest


def window_summary(days: list[dict[str, Any]], goal_h: float, leak_ref: float) -> dict[str, Any]:
    goal = goal_h * 60
    used = [n for n in days if _used(n)]
    usage_h = [n["usage_min"] / 60 for n in used]
    ahis = [n["ahi"] for n in used if n.get("ahi") is not None]
    leaks = [n["leak_lpm"] for n in used if n.get("leak_lpm") is not None]
    scores = [n["sleep_score"] for n in used if n.get("sleep_score") is not None]
    pairs = [n["mask_pairs"] for n in used if n.get("mask_pairs") is not None]
    met = sum(1 for n in days if (n.get("usage_min") or 0) >= goal)
    return {
        "days": len(days),
        "nights_used": len(used),
        "nights_goal": met,
        "compliance_pct": round(100 * met / len(days), 1) if days else None,
        "usage_avg_h": round(statistics.mean(usage_h), 2) if usage_h else None,
        "usage_avg_all_days_h": round(sum(usage_h) / len(days), 2) if days else None,
        "usage_median_h": round(statistics.median(usage_h), 2) if usage_h else None,
        "usage_total_h": round(sum(usage_h), 1),
        "ahi_weighted": _weighted_ahi(used),
        "ahi_median": _pct(ahis, 0.5),
        "ahi_max": max(ahis) if ahis else None,
        "nights_ahi_over_5": sum(1 for a in ahis if a >= 5),
        "leak_median": _pct(leaks, 0.5),
        "leak_p95": _pct(leaks, 0.95),
        "nights_leak_over_ref": sum(1 for x in leaks if x >= leak_ref),
        "score_avg": round(statistics.mean(scores), 1) if scores else None,
        "mask_pairs_avg": round(statistics.mean(pairs), 1) if pairs else None,
    }


def best_30_day_window(days: list[dict[str, Any]], goal_h: float) -> dict[str, Any] | None:
    """Highest share of goal-meeting nights in any 30 consecutive days.
    (US insurers commonly check 4h+ on 70% of nights in a 30-day span.)"""
    if len(days) < 30:
        return None
    goal = goal_h * 60
    flags = [1 if (n.get("usage_min") or 0) >= goal else 0 for n in days]
    run = sum(flags[:30])
    best, best_i = run, 0
    for i in range(30, len(flags)):
        run += flags[i] - flags[i - 30]
        if run > best:
            best, best_i = run, i - 29
    return {"start": days[best_i]["date"], "end": days[best_i + 29]["date"],
            "nights": best, "pct": round(100 * best / 30, 1)}


def weekday_pattern(days: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[int, list[float]] = defaultdict(list)
    for n in days:
        if _used(n):
            buckets[_d(n["date"]).weekday()].append(n["usage_min"] / 60)
    return [{"day": WEEKDAYS[i], "usage_avg_h": round(statistics.mean(buckets[i]), 2) if buckets[i] else None,
             "nights": len(buckets[i])} for i in range(7)]


def monthly(days: list[dict[str, Any]], goal_h: float, leak_ref: float) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for n in days:
        groups[n["date"][:7]].append(n)
    return [{"month": m, **window_summary(v, goal_h, leak_ref)} for m, v in sorted(groups.items())]


def _delta_phrase(now: float | None, before: float | None, unit: str, fmt: str = "{:.1f}") -> str | None:
    if now is None or before is None:
        return None
    if abs(now - before) < 0.05:
        return f"about the same as the 30 nights before ({fmt.format(before)}{unit})"
    word = "up" if now > before else "down"
    return f"{word} from {fmt.format(before)}{unit} the 30 nights before"


def insights(days: list[dict[str, Any]], goal_h: float, leak_ref: float) -> list[dict[str, str]]:
    """Plain-language observations. Descriptive only."""
    out: list[dict[str, str]] = []
    if not days:
        return out
    last30 = days[-30:]
    prev30 = days[-60:-30] if len(days) >= 60 else []
    a, b = window_summary(last30, goal_h, leak_ref), (window_summary(prev30, goal_h, leak_ref) if prev30 else {})

    if a["days"]:
        out.append({"kind": "usage", "text":
                    f"You used CPAP {goal_h:g}+ hours on {a['nights_goal']} of the last {a['days']} nights "
                    f"({a['compliance_pct']:.0f}%)."})
    if a["usage_avg_h"] is not None:
        d = _delta_phrase(a["usage_avg_h"], b.get("usage_avg_h"), " h")
        out.append({"kind": "usage", "text":
                    f"Average use on nights you wore it: {a['usage_avg_h']:.1f} h" + (f", {d}." if d else ".")})
    if a["ahi_weighted"] is not None:
        d = _delta_phrase(a["ahi_weighted"], b.get("ahi_weighted"), "")
        out.append({"kind": "ahi", "text":
                    f"Usage-weighted AHI over the last 30 nights: {a['ahi_weighted']:.1f} events/hr"
                    + (f", {d}." if d else ".")})
        if a["nights_ahi_over_5"]:
            out.append({"kind": "ahi", "text":
                        f"{a['nights_ahi_over_5']} of those nights had an AHI of 5 or higher "
                        f"(highest: {a['ahi_max']:.1f})."})
    if a["leak_median"] is not None:
        t = f"Median mask leak: {a['leak_median']:.1f} L/min."
        if a["nights_leak_over_ref"]:
            t += f" Leak reached {leak_ref:g} L/min or more on {a['nights_leak_over_ref']} night(s) — worth a look at mask fit."
        out.append({"kind": "leak", "text": t})
    if a["mask_pairs_avg"] is not None and a["mask_pairs_avg"] >= 3:
        out.append({"kind": "mask", "text":
                    f"You're averaging {a['mask_pairs_avg']:.1f} mask on/off cycles a night."})
    wk = [w for w in weekday_pattern(days[-90:]) if w["usage_avg_h"] is not None and w["nights"] >= 3]
    if len(wk) >= 5:
        lo, hi = min(wk, key=lambda w: w["usage_avg_h"]), max(wk, key=lambda w: w["usage_avg_h"])
        if hi["usage_avg_h"] - lo["usage_avg_h"] >= 0.75:
            out.append({"kind": "pattern", "text":
                        f"Over the last 90 days, {lo['day']} nights are your shortest "
                        f"({lo['usage_avg_h']:.1f} h) and {hi['day']} your longest ({hi['usage_avg_h']:.1f} h)."})
    cur, longest = streaks(days, goal_h * 60)
    if cur >= 3:
        out.append({"kind": "streak", "text":
                    f"Current streak: {cur} nights in a row at {goal_h:g}+ hours (longest ever: {longest})."})
    return out


def build_summary(nights: list[dict[str, Any]], goal_h: float = 4.0, leak_ref: float = 24.0) -> dict[str, Any]:
    if not nights:
        return {"empty": True}
    end = max(dt.date.today() - dt.timedelta(days=1), _d(nights[-1]["date"]))
    days = calendar_fill(nights, end=end)
    # don't count "last night" as missed if myAir simply hasn't reported it yet
    if days and days[-1].get("missing"):
        days = days[:-1]
    cur, longest = streaks(days, goal_h * 60)
    last_used = next((n for n in reversed(days) if _used(n)), None)
    windows = {}
    for label, n in (("7d", 7), ("30d", 30), ("90d", 90), ("365d", 365)):
        if len(days) >= min(n, 1):
            windows[label] = window_summary(days[-n:], goal_h, leak_ref)
    windows["all"] = window_summary(days, goal_h, leak_ref)
    return {
        "empty": False,
        "first_date": days[0]["date"],
        "last_date": days[-1]["date"],
        "last_night": last_used,
        "streak_current": cur,
        "streak_longest": longest,
        "windows": windows,
        "best_30": best_30_day_window(days, goal_h),
        "weekday": weekday_pattern(days[-90:]),
        "monthly": monthly(days, goal_h, leak_ref),
        "insights": insights(days, goal_h, leak_ref),
        "goal_h": goal_h,
        "leak_ref": leak_ref,
    }
