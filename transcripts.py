"""Per-call transcript persistence + post-call ingestion into agent memory.

On call end the browser POSTs the full transcript (list of {role, text, ts}
entries) to /api/end. We:
  1. Write it to ${TRANSCRIPT_DIR}/<conv_id>.json (default ./transcripts).
  2. Send a one-paragraph summary back to the same agent session via
     /v1/chat/completions so the agent can absorb the call into memory.
  3. (Optional) Post a scannable headline + full transcript thread to a
     private Slack channel for searchable scrollback.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import httpx

log = logging.getLogger("ttyc.transcripts")

TRANSCRIPT_DIR = Path(os.getenv("TRANSCRIPT_DIR", "./transcripts")).expanduser()


def _platform_for_role(role: str) -> str:
    """Derive the originating surface from an entry role.

    Roles already encode platform; this helper centralizes the mapping so
    consumers don't reimplement the convention.
    """
    if role in ("user", "assistant"):
        return "voice"
    if role in ("user_text", "assistant_text"):
        return "text"
    if role in ("tool_question", "tool_answer"):
        return "tool"
    return "system"


def _session_platform(entries: List[Dict[str, Any]]) -> str:
    seen = {_platform_for_role(e.get("role", "")) for e in entries}
    has_voice = "voice" in seen
    has_text = "text" in seen
    if has_voice and has_text:
        return "mixed"
    if has_voice:
        return "voice"
    if has_text:
        return "text"
    return "unknown"


def write_transcript(
    conv_id: str,
    started_at: float,
    entries: List[Dict[str, Any]],
    *,
    metrics: Dict[str, Any] | None = None,
) -> Path:
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPT_DIR / f"{conv_id}.json"
    payload: Dict[str, Any] = {
        "conv_id": conv_id,
        "started_at": started_at,
        "ended_at": time.time(),
        "entries": entries,
    }
    if metrics:
        payload["metrics"] = metrics
    path.write_text(json.dumps(payload, indent=2))
    log.info("wrote transcript %s (%d entries)", path, len(entries))
    return path


async def ingest_into_agent(
    conv_id: str,
    entries: List[Dict[str, Any]],
    *,
    agent_base: str,
    agent_key: str,
) -> None:
    """Send a closing message into the call's agent session so it lives in memory."""
    if not entries or not agent_key:
        return
    lines = []
    for e in entries:
        role = e.get("role", "?")
        text = (e.get("text", "") or "").strip()
        if text:
            lines.append(f"{role}: {text}")
    started_at = entries[0].get("ts") if entries else None
    ended_at = entries[-1].get("ts") if entries else None
    duration_s = int(ended_at - started_at) if started_at and ended_at else 0
    started_iso = (
        datetime.fromtimestamp(started_at).astimezone().isoformat(timespec="seconds")
        if started_at
        else "unknown"
    )
    metadata_header = (
        f"[platform={_session_platform(entries)} conv_id={conv_id} "
        f"started={started_iso} duration={duration_s}s turns={len(entries)}]"
    )
    body = (
        f"{metadata_header}\n"
        "Voice call just ended. Here is the transcript - please save the gist "
        "to memory in your usual brief style; no need to reply at length.\n\n"
        + "\n".join(lines)
    )
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{agent_base}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {agent_key}",
                    "X-Session-Id": f"voice-{conv_id}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "agent",
                    "messages": [{"role": "user", "content": body}],
                },
            )
            if r.status_code >= 300:
                log.warning("ingestion non-2xx: %s %s", r.status_code, r.text[:200])
    except Exception as e:  # noqa: BLE001
        log.warning("ingestion failed: %s", e)


SLACK_CHUNK_CHARS = 3500


def _latency_stats(entries: List[Dict[str, Any]]) -> str:
    """Return ' - avg Ns - max Ns' if any assistant entries carry latencyMs, else ''."""
    samples = [
        e["latencyMs"] for e in entries
        if isinstance(e.get("latencyMs"), (int, float)) and e["latencyMs"] > 0
    ]
    if not samples:
        return ""
    avg_s = round(sum(samples) / len(samples) / 1000)
    max_s = round(max(samples) / 1000)
    return f" - avg {avg_s}s - max {max_s}s"


def _build_headline(started_at: float, ended_at: float, entries: List[Dict[str, Any]]) -> str:
    when = datetime.fromtimestamp(started_at).astimezone().strftime("%Y-%m-%d %H:%M")
    duration_s = max(0, int(ended_at - started_at))
    duration = f"{duration_s // 60}:{duration_s % 60:02d}"
    first = ""
    for e in entries:
        if e.get("role") in ("user", "user_text"):
            first = (e.get("text") or "").strip()
            if first:
                break
    if len(first) > 140:
        first = first[:137] + "..."
    head = (
        f":telephone_receiver: Voice call - {when} - {duration} - "
        f"{len(entries)} turns{_latency_stats(entries)}"
    )
    if first:
        head += f"\nFirst: “{first}”"
    return head


def _chunk_transcript(entries: List[Dict[str, Any]]) -> List[str]:
    lines = []
    for e in entries:
        role = e.get("role", "?")
        text = (e.get("text", "") or "").strip()
        if text:
            lines.append(f"[{role}] {text}")
    chunks: List[str] = []
    current = ""
    for line in lines:
        if current and len(current) + len(line) + 1 > SLACK_CHUNK_CHARS:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


async def post_to_slack(
    conv_id: str,
    started_at: float,
    ended_at: float,
    entries: List[Dict[str, Any]],
    *,
    slack_token: str,
    channel_id: str,
) -> None:
    """Post the call as a thread to a private Slack channel.

    Top message: scannable headline (now includes ask_agent latency stats
    when client transcripts carry latencyMs). Thread replies: full
    role-prefixed transcript, chunked on entry boundaries. Fire-and-forget.

    Mid-call notifications for slow ask_agent turns are intentionally not
    sent - they'd be noisy and the end-of-call summary is sufficient.
    """
    if not entries:
        return
    headline = _build_headline(started_at, ended_at, entries)
    body_chunks = _chunk_transcript(entries)
    headers = {
        "Authorization": f"Bearer {slack_token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://slack.com/api/chat.postMessage",
                headers=headers,
                json={
                    "channel": channel_id,
                    "text": headline,
                    "unfurl_links": False,
                    "unfurl_media": False,
                },
            )
            data = r.json() if r.status_code < 500 else {}
            if not data.get("ok"):
                log.warning("slack top-message failed [%s]: %s", conv_id, data.get("error") or r.text[:200])
                return
            thread_ts = data["ts"]
            for chunk in body_chunks:
                rr = await client.post(
                    "https://slack.com/api/chat.postMessage",
                    headers=headers,
                    json={
                        "channel": channel_id,
                        "thread_ts": thread_ts,
                        "text": f"```\n{chunk}\n```",
                        "unfurl_links": False,
                        "unfurl_media": False,
                    },
                )
                rdata = rr.json() if rr.status_code < 500 else {}
                if not rdata.get("ok"):
                    log.warning("slack thread reply failed [%s]: %s", conv_id, rdata.get("error") or rr.text[:200])
                    return
            log.info("posted call %s to slack (%d chunks)", conv_id, len(body_chunks))
    except Exception as e:  # noqa: BLE001
        log.warning("slack post failed: %s", e)
