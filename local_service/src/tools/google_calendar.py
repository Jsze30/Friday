from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from ..google_auth import load_credentials
from .base import ToolParam, ToolResult, tool

DEFAULT_DURATION_MINUTES = 60
NOT_CONNECTED_MSG = (
    "Google Calendar isn't connected yet. Run "
    "`uv run python scripts/google_calendar_auth.py` from the local_service "
    "directory to connect it."
)


def _local_tz() -> ZoneInfo:
    # /etc/localtime is a symlink like /var/db/timezone/zoneinfo/America/Chicago
    try:
        target = Path("/etc/localtime").resolve()
        parts = target.parts
        if "zoneinfo" in parts:
            i = parts.index("zoneinfo")
            name = "/".join(parts[i + 1 :])
            return ZoneInfo(name)
    except Exception:
        pass
    return ZoneInfo("UTC")


def _service():
    creds = load_credentials()
    if creds is None:
        return None
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _list_calendars(svc) -> list[dict]:
    """Return all subscribed calendars (id, summary, primary). Skips hidden ones."""
    out: list[dict] = []
    page_token: str | None = None
    while True:
        resp = svc.calendarList().list(pageToken=page_token, showHidden=False).execute()
        for c in resp.get("items", []):
            if c.get("hidden"):
                continue
            out.append(c)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


def _resolve_calendar_id(svc, name: str | None) -> tuple[str, str]:
    """Map a user-supplied calendar name to (id, display_name). None → primary."""
    if not name:
        return "primary", "your calendar"
    needle = name.strip().lower()
    cals = _list_calendars(svc)
    for c in cals:
        if (c.get("summary") or "").lower() == needle:
            return c["id"], c.get("summary") or c["id"]
    for c in cals:
        if needle in (c.get("summary") or "").lower():
            return c["id"], c.get("summary") or c["id"]
    raise ValueError(f"No calendar matching '{name}'")


WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thurs": 3,
    "fri": 4, "sat": 5, "sun": 6,
}


def _day_window(d, tz) -> tuple[datetime, datetime]:
    return (
        datetime.combine(d, time(0, 0), tz),
        datetime.combine(d, time(23, 59, 59), tz),
    )


def _resolve_range(range_: str | None) -> tuple[datetime, datetime, str]:
    tz = _local_tz()
    now = datetime.now(tz)
    today = now.date()
    label = (range_ or "today").lower().strip().replace("_", " ")

    if label in ("now", "next", "upcoming"):
        return now, now + timedelta(hours=24), "the next 24 hours"
    if label in ("today", "day"):
        s, e = _day_window(today, tz)
        return s, e, "today"
    if label == "tomorrow":
        s, e = _day_window(today + timedelta(days=1), tz)
        return s, e, "tomorrow"
    if label == "yesterday":
        s, e = _day_window(today - timedelta(days=1), tz)
        return s, e, "yesterday"
    if label in ("week", "this week", "7 days", "next 7 days"):
        return now, now + timedelta(days=7), "the next 7 days"
    if label == "next week":
        # Monday of next week through following Sunday.
        days_until_mon = (7 - today.weekday()) % 7 or 7
        start_d = today + timedelta(days=days_until_mon)
        s, _ = _day_window(start_d, tz)
        _, e = _day_window(start_d + timedelta(days=6), tz)
        return s, e, "next week"
    if label in ("month", "this month", "30 days", "next 30 days"):
        return now, now + timedelta(days=30), "the next 30 days"

    # Weekday name → next occurrence (today counts if it matches).
    if label in WEEKDAYS:
        target = WEEKDAYS[label]
        delta = (target - today.weekday()) % 7
        d = today + timedelta(days=delta)
        s, e = _day_window(d, tz)
        return s, e, d.strftime("%A")

    # ISO date "YYYY-MM-DD".
    try:
        d = datetime.fromisoformat(label).date()
        s, e = _day_window(d, tz)
        return s, e, d.strftime("%a %b %-d")
    except ValueError:
        pass

    # Fallback: full week ahead so we don't silently return an empty window.
    return now, now + timedelta(days=7), "the next 7 days"


def _parse_when(value: str) -> datetime:
    """Accept ISO 8601 with or without timezone. Naive values get local tz."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_local_tz())
    return dt


def _fmt_when(dt_iso: str) -> str:
    try:
        dt = datetime.fromisoformat(dt_iso.replace("Z", "+00:00")).astimezone(_local_tz())
        return dt.strftime("%a %b %-d at %-I:%M %p")
    except Exception:
        return dt_iso


def _now_context() -> dict:
    """Current date/time the LLM should treat as ground truth on every call."""
    now = datetime.now(_local_tz())
    return {
        "today": now.strftime("%Y-%m-%d"),
        "weekday": now.strftime("%A"),
        "now": now.isoformat(timespec="minutes"),
        "timezone": str(now.tzinfo),
    }


def _result(spoken: str, data: dict | None = None) -> ToolResult:
    """ToolResult with current-date context always merged in."""
    merged = dict(data or {})
    merged["context"] = _now_context()
    return ToolResult(spoken=spoken, data=merged)


def _slim_event(ev: dict) -> dict:
    """Strip a Google Calendar event to fields the LLM actually needs.
    Full payloads include etag, attendees, attachments, etc. and blow past
    LiveKit's RPC size limit when many events are returned."""
    s = ev.get("start") or {}
    e = ev.get("end") or {}
    return {
        "title": ev.get("summary") or "(untitled)",
        "start": s.get("dateTime") or s.get("date"),
        "end": e.get("dateTime") or e.get("date"),
        "calendar": ev.get("_calendar"),
        "location": ev.get("location"),
        "id": ev.get("id"),
    }


@tool(
    name="list_calendar_events",
    description=(
        "List events on the user's Google Calendars (all of them by default). "
        "Use for 'what's on my calendar', 'what do I have on Wednesday', "
        "'any exams this week'. STRONGLY PREFER the `range` parameter with "
        "relative keywords ('today', 'tomorrow', 'friday', 'this_week', "
        "'next_week') — the tool resolves them against the user's actual "
        "current local date. Only use start_date/end_date if the user gives "
        "an explicit calendar date (e.g. 'May 8th'). Every response includes a "
        "'context' field with today's real date — trust it over your own "
        "guess of the current date."
    ),
    parameters=[
        ToolParam(
            name="range",
            type="string",
            description=(
                "Relative window: 'now' (next 24h), 'today', 'tomorrow', "
                "'yesterday', a weekday name ('monday'..'sunday'), 'week' "
                "(next 7 days), 'next week', 'month'. Ignored if start_date is given."
            ),
            required=False,
        ),
        ToolParam(
            name="start_date",
            type="string",
            description="Window start as 'YYYY-MM-DD'. Takes precedence over range.",
            required=False,
        ),
        ToolParam(
            name="end_date",
            type="string",
            description="Window end as 'YYYY-MM-DD' (inclusive). Defaults to start_date.",
            required=False,
        ),
        ToolParam(
            name="calendar",
            type="string",
            description=(
                "Filter to a single calendar by name (case-insensitive substring, "
                "e.g. 'Exams', 'Classes'). Omit to search all calendars."
            ),
            required=False,
        ),
        ToolParam(
            name="max_results",
            type="integer",
            description="Max events to return. Defaults to 20.",
            required=False,
        ),
    ],
    permission="read_only",
)
async def list_calendar_events(
    range: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    calendar: str | None = None,
    max_results: int | None = None,
) -> ToolResult:
    svc = await asyncio.to_thread(_service)
    if svc is None:
        return _result(NOT_CONNECTED_MSG, {"error": "not_connected"})

    tz = _local_tz()
    today = datetime.now(tz).date()
    if start_date:
        try:
            sd = datetime.fromisoformat(start_date).date()
            ed = datetime.fromisoformat(end_date).date() if end_date else sd
        except ValueError:
            return _result(
                f"I couldn't parse the date '{start_date}'.", {"error": "bad_date"}
            )
        # Reject obviously-wrong dates (e.g. LLM hallucinated last year).
        if (today - ed).days > 60:
            return _result(
                f"That date ({sd}) is in the past — today is {today.strftime('%A %b %-d, %Y')}. "
                "If you meant the upcoming day, ask again with a relative term like 'this Friday'.",
                {"error": "date_too_old", "given": str(sd)},
            )
        start = datetime.combine(sd, time(0, 0), tz)
        end = datetime.combine(ed, time(23, 59, 59), tz)
        label = (
            sd.strftime("%a %b %-d")
            if sd == ed
            else f"{sd.strftime('%a %b %-d')} through {ed.strftime('%a %b %-d')}"
        )
    else:
        start, end, label = _resolve_range(range)

    limit = max_results if isinstance(max_results, int) and max_results > 0 else 20
    cal_filter = (calendar or "").strip().lower()

    def _fetch_all():
        cals = _list_calendars(svc)
        if cal_filter:
            cals = [
                c for c in cals if cal_filter in (c.get("summary") or "").lower()
            ]
        merged: list[dict] = []
        for c in cals:
            try:
                resp = (
                    svc.events()
                    .list(
                        calendarId=c["id"],
                        timeMin=start.isoformat(),
                        timeMax=end.isoformat(),
                        singleEvents=True,
                        orderBy="startTime",
                        maxResults=limit,
                    )
                    .execute()
                )
            except HttpError:
                continue
            for ev in resp.get("items", []):
                ev["_calendar"] = c.get("summary") or c["id"]
                merged.append(ev)
        return merged

    try:
        items = await asyncio.to_thread(_fetch_all)
    except HttpError as e:
        return _result(
            "Google Calendar didn't respond.",
            {"error": "api_error", "detail": str(e)},
        )

    def _start_key(ev: dict) -> str:
        s = ev.get("start") or {}
        return s.get("dateTime") or s.get("date") or ""

    items.sort(key=_start_key)
    items = items[:limit]

    scope = f" on {calendar}" if cal_filter else ""
    if not items:
        return _result(
            f"You have nothing{scope} for {label}.",
            {"events": [], "range": label},
        )

    multi_cal = len({ev.get("_calendar") for ev in items}) > 1
    summaries = []
    for ev in items:
        title = ev.get("summary") or "(untitled)"
        start_str = (ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get("date")
        when = _fmt_when(start_str) if start_str else "no time"
        suffix = f" ({ev['_calendar']})" if multi_cal else ""
        summaries.append(f"{title} {when}{suffix}")

    if len(items) == 1:
        spoken = f"You have one event {label}: {summaries[0]}."
    else:
        spoken = f"You have {len(items)} events {label}: " + "; ".join(summaries) + "."

    return _result(
        spoken, {"range": label, "events": [_slim_event(ev) for ev in items]}
    )


@tool(
    name="create_calendar_event",
    description=(
        "Create an event on the user's primary Google Calendar. Use for 'schedule', "
        "'add to my calendar', 'book a meeting'. If the user doesn't specify an end "
        "time, the event defaults to one hour."
    ),
    parameters=[
        ToolParam(
            name="title",
            type="string",
            description="Event title.",
            required=True,
        ),
        ToolParam(
            name="start",
            type="string",
            description="Start time as ISO 8601 (e.g. '2026-05-05T15:00'). Local tz assumed if no offset.",
            required=True,
        ),
        ToolParam(
            name="end",
            type="string",
            description="End time as ISO 8601. Optional; defaults to start + 1 hour.",
            required=False,
        ),
        ToolParam(
            name="description",
            type="string",
            description="Event notes.",
            required=False,
        ),
        ToolParam(
            name="location",
            type="string",
            description="Event location.",
            required=False,
        ),
        ToolParam(
            name="calendar",
            type="string",
            description="Calendar name to add to (case-insensitive substring match). Defaults to primary.",
            required=False,
        ),
    ],
    permission="low_risk_write",
)
async def create_calendar_event(
    title: str,
    start: str,
    end: str | None = None,
    description: str | None = None,
    location: str | None = None,
    calendar: str | None = None,
) -> ToolResult:
    svc = await asyncio.to_thread(_service)
    if svc is None:
        return _result(NOT_CONNECTED_MSG, {"error": "not_connected"})

    try:
        cal_id, cal_name = await asyncio.to_thread(_resolve_calendar_id, svc, calendar)
    except ValueError as e:
        return _result(str(e), {"error": "unknown_calendar"})

    try:
        start_dt = _parse_when(start)
    except ValueError:
        return _result(
            f"I couldn't understand the start time '{start}'.",
            {"error": "bad_start"},
        )

    if end:
        try:
            end_dt = _parse_when(end)
        except ValueError:
            return _result(
                f"I couldn't understand the end time '{end}'.",
                {"error": "bad_end"},
            )
    else:
        end_dt = start_dt + timedelta(minutes=DEFAULT_DURATION_MINUTES)

    body = {
        "summary": title,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": str(start_dt.tzinfo)},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": str(end_dt.tzinfo)},
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location

    def _insert():
        return svc.events().insert(calendarId=cal_id, body=body).execute()

    try:
        created = await asyncio.to_thread(_insert)
    except HttpError as e:
        return _result(
            "Google Calendar rejected the event.",
            {"error": "api_error", "detail": str(e)},
        )

    when = _fmt_when(created["start"]["dateTime"])
    return _result(
        f"Added '{title}' to {cal_name} for {when}.",
        {"event": _slim_event({**created, "_calendar": cal_name}), "calendar": cal_name},
    )


@tool(
    name="find_free_time",
    description=(
        "Find free slots on the user's primary calendar of at least the requested "
        "duration. Use for 'when am I free tomorrow', 'find me an hour this week'."
    ),
    parameters=[
        ToolParam(
            name="duration_minutes",
            type="integer",
            description="Minimum slot length in minutes.",
            required=True,
        ),
        ToolParam(
            name="within",
            type="string",
            description="'today', 'tomorrow', or 'week'. Defaults to 'today'.",
            required=False,
        ),
    ],
    permission="read_only",
)
async def find_free_time(
    duration_minutes: int, within: str | None = None
) -> ToolResult:
    svc = await asyncio.to_thread(_service)
    if svc is None:
        return _result(NOT_CONNECTED_MSG, {"error": "not_connected"})

    start, end, label = _resolve_range(within or "today")
    # Free-time only looks forward — drop any window already in the past.
    now = datetime.now(_local_tz())
    if start < now:
        start = now

    def _busy():
        cals = _list_calendars(svc)
        cal_ids = [c["id"] for c in cals]
        return (
            svc.freebusy()
            .query(
                body={
                    "timeMin": start.isoformat(),
                    "timeMax": end.isoformat(),
                    "items": [{"id": cid} for cid in cal_ids],
                }
            )
            .execute()
        ), cal_ids

    try:
        fb, cal_ids = await asyncio.to_thread(_busy)
    except HttpError as e:
        return _result(
            "Google Calendar didn't respond.",
            {"error": "api_error", "detail": str(e)},
        )

    cal_map = fb.get("calendars", {})
    all_busy: list[tuple[datetime, datetime]] = []
    for cid in cal_ids:
        for b in cal_map.get(cid, {}).get("busy", []):
            all_busy.append((_parse_when(b["start"]), _parse_when(b["end"])))

    # Merge overlapping busy intervals.
    all_busy.sort()
    busy_dt: list[tuple[datetime, datetime]] = []
    for s, e in all_busy:
        if busy_dt and s <= busy_dt[-1][1]:
            busy_dt[-1] = (busy_dt[-1][0], max(busy_dt[-1][1], e))
        else:
            busy_dt.append((s, e))

    need = timedelta(minutes=int(duration_minutes))
    slots: list[tuple[datetime, datetime]] = []
    cursor = start
    for b_start, b_end in busy_dt:
        if b_start - cursor >= need:
            slots.append((cursor, b_start))
        cursor = max(cursor, b_end)
    if end - cursor >= need:
        slots.append((cursor, end))

    if not slots:
        return _result(
            f"You don't have a free {duration_minutes}-minute window {label}.",
            {"slots": [], "range": label},
        )

    parts = [
        f"{s.strftime('%-I:%M %p')} to {e.strftime('%-I:%M %p')}"
        for s, e in slots[:5]
    ]
    spoken = f"You're free {label}: " + "; ".join(parts) + "."
    return _result(
        spoken,
        {
            "range": label,
            "slots": [{"start": s.isoformat(), "end": e.isoformat()} for s, e in slots],
        },
    )
