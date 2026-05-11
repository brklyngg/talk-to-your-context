"""Mission Control queries via Supabase PostgREST.

Mirrors `~/VibeCoding/mission-control/lib/supabase-helper.js` so the data
contract is identical — same tables (`cards`, `journal_entries`), same
column names. We talk PostgREST directly via httpx; no node subprocess.

Env lookup order for creds:
  1. SUPABASE_URL / SUPABASE_SERVICE_KEY (env)
  2. ~/.config/secrets/supabase-mission-control-{url,service-key} (files)

All functions return `{"error": "<short>"}` on failure rather than raising,
so the calling tool dispatch can pass a structured failure to Realtime.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("ttyc.backends.supabase")

CARDS_TABLE = "cards"
JOURNAL_TABLE = "journal_entries"
SECRETS_DIR = Path(os.path.expanduser("~/.config/secrets"))

# Columns excluded from the "open loops" universe.
_CLOSED_COLUMNS = {"done", "archived"}

# Light-weight projection so we don't ship 50KB blobs back to Realtime when a
# few hundred bytes suffice.
_CARD_SELECT = ",".join([
    "id", "title", "column_id", "priority", "tags",
    "description", "extended_context", "position",
    "created_at", "updated_at", "completed_at", "archived_at",
])


def _creds() -> tuple[str | None, str | None]:
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not url:
        try:
            url = (SECRETS_DIR / "supabase-mission-control-url").read_text().strip()
        except OSError:
            url = ""
    if not key:
        try:
            key = (SECRETS_DIR / "supabase-mission-control-service-key").read_text().strip()
        except OSError:
            key = ""
    return (url or None), (key or None)


async def _get(path: str, *, timeout_s: float = 5.0) -> Any:
    url, key = _creds()
    if not url or not key:
        return {"error": "supabase_not_configured"}
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }
    full = f"{url.rstrip('/')}{path}"
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            r = await client.get(full, headers=headers)
            if r.status_code >= 400:
                log.warning("supabase %s -> %s %s", path, r.status_code, r.text[:200])
                return {"error": f"http_{r.status_code}"}
            return r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("supabase %s failed: %s", path, e)
        return {"error": "network"}


# ---- public tool functions ----

async def list_open_loops(limit: int = 50) -> list[dict] | dict:
    """All cards not in `done`/`archived`, priority-ordered.

    Shape matches the dossier `open_loops` schema:
      [{id, title, column, priority, tags}]
    """
    rows = await _get(
        f"/rest/v1/{CARDS_TABLE}"
        f"?select={_CARD_SELECT}"
        f"&order=priority.asc.nullsfirst,position.asc"
        f"&limit={int(limit)}"
    )
    if isinstance(rows, dict) and rows.get("error"):
        return rows
    if not isinstance(rows, list):
        return {"error": "unexpected_response"}
    out: list[dict] = []
    for r in rows:
        col = r.get("column_id") or ""
        if col in _CLOSED_COLUMNS:
            continue
        out.append({
            "id": r.get("id"),
            "title": r.get("title"),
            "column": col,
            "priority": r.get("priority") or "normal",
            "tags": r.get("tags") or [],
        })
    return out


async def lookup_open_loop(id: str) -> dict:  # noqa: A002 - matches tool schema arg name
    """Single-card lookup by ID.

    Returns the canonical `{title, column, status, last_touched, summary}`
    shape Phase B specifies. `status` is derived (open/done/archived).
    """
    loop_id = id
    if not loop_id or "/" in loop_id or "?" in loop_id:
        return {"error": "bad_id"}
    rows = await _get(
        f"/rest/v1/{CARDS_TABLE}"
        f"?select={_CARD_SELECT}"
        f"&id=eq.{loop_id}"
        f"&limit=1"
    )
    if isinstance(rows, dict) and rows.get("error"):
        return rows
    if not isinstance(rows, list) or not rows:
        return {"error": "not_found"}
    r = rows[0]
    col = r.get("column_id") or ""
    if r.get("archived_at"):
        status = "archived"
    elif r.get("completed_at") or col == "done":
        status = "done"
    else:
        status = "open"
    summary_parts = [s for s in [r.get("description"), r.get("extended_context")] if s]
    summary = " — ".join(summary_parts)[:1000]
    return {
        "id": r.get("id"),
        "title": r.get("title"),
        "column": col,
        "status": status,
        "last_touched": r.get("updated_at") or r.get("created_at"),
        "summary": summary,
        "priority": r.get("priority") or "normal",
        "tags": r.get("tags") or [],
    }


async def mission_control_card(id: str) -> dict:  # noqa: A002
    """Fuller view of a card: same as lookup_open_loop plus raw description/context.

    Mostly identical today; kept separate so future enrichment (journal links,
    related cards) can extend it without churning the open-loop tool.
    """
    base = await lookup_open_loop(id=id)
    if base.get("error"):
        return base
    rows = await _get(
        f"/rest/v1/{CARDS_TABLE}"
        f"?select=description,extended_context,position,created_at"
        f"&id=eq.{id}"
        f"&limit=1"
    )
    if isinstance(rows, list) and rows:
        r = rows[0]
        base["fields"] = {
            "description": r.get("description") or "",
            "extended_context": r.get("extended_context") or "",
            "position": r.get("position"),
            "created_at": r.get("created_at"),
        }
    return base


async def recent_decisions(days: int = 7, limit: int = 25) -> list[dict] | dict:
    """Recent journal entries of type=action with status=closed.

    Shape: [{date, decision, context}] — directly usable in the dossier and as
    the `recent_decisions` tool return.
    """
    days = max(1, min(int(days), 30))
    rows = await _get(
        f"/rest/v1/{JOURNAL_TABLE}"
        f"?select=timestamp,summary,detail,type,status"
        f"&type=eq.action"
        f"&order=timestamp.desc"
        f"&limit={int(limit)}"
    )
    if isinstance(rows, dict) and rows.get("error"):
        return rows
    if not isinstance(rows, list):
        return {"error": "unexpected_response"}
    import datetime as _dt
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)
    out: list[dict] = []
    for r in rows:
        ts = r.get("timestamp") or ""
        try:
            when = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when < cutoff:
            continue
        out.append({
            "date": when.date().isoformat(),
            "decision": (r.get("summary") or "").strip(),
            "context": (r.get("detail") or "")[:300].strip(),
        })
    return out
