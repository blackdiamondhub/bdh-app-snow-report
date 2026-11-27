"""Assistant tools: the Sun Peaks snow report for the hub's voice/chat assistant.

One read-only lookup for every mode: new snow, snow base, the resort's
conditions summary and on-mountain temperatures and wind, from the resort's
conditions page. Answers are small dicts of speech-ready strings in the hub's
units ("4 cm", "-2°C"), with times in lodge time ("today at 12:17 pm").
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from bdh_sdk import ALL_MODES
from bdh_sdk.tools import Tool, ToolError

from .resort import get_resort
from .views import Units, default_units

SNOW_REPORT = "snow_report"

UNAVAILABLE = "The Sun Peaks snow report isn't answering right now."
OFF_SEASON = "Sun Peaks isn't posting a snow report right now; it's probably between seasons."
# Sun Peaks keeps UTC-7 all year.
LODGE_TZ = timezone(timedelta(hours=-7))
# The resort updates the report every morning (and often again later in the day).
STALE_AFTER = timedelta(hours=36)
PERIODS = {"new snow": "overnight", "24 hr": "24 hours", "48 hr": "48 hours", "7 days": "7 days"}
WIND_WORDS = {"N": "north", "E": "east", "S": "south", "W": "west"}
_UPDATED = re.compile(r"([a-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})?\s+at\s+(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m",
                      re.I)


def utc_now():
    return datetime.now(timezone.utc)


def tools(mode=None):
    return [
        Tool(SNOW_REPORT, "Snow report",
             "Sun Peaks ski resort snow report: new snow overnight, 24 h, 48 h and 7 days, snow base "
             "(mid-mountain and alpine), the resort's conditions summary, and temperatures and wind on the "
             "mountain. Use for 'how much new snow?', 'what's the base?', 'how cold is it up top?'.",
             modes=ALL_MODES, readonly=True),
    ]


# ── speech ──

def _time(t):
    if t.hour == 12 and t.minute == 0:
        return "noon"
    minutes = f":{t:%M}" if t.minute else ""
    return f"{t.hour % 12 or 12}{minutes} {'am' if t.hour < 12 else 'pm'}"


def parse_updated(text, local):
    """``"Updated November 27th at 12:17pm"`` → lodge-local naive datetime, or None."""
    m = _UPDATED.search(text or "")
    if not m:
        return None
    month_name, day, year, hour, minute, ap = m.groups()
    try:
        month = datetime.strptime(month_name[:3].title(), "%b").month
        hour = int(hour) % 12 + (12 if ap.lower() == "p" else 0)
        stamp = datetime(int(year or local.year), month, int(day), hour, int(minute or 0))
    except ValueError:
        return None
    if not year and stamp > local + timedelta(days=1):  # December's report read in January
        stamp = stamp.replace(year=stamp.year - 1)
    return stamp


def _when(stamp, local):
    days = (local.date() - stamp.date()).days
    if days == 0:
        day = "today"
    elif days == 1:
        day = "yesterday"
    else:
        day = f"{stamp:%B} {stamp.day}"
    return f"{day} at {_time(stamp)}"


def _depth(u, cm):
    value = u.d(cm)
    return None if value is None else f"{value} {u.depth}"


def _wind_direction(code):
    """"W" → "west", "SW" → "southwest", "WSW" → "west-southwest"."""
    code = (code or "").upper()
    if not code or any(c not in WIND_WORDS for c in code) or len(code) > 3:
        return None
    if len(code) == 3:
        return f"{WIND_WORDS[code[0]]}-{_wind_direction(code[1:])}"
    return "".join(WIND_WORDS[c] for c in code)


def _compact(entry):
    return {k: v for k, v in entry.items() if v not in (None, "", [], {})}


def build_report(resort, units, local):
    """The tool's answer from parsed resort data (see :func:`resort.parse_resort_html`)."""
    u = Units(units)
    new_snow = {}
    for row in resort.get("new_snow") or ():
        value = _depth(u, row.get("cm"))
        if value is not None:
            period = (row.get("period") or "").strip()
            new_snow[PERIODS.get(period.lower(), period.lower())] = value
    base = {}
    for row in resort.get("base") or ():
        value = _depth(u, row.get("cm"))
        if value is not None:
            base[(row.get("name") or "").lower()] = value

    temps = {t["name"]: f"{u.t(t['c'])}{u.temp}" for t in sorted(
        resort.get("temperatures") or (), key=lambda t: -(t.get("elevation_m") or 0)) if t.get("c") is not None}
    winds = [w for w in resort.get("wind") or () if w.get("kph") is not None]
    top = max(winds, key=lambda w: w.get("elevation_m") or 0, default=None)
    wind = None
    if top:
        wind = f"{u.s(top['kph'])} {u.speed}"
        direction = _wind_direction(top.get("direction"))
        if direction and top["kph"]:
            wind += f" from the {direction}"
    aqhi = max((a["aqhi"] for a in resort.get("air_quality") or () if a.get("aqhi") is not None), default=None)

    entry = {}
    if new_snow or base:
        stamp = parse_updated(resort.get("updated"), local)
        entry["updated"] = _when(stamp, local) if stamp else None
        if stamp and local - stamp > STALE_AFTER:
            entry["note"] = "The resort hasn't updated this report recently, so the new snow may be out of date."
        entry["new_snow"] = new_snow
        entry["base"] = base
    else:
        entry["snow_report"] = OFF_SEASON
    entry.update({
        "conditions": resort.get("narrative"),
        "temperatures": temps,
        "wind_up_top": wind,
        "air_quality": f"high risk (AQHI {round(aqhi)})" if aqhi is not None and aqhi > 6 else None,
    })
    return _compact(entry)


def _snow_report(args, now):
    resort = get_resort()
    if not resort or not any(resort.get(k) for k in ("new_snow", "base", "temperatures", "narrative")):
        raise ToolError(UNAVAILABLE)
    local = now.astimezone(LODGE_TZ).replace(tzinfo=None)
    return build_report(resort, default_units(), local)


_RUNNERS = {SNOW_REPORT: _snow_report}


def run_tool(tool_id, args, mode=None):
    runner = _RUNNERS.get(tool_id)
    if runner is None:
        raise ToolError(f"Unknown tool {tool_id!r}")
    return runner(args or {}, utc_now())
