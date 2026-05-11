"""Session dossier: today's facts + last call's working state.

Loaded into every Realtime session's instructions at mint time so the voice
model has substantive context from second 1 — no "generic until deep dive"
tax for routine turns.

Two write paths:

1. `refresh_dossier()` — periodic regeneration of today's facts (open loops,
   calendar, recent decisions, hot people, last handoff). Triggered on
   server boot and after each call ends.

2. `extract_working_state(conv_id, entries)` — post-call extraction of what
   we just decided/committed/learned, merged into the dossier's
   `working_state_from_last_call` so the next session starts smarter.

The shape is a single JSON file at `DOSSIER_PATH`; `render_markdown()`
turns it into the suffix appended to `VOICE_SYSTEM_PROMPT`. Same file,
two consumers (render for prompt, structured for tools later in Phase E).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("ttyc.dossier")

DOSSIER_PATH = Path(
    os.getenv("DOSSIER_PATH", "~/.hermes/dossier/today.json")
).expanduser()

# Refresh is a no-op if the on-disk dossier is younger than this. Tuned so the
# startup refresh and the post-call refresh don't both fire when calls are
# rapid; force=True bypasses.
DOSSIER_REFRESH_MIN_AGE_SEC = 60 * 30  # 30 min

# Bounded lengths in working_state_from_last_call so the prompt suffix stays
# small. Older items roll off the head.
WORKING_STATE_KEEP = {
    "decisions": 10,
    "commitments": 10,
    "open_questions": 8,
    "deltas": 8,
}

# Dossier counts older than this are considered stale at mint time and skipped.
# Local-date match (today only) — calendars and open loops are date-sensitive.
# A session minted at 11pm using yesterday's dossier would show wrong "today".
DOSSIER_TZ = os.getenv("DOSSIER_TZ", "America/New_York")


def _today_local() -> date:
    """Local-date for the configured TZ. Used to gate "is the dossier today's?"."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(DOSSIER_TZ)).date()
    except Exception:  # noqa: BLE001
        return datetime.now().date()


def _generated_local_date(generated_at: float) -> date:
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(generated_at, ZoneInfo(DOSSIER_TZ)).date()
    except Exception:  # noqa: BLE001
        return datetime.fromtimestamp(generated_at).date()


def _empty_dossier() -> dict:
    return {
        "generated_at": 0.0,
        "tz": DOSSIER_TZ,
        "open_loops": [],
        "calendar_today": [],
        "recent_decisions": [],
        "hot_people": [],
        "last_handoff_summary": "",
        "working_state_from_last_call": {
            "decisions": [],
            "commitments": [],
            "open_questions": [],
            "deltas": [],
        },
    }


def _read_disk() -> dict | None:
    """Read the on-disk dossier. Never raises."""
    try:
        if not DOSSIER_PATH.exists():
            return None
        return json.loads(DOSSIER_PATH.read_text())
    except Exception:  # noqa: BLE001
        log.exception("dossier read failed")
        return None


def _write_atomic(payload: dict) -> None:
    DOSSIER_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DOSSIER_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    os.replace(tmp, DOSSIER_PATH)


def _age_hours(generated_at: float) -> float:
    return max(0.0, (time.time() - generated_at) / 3600.0)


def render_markdown(d: dict | None) -> str:
    """Return the prompt-suffix markdown for a dossier dict, or '' if empty.

    Kept compact (target ≤2K tokens). Sections are clearly labeled so the
    model can address them — e.g. "per your dossier §open_loops".
    """
    if not d:
        return ""
    parts: list[str] = []
    parts.append("DOSSIER (today's standing context — address directly; don't re-fetch what's here):")

    def _bullet_lines(items: list[dict], render: callable) -> list[str]:
        out = []
        for it in items[:25]:
            try:
                line = render(it)
            except Exception:
                continue
            if line:
                out.append(f"- {line}")
        return out

    open_loops = d.get("open_loops") or []
    if open_loops:
        parts.append("\n§open_loops:")
        parts.extend(_bullet_lines(open_loops, lambda x: (
            f"`{x.get('id','?')}` [{x.get('column','?')}/{x.get('priority','normal')}] "
            f"{x.get('title','(untitled)')}"
        )))

    cal = d.get("calendar_today") or []
    if cal:
        parts.append("\n§calendar_today:")
        parts.extend(_bullet_lines(cal, lambda x: (
            f"{x.get('start','?')}–{x.get('end','?')}: {x.get('title','(untitled)')}"
            + (f" — with {', '.join(x['attendees'])}" if x.get("attendees") else "")
        )))

    recent = d.get("recent_decisions") or []
    if recent:
        parts.append("\n§recent_decisions (last 7 days):")
        parts.extend(_bullet_lines(recent, lambda x: (
            f"{x.get('date','?')}: {x.get('decision','?')}"
            + (f" ({x['context']})" if x.get("context") else "")
        )))

    hot = d.get("hot_people") or []
    if hot:
        parts.append("\n§hot_people:")
        parts.extend(_bullet_lines(hot, lambda x: (
            f"{x.get('name','?')}"
            + (f" ({x['role']})" if x.get("role") else "")
            + (f" — {x['recent_context']}" if x.get("recent_context") else "")
        )))

    handoff = (d.get("last_handoff_summary") or "").strip()
    if handoff:
        parts.append("\n§last_handoff_summary:")
        parts.append(handoff)

    ws = d.get("working_state_from_last_call") or {}
    ws_parts: list[str] = []
    for key in ("decisions", "commitments", "open_questions", "deltas"):
        items = ws.get(key) or []
        if items:
            ws_parts.append(f"  {key}:")
            for it in items[:WORKING_STATE_KEEP[key]]:
                if isinstance(it, dict):
                    text = it.get("text") or it.get("summary") or json.dumps(it)
                else:
                    text = str(it)
                ws_parts.append(f"    - {text}")
    if ws_parts:
        parts.append("\n§working_state_from_last_call (carry forward from your previous session):")
        parts.extend(ws_parts)

    return "\n".join(parts)


def load_dossier() -> tuple[str | None, dict]:
    """Return (markdown_suffix_or_None, meta).

    `markdown_suffix_or_None` is the rendered prompt suffix if the dossier
    is from today's local date; None otherwise (so mint can fall back to a
    plain session). `meta` always returns the audit fields the caller logs:
    `loaded` (bool), `chars` (int), `age_h` (float|None),
    `working_state_present` (bool).
    """
    d = _read_disk()
    meta: dict[str, Any] = {
        "loaded": False, "chars": 0, "age_h": None, "working_state_present": False,
    }
    if not d or not d.get("generated_at"):
        return None, meta
    gen_at = float(d["generated_at"])
    meta["age_h"] = round(_age_hours(gen_at), 2)
    if _generated_local_date(gen_at) != _today_local():
        return None, meta
    suffix = render_markdown(d)
    if not suffix:
        return None, meta
    ws = d.get("working_state_from_last_call") or {}
    meta["loaded"] = True
    meta["chars"] = len(suffix)
    meta["working_state_present"] = any(
        ws.get(k) for k in ("decisions", "commitments", "open_questions", "deltas")
    )
    return suffix, meta


# ---- agent call (internal; keeps dossier independent of server module) ----

async def _agent_call_json(prompt: str, *, agent_base: str, agent_key: str,
                           timeout_s: float = 90.0) -> dict | None:
    """Call the agent backend, ask for JSON-only output, parse it.

    Returns the parsed dict on success, None on any failure (network, parse,
    auth, etc.). Never raises — caller treats None as "skip this refresh."
    """
    if not agent_key:
        log.warning("dossier: no agent key — skipping refresh")
        return None
    timeout = httpx.Timeout(connect=10.0, read=timeout_s, write=10.0, pool=10.0)
    chunks: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST", f"{agent_base}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {agent_key}",
                    "X-Session-Id": "dossier",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
                json={
                    "model": "agent",
                    "stream": True,
                    "messages": [{"role": "user", "content": prompt}],
                },
            ) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    payload = line[len("data: "):]
                    if payload == "[DONE]":
                        break
                    try:
                        evt = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    delta = (evt.get("choices") or [{}])[0].get("delta") or {}
                    piece = delta.get("content") or ""
                    if piece:
                        chunks.append(piece)
    except Exception as e:  # noqa: BLE001
        log.warning("dossier agent call failed: %s", e)
        return None
    text = "".join(chunks).strip()
    return _extract_json_object(text)


def _extract_json_object(text: str) -> dict | None:
    """Best-effort: pull the first balanced top-level JSON object out of `text`.

    Agent answers sometimes wrap JSON in fences or preamble prose; this scans
    for the first `{`, matches braces (respecting strings), and parses.
    """
    if not text:
        return None
    # Strip common fences.
    if "```" in text:
        # take content between the first ```... and the next ```
        try:
            after = text.split("```", 1)[1]
            if after.startswith("json"):
                after = after[4:]
            text = after.split("```", 1)[0]
        except IndexError:
            pass
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


# ---- public refresh / extract ----

DOSSIER_REFRESH_PROMPT = """\
Generate Gary's standing-context dossier as a single JSON object. Today is {today_iso}, TZ {tz}.

Return ONLY valid JSON matching this schema (no prose, no markdown fences):

{{
  "open_loops": [{{"id": str, "title": str, "column": str, "priority": "high"|"medium"|"normal"|"low"}}],
  "calendar_today": [{{"start": "HH:MM", "end": "HH:MM", "title": str, "attendees": [str]}}],
  "recent_decisions": [{{"date": "YYYY-MM-DD", "decision": str, "context": str}}],
  "hot_people": [{{"name": str, "role": str, "recent_context": str}}],
  "last_handoff_summary": str
}}

Source data:
- open_loops: live Mission Control cards not in done/archived columns. Use the real card UUIDs as `id`.
- calendar_today: today's events from Gary's primary calendar.
- recent_decisions: last 7 days of meaningful decisions from memory/journals.
- hot_people: people Gary has been actively engaged with this week (e.g., investors, clients, key collaborators).
- last_handoff_summary: ≤80 words on where the last voice call left off (continuation note).

Caps: ≤25 open_loops (priority-ordered), ≤10 calendar events, ≤8 recent_decisions, ≤6 hot_people.
If a section has no data, return an empty list/string. Do not omit keys.
"""


async def refresh_dossier(*, agent_base: str, agent_key: str,
                          force: bool = False) -> dict | None:
    """Regenerate the dossier from the agent backend.

    No-op if disk dossier is younger than DOSSIER_REFRESH_MIN_AGE_SEC unless
    `force=True`. Preserves existing `working_state_from_last_call` (that key
    is owned by `extract_working_state`, not this function).

    Returns the new dossier dict on success, None on failure.
    """
    existing = _read_disk() or _empty_dossier()
    gen_at = float(existing.get("generated_at") or 0)
    if not force and gen_at and (time.time() - gen_at) < DOSSIER_REFRESH_MIN_AGE_SEC:
        log.info("dossier: skipping refresh (age %.1fh < %.1fh threshold)",
                 _age_hours(gen_at), DOSSIER_REFRESH_MIN_AGE_SEC / 3600)
        return existing
    today = _today_local().isoformat()
    prompt = DOSSIER_REFRESH_PROMPT.format(today_iso=today, tz=DOSSIER_TZ)
    data = await _agent_call_json(prompt, agent_base=agent_base, agent_key=agent_key)
    if data is None:
        log.warning("dossier refresh: agent returned no parseable JSON")
        return None
    out = _empty_dossier()
    out["generated_at"] = time.time()
    out["tz"] = DOSSIER_TZ
    for key in ("open_loops", "calendar_today", "recent_decisions", "hot_people"):
        v = data.get(key)
        if isinstance(v, list):
            out[key] = v
    handoff = data.get("last_handoff_summary")
    if isinstance(handoff, str):
        out["last_handoff_summary"] = handoff.strip()
    # Preserve working_state_from_last_call across refresh — owned by extractor.
    out["working_state_from_last_call"] = (
        existing.get("working_state_from_last_call") or out["working_state_from_last_call"]
    )
    _write_atomic(out)
    log.info("dossier: refreshed (open_loops=%d calendar=%d decisions=%d people=%d)",
             len(out["open_loops"]), len(out["calendar_today"]),
             len(out["recent_decisions"]), len(out["hot_people"]))
    return out


EXTRACT_WORKING_STATE_PROMPT = """\
Extract Gary's working state from the voice-call transcript below as a single JSON object.

Return ONLY valid JSON matching this schema (no prose, no markdown fences):

{{
  "decisions": [{{"text": str}}],
  "commitments": [{{"text": str}}],
  "open_questions": [{{"text": str}}],
  "deltas": [{{"text": str}}]
}}

Definitions:
- decisions: concrete things Gary decided during this call ("we're shipping X by Friday", "kill the Y feature").
- commitments: things Gary or the assistant committed to doing next ("draft the email", "call Pat tomorrow").
- deltas: changes in Gary's mental model — beliefs updated, assumptions overturned, plans revised.
- open_questions: things explicitly left unresolved or flagged for later.

Caps: ≤10 decisions, ≤10 commitments, ≤8 deltas, ≤8 open_questions.
Each item ≤200 chars. If a section has no entries, return [].
Skip greetings, small talk, tool dumps, and procedural chatter — only signal worth carrying forward.

Transcript:
{transcript}
"""


def _format_transcript_for_extraction(entries: list[dict]) -> str:
    lines: list[str] = []
    for e in entries:
        role = e.get("role", "?")
        if role in ("tool_question", "tool_answer"):
            # Skip the raw tool dumps; the assistant's surrounding speech carries
            # the signal. Tool answers can be 5K-token blobs that would push out
            # the rest of the transcript under the agent's context budget.
            continue
        text = (e.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"{role}: {text}")
    return "\n".join(lines)


async def extract_working_state(conv_id: str, entries: list[dict], *,
                                agent_base: str, agent_key: str) -> dict | None:
    """Read the just-ended call's transcript, extract working state, merge into dossier.

    Fires async from end_call and the reaper — never blocks the live path.
    Returns the updated `working_state_from_last_call` dict on success, None
    on failure (logged but not raised).
    """
    if not entries:
        return None
    transcript = _format_transcript_for_extraction(entries)
    if not transcript.strip():
        return None
    prompt = EXTRACT_WORKING_STATE_PROMPT.format(transcript=transcript)
    data = await _agent_call_json(prompt, agent_base=agent_base, agent_key=agent_key)
    if data is None:
        log.warning("dossier: working-state extraction returned no parseable JSON (conv=%s)", conv_id)
        return None
    new_ws = {"decisions": [], "commitments": [], "open_questions": [], "deltas": []}
    for key in new_ws:
        v = data.get(key)
        if isinstance(v, list):
            new_ws[key] = v[: WORKING_STATE_KEEP[key]]
    existing = _read_disk() or _empty_dossier()
    existing["working_state_from_last_call"] = new_ws
    # Touch generated_at so a stale dossier doesn't suddenly look fresh just
    # because we wrote working_state. Only refresh_dossier moves generated_at.
    _write_atomic(existing)
    log.info("dossier: working_state updated for conv=%s (d=%d c=%d q=%d Δ=%d)",
             conv_id, len(new_ws["decisions"]), len(new_ws["commitments"]),
             len(new_ws["open_questions"]), len(new_ws["deltas"]))
    return new_ws


# ---- fire-and-forget convenience ----

def schedule_refresh(*, agent_base: str, agent_key: str, force: bool = False) -> None:
    """Spawn a refresh_dossier task on the current event loop. Never raises."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        return
    loop.create_task(refresh_dossier(agent_base=agent_base, agent_key=agent_key, force=force))


def schedule_extract(conv_id: str, entries: list[dict], *,
                     agent_base: str, agent_key: str,
                     then_refresh: bool = True) -> None:
    """Spawn the post-call extractor; optionally chain a dossier refresh.

    The chain matters: refresh-then-mint would race with extract-then-merge.
    Sequencing extract → refresh ensures the next session sees both updates.
    """
    async def _run():
        await extract_working_state(conv_id, entries, agent_base=agent_base, agent_key=agent_key)
        if then_refresh:
            await refresh_dossier(agent_base=agent_base, agent_key=agent_key, force=True)
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        return
    loop.create_task(_run())
