"""Google Workspace queries via the gws-as.sh per-account wrapper.

Wrapper enforces account selection + token-store integrity (see CLAUDE.md).
We shell out with `asyncio.create_subprocess_exec`, parse stdout JSON,
return small structured dicts.

`when` parsing for calendar accepts: "today", "tomorrow", "YYYY-MM-DD",
or an ISO datetime range "<start>/<end>". Anything ambiguous falls back
to today.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("ttyc.backends.gws")

GWS_WRAPPER = Path(os.path.expanduser("~/scripts/gws-as.sh"))
DEFAULT_ACCOUNT = os.getenv("GWS_DEFAULT_ACCOUNT", "gurevich.gary@gmail.com")
ALLOWED_ACCOUNTS = {
    "gary@crunchy.tools",
    "gary@flowocity.ai",
    "gurevich.gary@gmail.com",
    "jerome.cbmb@gmail.com",
}


def _local_tz() -> timezone:
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(os.getenv("DOSSIER_TZ", "America/New_York"))  # type: ignore[return-value]
    except Exception:  # noqa: BLE001
        return timezone.utc


async def _run_gws(account: str, args: list[str], *, timeout_s: float = 8.0) -> dict | list | None:
    """Spawn the wrapper, parse JSON stdout. Returns None on failure (caller decides)."""
    if not GWS_WRAPPER.exists():
        log.warning("gws wrapper not at %s", GWS_WRAPPER)
        return None
    if account not in ALLOWED_ACCOUNTS:
        log.warning("gws account not allowed: %s", account)
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            str(GWS_WRAPPER), account, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            log.warning("gws timeout (account=%s args=%s)", account, args[:3])
            return None
    except Exception as e:  # noqa: BLE001
        log.warning("gws spawn failed: %s", e)
        return None
    if proc.returncode != 0:
        log.warning("gws returncode=%s stderr=%s", proc.returncode, stderr[:300].decode(errors="replace"))
        return None
    out = stdout.decode(errors="replace").strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        log.warning("gws non-JSON stdout: %s", out[:200])
        return None


# ---- calendar ----

_WHEN_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})(?:T[\d:.]+(?:Z|[+-]\d{2}:?\d{2})?)?\s*/\s*(\d{4}-\d{2}-\d{2})(?:T[\d:.]+(?:Z|[+-]\d{2}:?\d{2})?)?\s*$")


def _resolve_when(when: str | None) -> tuple[datetime, datetime]:
    """Map a `when` arg to (timeMin, timeMax) in UTC. Falls back to today."""
    tz = _local_tz()
    today_local = datetime.now(tz).date()
    w = (when or "").strip().lower()
    if not w or w == "today":
        start = datetime.combine(today_local, time.min, tz)
        end = start + timedelta(days=1)
    elif w == "tomorrow":
        start = datetime.combine(today_local + timedelta(days=1), time.min, tz)
        end = start + timedelta(days=1)
    elif w == "this week":
        start = datetime.combine(today_local, time.min, tz)
        end = start + timedelta(days=7)
    elif (m := _WHEN_RE.match(when or "")):
        try:
            d1 = datetime.fromisoformat(m.group(1)).replace(tzinfo=tz)
            d2 = datetime.fromisoformat(m.group(2)).replace(tzinfo=tz) + timedelta(days=1)
            start, end = d1, d2
        except ValueError:
            start = datetime.combine(today_local, time.min, tz)
            end = start + timedelta(days=1)
    else:
        try:
            d = datetime.fromisoformat(w).date() if "-" in w else today_local
            start = datetime.combine(d, time.min, tz)
            end = start + timedelta(days=1)
        except ValueError:
            start = datetime.combine(today_local, time.min, tz)
            end = start + timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _fmt_hhmm(iso_or_date: str, tz: timezone) -> str:
    """Render an event start/end into HH:MM local. All-day events become 'all-day'."""
    if not iso_or_date:
        return "?"
    if "T" not in iso_or_date:
        return "all-day"
    try:
        dt = datetime.fromisoformat(iso_or_date.replace("Z", "+00:00")).astimezone(tz)
        return dt.strftime("%H:%M")
    except ValueError:
        return iso_or_date[:16]


async def calendar(when: str | None = None, account: str | None = None) -> list[dict] | dict:
    """Return events for `when` on the given account's primary calendar.

    Shape: [{start, end, title, attendees}] — local-time HH:MM, attendees as
    bare email list (organizer included if not the account holder).
    """
    acct = account or DEFAULT_ACCOUNT
    time_min, time_max = _resolve_when(when)
    params = {
        "calendarId": "primary",
        "timeMin": time_min.isoformat().replace("+00:00", "Z"),
        "timeMax": time_max.isoformat().replace("+00:00", "Z"),
        "singleEvents": True,
        "orderBy": "startTime",
        "maxResults": 25,
    }
    raw = await _run_gws(acct, ["calendar", "events", "list", "--params", json.dumps(params)])
    if raw is None:
        return {"error": "gws_unavailable"}
    items = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return {"error": "unexpected_response"}
    tz = _local_tz()
    out: list[dict] = []
    for ev in items:
        if ev.get("status") == "cancelled" or ev.get("eventType") == "birthday":
            continue
        start = ev.get("start") or {}
        end = ev.get("end") or {}
        attendees: list[str] = []
        for a in (ev.get("attendees") or []):
            e = a.get("email")
            if e and not a.get("self"):
                attendees.append(e)
        out.append({
            "start": _fmt_hhmm(start.get("dateTime") or start.get("date") or "", tz),
            "end": _fmt_hhmm(end.get("dateTime") or end.get("date") or "", tz),
            "title": ev.get("summary") or "(no title)",
            "attendees": attendees[:10],
        })
    return out


# ---- gmail ----

async def gmail_search(query: str, account: str | None = None,
                       limit: int = 10) -> list[dict] | dict:
    """Search messages on the given account. Shape: [{from, subject, snippet, ts}]."""
    if not query or not query.strip():
        return {"error": "empty_query"}
    acct = account or DEFAULT_ACCOUNT
    list_params = {"userId": "me", "q": query.strip(), "maxResults": int(max(1, min(limit, 25)))}
    listed = await _run_gws(acct, ["gmail", "users", "messages", "list",
                                   "--params", json.dumps(list_params)])
    if listed is None:
        return {"error": "gws_unavailable"}
    messages = listed.get("messages") if isinstance(listed, dict) else None
    if not isinstance(messages, list) or not messages:
        return []
    # Hydrate each message's headers in parallel via separate wrapper calls.
    async def _hydrate(mid: str) -> dict | None:
        gparams = {"userId": "me", "id": mid, "format": "metadata",
                   "metadataHeaders": ["From", "Subject", "Date"]}
        raw = await _run_gws(acct, ["gmail", "users", "messages", "get",
                                    "--params", json.dumps(gparams)],
                             timeout_s=6.0)
        if not isinstance(raw, dict):
            return None
        headers = {h.get("name", "").lower(): h.get("value", "")
                   for h in (raw.get("payload", {}).get("headers") or [])}
        return {
            "from": headers.get("from", ""),
            "subject": headers.get("subject", ""),
            "snippet": (raw.get("snippet") or "")[:300],
            "ts": headers.get("date") or raw.get("internalDate"),
        }
    out = await asyncio.gather(*[_hydrate(m["id"]) for m in messages if m.get("id")])
    return [m for m in out if m]
