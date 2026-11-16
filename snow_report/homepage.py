from django.core.cache import cache

from bdh_sdk import INFO, ON, Reading, Status

from .provides import _bottom, _named, _top
from .resort import get_resort

CACHE_KEY = "snow_report:homepage"
CACHE_TTL = 15 * 60


def _temps(rows):
    """Up top, mid and base temperatures (°C), skipping any the resort lacks."""
    picked, seen = [], set()
    for label, row in (("Up top", _top(rows)), ("Mid", _named(rows, "mid")), ("Base", _bottom(rows))):
        if row and row.get("c") is not None and id(row) not in seen:
            seen.add(id(row))
            picked.append((label, round(row["c"])))
    return picked


def _summary():
    data = get_resort()
    if not data:
        return {}
    temps = _temps(data["temperatures"] or [])
    summary = _text(data)
    if temps:
        summary["temps"] = temps
    return summary


def _text(data):
    for snow in data["new_snow"]:
        if snow["cm"] is not None:
            return {"period": snow["period"], "cm": round(snow["cm"])}
    if data["temperatures"]:
        top = max(data["temperatures"], key=lambda t: t["elevation_m"] or 0)
        return {"temp_c": round(top["c"]), "where": top["name"]}
    return {"description": data["narrative"]} if data["narrative"] else {}


def status():
    summary = cache.get(CACHE_KEY)
    if summary is None:
        summary = _summary()
        cache.set(CACHE_KEY, summary, CACHE_TTL)
    readings = tuple(Reading(label, c, "C") for label, c in summary.get("temps", ()))
    if "cm" in summary:
        period = summary["period"]
        if "new" in period.lower():
            text = f"{summary['cm']} cm new snow"
        else:
            text = f"{summary['cm']} cm in {period.lower()}"
        return Status(text, ON if summary["cm"] > 0 else INFO, readings=readings)
    if "temp_c" in summary:
        return Status(f"{summary['temp_c']} °C at {summary['where']}", readings=readings)
    if summary.get("description"):
        return Status(summary["description"], readings=readings)
    return None