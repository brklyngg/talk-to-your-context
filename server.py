"""Talk to Your Context — local voice sidecar.

Browser <-> OpenAI Realtime (WebRTC, direct) <-> your agent (via this proxy).

Endpoints:
  GET  /                   - UI
  GET  /static/*           - assets
  GET  /api/health         - open (for launchd / monitoring)
  POST /api/session        - mints OpenAI Realtime ephemeral key
  POST /api/ask-agent      - proxies a single user turn to your agent
  POST /api/text-turn      - same as ask-agent but framed as the dual-mode
                             text channel (UI shows in the agent-text bubble)
  POST /api/end            - persists transcript + ingests into agent memory
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any, Dict

import httpx
from aiohttp import web
from dotenv import load_dotenv

# Load env from sibling .env then process env (process env wins for OPENAI_API_KEY)
HERE = Path(__file__).parent
load_dotenv(HERE / ".env")

from auth import tailnet_middleware  # noqa: E402
from events import log_call_event  # noqa: E402
from transcripts import write_transcript, ingest_into_agent, post_to_slack  # noqa: E402

LOG_DIR = HERE / "logs"
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "server.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("ttyc.server")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime")
OPENAI_REALTIME_VOICE = os.getenv("OPENAI_REALTIME_VOICE", "alloy")
AGENT_API_BASE = os.getenv("AGENT_API_BASE", "http://127.0.0.1:8642").rstrip("/")
ASK_AGENT_IDLE_TIMEOUT = float(os.getenv("ASK_AGENT_IDLE_TIMEOUT", "45"))
HOST = os.getenv("VOICE_HOST", "127.0.0.1")
PORT = int(os.getenv("VOICE_PORT", "8090"))
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "").strip()
SLACK_CALL_CHANNEL_ID = os.environ.get("SLACK_CALL_CHANNEL_ID", "").strip()

# Agent API key. Prefer env var; fall back to a sibling file (chmod 600) for
# operators who'd rather not put a long-lived key in their shell rc.
AGENT_API_KEY = os.environ.get("AGENT_API_KEY", "").strip()
if not AGENT_API_KEY:
    _key_file = HERE / ".agent-api-key"
    if _key_file.exists():
        AGENT_API_KEY = _key_file.read_text().strip()

if not AGENT_API_KEY:
    log.warning("AGENT_API_KEY not set - /api/ask-agent will 500")
if not OPENAI_API_KEY:
    log.warning("OPENAI_API_KEY not in env - /api/session will 500")

# Active conversations: conv_id -> {
#   started_at: float, last_activity_ts: float,
#   entries: [...],
#   agent_tasks: {task_id: {question, status, answer, started_at,
#                           finished_at, realtime_call_id,
#                           delivered_to_realtime: bool, task: asyncio.Task}}
# }
CONVERSATIONS: Dict[str, Dict[str, Any]] = {}

# How long /api/agent-task long-polls before returning (client retries).
# Bounded below the typical mobile-network/CDN idle timeout.
AGENT_TASK_LONG_POLL_SEC = 25.0
# Inline fast-path: if the agent answers within this many seconds, return it
# in the /api/ask-agent response so the browser never has to long-poll.
ASK_AGENT_FAST_PATH_SEC = 1.5
# Cap concurrent in-flight agent tasks per conversation. Realtime model can
# parallel-call -- bound it so a misbehaving session can't flood.
MAX_CONCURRENT_AGENT_TASKS = 3
# Idle-reap window. Backgrounding no longer ends a call, so we use this as
# the cost guard instead of the prior 65-min started_at ceiling.
REAPER_INTERVAL_SEC = 60
REAPER_IDLE_TIMEOUT_SEC = 20 * 60


def _new_task_id() -> str:
    return secrets.token_urlsafe(9)


def _touch(conv: Dict[str, Any]) -> None:
    conv["last_activity_ts"] = time.time()


def _ensure_conv_shape(conv: Dict[str, Any]) -> None:
    """Backfill optional keys so existing in-memory conversations from older
    server versions don't KeyError after a code update."""
    conv.setdefault("entries", [])
    conv.setdefault("agent_tasks", {})
    conv.setdefault("last_activity_ts", conv.get("started_at", time.time()))

ASK_AGENT_TOOL_SCHEMA = {
    "type": "function",
    "name": "ask_agent",
    "description": (
        "Consult your agent for memory, calendar, email, projects, drafting, "
        "research, or any substantive question. Use for everything that isn't "
        "small talk."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "The question or request to send to the agent, in full natural "
                    "language. Include relevant context from the conversation."
                ),
            }
        },
        "required": ["question"],
    },
}

# Optional deployment-specific facts the realtime model can rely on without
# guessing. Inject things like "your filesystem-write tools are real" or
# "your logs live at <path>" so the model stops hallucinating refusals about
# its own capabilities. Leave empty for the generic public scaffold.
AGENT_DEPLOYMENT_NOTE = os.getenv("AGENT_DEPLOYMENT_NOTE", "").strip()

# Customize this for your agent. Rules 5-9 are quality scaffolding kept from
# the prior revision. Rule 2 changed in May 2026: gpt-realtime now handles
# asynchronous function calls natively, so the prior "stay silent" rule was
# actively suppressing a paid-for capability.
_BASE_PROMPT = """\
You are a live voice assistant. Your voice is the OpenAI realtime model.
Your *brain* is consulted via the ask_agent tool, which routes to a backend
agent loop with full memory, skills, and external integrations.

CRITICAL TOOL-CALL RULES:
1. For ANY substantive question - calendar, email, memory, projects, drafting,
   research, anything beyond pure small talk - call ask_agent.
2. During an ask_agent call you may say one short bridging line ("one sec",
   "looking now") if the wait is going long. If the wait exceeds ~25 seconds,
   drop another brief "still working on it" beat every 20-30 seconds so the
   user knows you haven't dropped. Never invent the answer while waiting.
   Short interjections are fine; running narration is not.
3. After the tool result arrives, speak the answer naturally and briefly.
   Phone-call tempo: <=2 sentences per turn unless asked for more. Numbers
   spoken naturally ("ten thirty", not "10:30 colon zero zero"). No markdown.
4. If you didn't call ask_agent and the user asks something substantive,
   you'll be wrong. Trust the brain. Default to consulting.

ANSWER-QUALITY RULES:
5. If you don't have the info or aren't confident, say so directly. "I don't
   have that" or "I'd need to check" beats a guess. A clear no is more useful
   than a wrong yes.
6. Don't synthesize beyond what ask_agent returned. If the agent said it
   doesn't know, say you don't know. Don't fill gaps.
7. If ask_agent returns a list (meetings, emails, files, options), enumerate
   it briefly first - "You have three: A, B, and C" - then synthesize. Don't
   blend distinct items into one fuzzy summary.

VERIFIED COMPLETION RULES:
8. Don't say "done", "sent", "created", "updated", or "saved" unless
   ask_agent's answer included a concrete handle - a file path, ID, link,
   or timestamp confirming the action. If the agent said it queued or drafted
   something, say "I asked for it" or "drafted", not "done".

STYLE RULES:
9. Lead with the answer - the number, the time, the yes/no, the decision.
   Reasons after, only if asked or load-bearing.

TOOL-FAILURE HONESTY:
10. If ask_agent's answer starts with "Agent is temporarily unreachable" or
    is empty, say so plainly and ask the user to repeat. Never invent.

CAPABILITIES (TRUTH - DO NOT CONTRADICT):
- Questions about your model, voice, logs, file capabilities, or where data
  lives must be answered via ask_agent. The agent knows its own setup; you
  do not. Do not improvise refusals like "I don't have access to that".

LANGUAGE:
- Always speak English unless the user explicitly switches mid-conversation.
"""

VOICE_SYSTEM_PROMPT = (
    _BASE_PROMPT
    + (("\nDEPLOYMENT NOTE:\n" + AGENT_DEPLOYMENT_NOTE + "\n") if AGENT_DEPLOYMENT_NOTE else "")
    + """
PERSONA:
Warm, dry-witted, and concise. Sound like a sharp colleague who doesn't waste
time. Avoid corporate hedging. Don't say "I can help you with that" - just help.
"""
)


# ---- helpers ---------------------------------------------------------------

def _new_conv_id() -> str:
    return secrets.token_urlsafe(12)


async def _iter_sse_with_idle_watchdog(response: "httpx.Response", idle_timeout: float):
    """Yield non-empty SSE lines; raise asyncio.TimeoutError on idle stalls.

    A streaming chat-completions endpoint typically emits content deltas,
    progress events, and `: keepalive` comment frames during long agent
    loops, so any idle period longer than the keepalive cadence (~30s)
    indicates a stuck server, not a slow operation.
    """
    aiter = response.aiter_lines()
    while True:
        try:
            line = await asyncio.wait_for(aiter.__anext__(), timeout=idle_timeout)
        except StopAsyncIteration:
            return
        if line:
            yield line


async def _agent_chat(conv_id: str, user_text: str) -> str:
    if not AGENT_API_KEY:
        raise RuntimeError("Agent API key not configured")
    # SSE streaming: connection stays alive for any agent-loop duration via
    # the backend's keepalive frames. We bound only idle gaps, not total
    # duration, since legitimate calls span 4-180s depending on which
    # skills the agent invokes.
    timeout = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
    chunks: list[str] = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST",
            f"{AGENT_API_BASE}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {AGENT_API_KEY}",
                "X-Session-Id": f"voice-{conv_id}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            json={
                "model": "agent",
                "stream": True,
                "messages": [{"role": "user", "content": user_text}],
            },
        ) as r:
            r.raise_for_status()
            async for line in _iter_sse_with_idle_watchdog(r, ASK_AGENT_IDLE_TIMEOUT):
                if not line.startswith("data: "):
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
    return "".join(chunks)


# ---- routes ----------------------------------------------------------------

async def health(request: web.Request) -> web.Response:
    agent_ok = False
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            r = await client.get(f"{AGENT_API_BASE}/health")
            agent_ok = r.status_code == 200
    except Exception:
        pass
    return web.json_response({
        "ok": True,
        "agent": agent_ok,
        # Back-compat for older Hermes-named PWA clients still cached on
        # devices. The pre-rename app.js looks for `hermes` here.
        "hermes": agent_ok,
        "model": OPENAI_REALTIME_MODEL,
        "voice": OPENAI_REALTIME_VOICE,
        "openai_key": bool(OPENAI_API_KEY),
    })


async def _mint_realtime_session() -> dict:
    """Mint an OpenAI Realtime ephemeral session. Raises on HTTP/network error."""
    body = {
        "model": OPENAI_REALTIME_MODEL,
        "voice": OPENAI_REALTIME_VOICE,
        "modalities": ["audio", "text"],
        "instructions": VOICE_SYSTEM_PROMPT,
        "tools": [ASK_AGENT_TOOL_SCHEMA],
        "tool_choice": "auto",
        "turn_detection": {
            "type": "semantic_vad",
            "eagerness": "low",
            "create_response": True,
            "interrupt_response": True,
        },
        "input_audio_noise_reduction": {"type": "near_field"},
        "input_audio_format": "pcm16",
        "output_audio_format": "pcm16",
        "input_audio_transcription": {
            "model": "gpt-4o-mini-transcribe",
            "language": "en",
            "prompt": (
                "Gary Gurevich, Crunchy Numbers, Crunchy Tools, Flowocity, Hermes, "
                "Jerome, Claude, OpenClaw, Paperclip, Rillet, Pat Leahy, "
                "Y Combinator, Obsidian, Supabase, Vercel, Tailscale, fractional CFO, "
                "P&L, GL, RFS, AI-native agency."
            ),
        },
        "max_response_output_tokens": 1500,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            "https://api.openai.com/v1/realtime/sessions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
                "OpenAI-Beta": "realtime=v1",
            },
            json=body,
        )
        r.raise_for_status()
        return r.json()


async def session_mint(request: web.Request) -> web.Response:
    if not OPENAI_API_KEY:
        return web.json_response({"error": "no_openai_key"}, status=500)
    conv_id = _new_conv_id()
    now = time.time()
    CONVERSATIONS[conv_id] = {
        "started_at": now,
        "last_activity_ts": now,
        "entries": [],
        "agent_tasks": {},
    }
    try:
        data = await _mint_realtime_session()
    except httpx.HTTPStatusError as e:
        log.error("ephemeral mint failed: %s %s", e.response.status_code, e.response.text[:300])
        return web.json_response({"error": "ephemeral_mint_failed", "detail": e.response.text[:300]}, status=502)
    except Exception as e:  # noqa: BLE001
        log.exception("ephemeral mint exception")
        return web.json_response({"error": "ephemeral_mint_exception", "detail": str(e)}, status=502)
    log_call_event(LOG_DIR, conv_id, "session_minted", model=OPENAI_REALTIME_MODEL, voice=OPENAI_REALTIME_VOICE)
    return web.json_response({"conv_id": conv_id, "session": data})


_AGENT_UNREACHABLE_SENTINEL = "Agent is temporarily unreachable"


def _structured_unreachable(error_type: str) -> dict:
    """Tool-result envelope the realtime model recognizes as a clean failure.

    System prompt rule #10 keys off the leading sentinel, so the realtime
    model says "the agent didn't answer, please repeat" instead of inventing
    a refusal.
    """
    return {
        "answer": f"{_AGENT_UNREACHABLE_SENTINEL} - please ask the user to repeat that.",
        "_error": error_type,
    }


async def _run_agent_task(conv_id: str, task_id: str) -> None:
    """Run the agent SSE stream as a server-side task. Survives client disconnect.

    Updates ``conv["agent_tasks"][task_id]`` with status/answer; touches
    ``last_activity_ts`` on completion so a long deep-context call doesn't
    get reaped while productive work is happening. Records the transcript
    pair via the local ``conv`` reference -- safe even if /api/end pops the
    conv mid-stream (the dict object survives via this function's closure).
    """
    conv = CONVERSATIONS.get(conv_id)
    if conv is None:
        return
    task_state = conv["agent_tasks"].get(task_id)
    if task_state is None:
        return
    question = task_state["question"]
    try:
        answer = await _agent_chat(conv_id, question)
        status = "done"
        error_type = None
    except asyncio.CancelledError:
        task_state["status"] = "cancelled"
        task_state["finished_at"] = time.time()
        log_call_event(LOG_DIR, conv_id, "ask_agent_cancelled", task_id=task_id)
        raise
    except httpx.TimeoutException:
        answer = _structured_unreachable("timeout")["answer"]
        status = "error"
        error_type = "timeout"
    except Exception as e:  # noqa: BLE001
        log.exception("agent task failed")
        answer = _structured_unreachable(type(e).__name__)["answer"]
        status = "error"
        error_type = type(e).__name__
    finished_at = time.time()
    task_state["answer"] = answer
    task_state["status"] = status
    task_state["finished_at"] = finished_at
    latency_ms = int((finished_at - task_state["started_at"]) * 1000)
    log_call_event(
        LOG_DIR, conv_id, "ask_agent_done",
        task_id=task_id, latency_ms=latency_ms, chars=len(answer),
        status=status, error_type=error_type,
    )
    # Belt-and-suspenders: append to the LOCAL conv ref. Even if /api/end
    # popped the conv from CONVERSATIONS during the SSE stream, the dict is
    # still alive via this closure -- no KeyError.
    conv["entries"].append({"role": "tool_question", "text": question, "ts": task_state["started_at"]})
    conv["entries"].append({"role": "tool_answer", "text": answer, "ts": finished_at})
    # Touch activity so deep work doesn't get reaped while it's actually running.
    conv["last_activity_ts"] = finished_at
    # If the conv was already popped (rare: user hit End mid-stream), surface
    # the late turn forensically.
    if CONVERSATIONS.get(conv_id) is None:
        log_call_event(LOG_DIR, conv_id, "late_turn", task_id=task_id, chars=len(answer))


async def ask_agent(request: web.Request) -> web.Response:
    """Spawn an agent task; return inline if the fast-path completes in time.

    Otherwise return ``{task_id, status: "running"}`` and let the client
    long-poll ``/api/agent-task/<task_id>``. The task survives client
    disconnect (Gary backgrounds the iPhone, the work keeps going).
    """
    body = await request.json()
    conv_id = body.get("conv_id")
    question = (body.get("question") or "").strip()
    realtime_call_id = body.get("call_id")
    conv = CONVERSATIONS.get(conv_id) if conv_id else None
    if conv is None:
        return web.json_response({"error": "unknown_conv_id"}, status=400)
    _ensure_conv_shape(conv)
    if not question:
        return web.json_response({"answer": "(empty question)"}, status=200)

    in_flight = sum(1 for t in conv["agent_tasks"].values() if t["status"] == "running")
    if in_flight >= MAX_CONCURRENT_AGENT_TASKS:
        log_call_event(LOG_DIR, conv_id, "tool_error", error_type="too_many_in_flight", in_flight=in_flight)
        return web.json_response({**_structured_unreachable("too_many_in_flight"), "task_id": None}, status=200)

    _touch(conv)
    task_id = _new_task_id()
    log.info("ask_agent [%s/%s]: %s", conv_id, task_id, question[:120])
    log_call_event(LOG_DIR, conv_id, "ask_agent_spawned", task_id=task_id, chars=len(question))
    state = {
        "task_id": task_id,
        "question": question,
        "status": "running",
        "answer": None,
        "started_at": time.time(),
        "finished_at": None,
        "realtime_call_id": realtime_call_id,
        "delivered_to_realtime": False,
        "task": None,
    }
    conv["agent_tasks"][task_id] = state
    coro = _run_agent_task(conv_id, task_id)
    task = asyncio.create_task(coro)
    state["task"] = task

    # Fast path: wait briefly for completion so chitchat-grade calls don't
    # round-trip through /api/agent-task.
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=ASK_AGENT_FAST_PATH_SEC)
    except asyncio.TimeoutError:
        return web.json_response({"task_id": task_id, "status": "running"})
    except asyncio.CancelledError:
        # The task itself was cancelled (e.g., conv ended). Surface as such.
        return web.json_response({"task_id": task_id, "status": "cancelled", "answer": ""})
    return web.json_response({
        "task_id": task_id,
        "status": state["status"],
        "answer": state.get("answer") or "",
    })


async def agent_task(request: web.Request) -> web.Response:
    """Long-poll endpoint for a server-side agent task.

    Returns when status != running, or after AGENT_TASK_LONG_POLL_SEC --
    whichever comes first. The client retries until status is final.
    """
    task_id = request.match_info["task_id"]
    conv_id = request.query.get("conv_id")
    conv = CONVERSATIONS.get(conv_id) if conv_id else None
    if conv is None:
        return web.json_response({"error": "unknown_conv_id"}, status=400)
    _ensure_conv_shape(conv)
    state = conv["agent_tasks"].get(task_id)
    if state is None:
        return web.json_response({"error": "unknown_task_id", "status": "unknown"}, status=404)
    task = state.get("task")
    if task is not None and not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=AGENT_TASK_LONG_POLL_SEC)
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            pass
    if state["status"] == "running":
        return web.json_response({"task_id": task_id, "status": "running"})
    # Mark delivered; idempotent for duplicate pollers.
    already = state["delivered_to_realtime"]
    state["delivered_to_realtime"] = True
    if not already:
        log_call_event(LOG_DIR, conv_id, "ask_agent_delivered_to_realtime", task_id=task_id)
    return web.json_response({
        "task_id": task_id,
        "status": state["status"],
        "answer": state.get("answer") or "",
        "already_delivered": already,
    })


async def text_turn(request: web.Request) -> web.Response:
    """Dual-mode: pure agent turn (no realtime model). Slower but full agent voice."""
    body = await request.json()
    conv_id = body.get("conv_id")
    user_text = (body.get("text") or "").strip()
    conv = CONVERSATIONS.get(conv_id) if conv_id else None
    if conv is None:
        return web.json_response({"error": "unknown_conv_id"}, status=400)
    _ensure_conv_shape(conv)
    if not user_text:
        return web.json_response({"answer": ""}, status=200)
    _touch(conv)
    started_at = time.time()
    try:
        answer = await _agent_chat(conv_id, user_text)
    except Exception as e:  # noqa: BLE001
        log.exception("text_turn failed")
        log_call_event(LOG_DIR, conv_id, "tool_error", channel="text_turn", error_type=type(e).__name__)
        return web.json_response(_structured_unreachable(type(e).__name__), status=200)
    finished_at = time.time()
    conv["entries"].append({"role": "user_text", "text": user_text, "ts": started_at})
    conv["entries"].append({"role": "assistant_text", "text": answer, "ts": finished_at})
    conv["last_activity_ts"] = finished_at
    log_call_event(
        LOG_DIR, conv_id, "text_turn",
        latency_ms=int((finished_at - started_at) * 1000), chars=len(answer),
    )
    return web.json_response({"answer": answer})


async def resume_call(request: web.Request) -> web.Response:
    """Mint a fresh OpenAI Realtime ephemeral session bound to the same conv.

    Returns answers the user missed while away (``completed_while_away``) so
    the new realtime session can speak them, plus pointers to any tasks
    still running (``still_working``) so the client can re-attach long polls.
    """
    body = await request.json()
    conv_id = body.get("conv_id")
    conv = CONVERSATIONS.get(conv_id) if conv_id else None
    if conv is None:
        return web.json_response({"expired": True})
    _ensure_conv_shape(conv)
    if not OPENAI_API_KEY:
        return web.json_response({"error": "no_openai_key"}, status=500)
    try:
        data = await _mint_realtime_session()
    except httpx.HTTPStatusError as e:
        log.error("resume mint failed: %s %s", e.response.status_code, e.response.text[:300])
        return web.json_response({"error": "ephemeral_mint_failed", "detail": e.response.text[:300]}, status=502)
    except Exception as e:  # noqa: BLE001
        log.exception("resume mint exception")
        return web.json_response({"error": "ephemeral_mint_exception", "detail": str(e)}, status=502)
    _touch(conv)
    completed_while_away: list[dict] = []
    still_working: list[dict] = []
    now = time.time()
    for tid, state in list(conv["agent_tasks"].items()):
        if state["status"] in ("done", "error") and not state["delivered_to_realtime"]:
            completed_while_away.append({
                "task_id": tid,
                "question": state["question"],
                "answer": state.get("answer") or "",
            })
            state["delivered_to_realtime"] = True
            log_call_event(LOG_DIR, conv_id, "ask_agent_carried_over_on_resume", task_id=tid)
        elif state["status"] == "running":
            still_working.append({
                "task_id": tid,
                "question": state["question"],
                "elapsed_s": int(now - state["started_at"]),
            })
    log_call_event(
        LOG_DIR, conv_id, "resumed",
        completed=len(completed_while_away), still_working=len(still_working),
    )
    return web.json_response({
        "conv_id": conv_id,
        "session": data,
        "resumed": True,
        "completed_while_away": completed_while_away,
        "still_working": still_working,
    })


def _cancel_in_flight_tasks(conv: dict) -> int:
    n = 0
    for tid, state in list(conv.get("agent_tasks", {}).items()):
        task = state.get("task")
        if task and not task.done():
            task.cancel()
            n += 1
    return n


async def end_call(request: web.Request) -> web.Response:
    """Explicit End. Backgrounding NO LONGER triggers this -- only the End
    button or pagehide. Idempotent for duplicate fires."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    conv_id = body.get("conv_id")
    client_entries = body.get("entries") or []
    reason = body.get("reason") or "user_click"
    if not conv_id:
        return web.json_response({"ok": True, "noop": "no_conv_id"})
    # Legacy clients may still POST with reason="visibilitychange" -- treat as a no-op
    # so old PWA tabs don't accidentally end calls during background.
    if reason == "visibilitychange":
        log_call_event(LOG_DIR, conv_id, "legacy_background_end_ignored")
        return web.json_response({"ok": True, "noop": "background_is_pause"})
    conv = CONVERSATIONS.pop(conv_id, None)
    if conv is None:
        return web.json_response({"ok": True, "noop": "already_ended"})
    cancelled = _cancel_in_flight_tasks(conv)
    all_entries = sorted(conv["entries"] + client_entries, key=lambda e: e.get("ts", 0))
    path = write_transcript(conv_id, conv["started_at"], all_entries)
    ended_at = time.time()
    asyncio.create_task(ingest_into_agent(
        conv_id, all_entries,
        agent_base=AGENT_API_BASE,
        agent_key=AGENT_API_KEY,
    ))
    slack_posted = False
    if SLACK_BOT_TOKEN and SLACK_CALL_CHANNEL_ID:
        asyncio.create_task(post_to_slack(
            conv_id=conv_id,
            started_at=conv["started_at"],
            ended_at=ended_at,
            entries=all_entries,
            slack_token=SLACK_BOT_TOKEN,
            channel_id=SLACK_CALL_CHANNEL_ID,
        ))
        slack_posted = True
    log_call_event(
        LOG_DIR, conv_id, "ended",
        reason=reason, duration_s=int(ended_at - conv["started_at"]),
        cancelled_tasks=cancelled, slack_posted=slack_posted,
    )
    return web.json_response({"ok": True, "transcript": str(path)})


async def client_event(request: web.Request) -> web.Response:
    """Sink for browser-side telemetry. Writes to the per-call NDJSON via the
    same log_call_event used by server-side events, so one timeline per call.
    Fire-and-forget from the client; never blocks audio/tool flow.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return web.Response(status=204)
    conv_id = (body.get("conv_id") or "unknown").strip() or "unknown"
    event = (body.get("event") or "client_unknown").strip() or "client_unknown"
    payload = {k: v for k, v in body.items() if k not in ("conv_id", "event")}
    if conv_id != "unknown" and conv_id not in CONVERSATIONS:
        payload["conv_status"] = "unknown_conv"
    log_call_event(LOG_DIR, conv_id, event, **payload)
    return web.Response(status=204)


# ---- session reaper --------------------------------------------------------
# Backgrounding is now a pause, not an end. The reaper guards against the
# truly-forgotten case: 20 min with zero activity (no /api/session,
# /api/ask-agent, /api/text-turn, /api/resume, or completed agent task).


async def _reap_stale_conversations() -> None:
    while True:
        await asyncio.sleep(REAPER_INTERVAL_SEC)
        cutoff = time.time() - REAPER_IDLE_TIMEOUT_SEC
        stale = [cid for cid, c in CONVERSATIONS.items() if c.get("last_activity_ts", c.get("started_at", 0)) < cutoff]
        for cid in stale:
            conv = CONVERSATIONS.pop(cid, None)
            if conv is None:
                continue
            cancelled = _cancel_in_flight_tasks(conv)
            ended_at = time.time()
            all_entries = sorted(conv.get("entries", []), key=lambda e: e.get("ts", 0))
            try:
                write_transcript(cid, conv["started_at"], all_entries)
            except Exception:  # noqa: BLE001
                log.exception("reap: write_transcript failed for %s", cid)
            slack_posted = False
            if SLACK_BOT_TOKEN and SLACK_CALL_CHANNEL_ID:
                asyncio.create_task(post_to_slack(
                    conv_id=cid,
                    started_at=conv["started_at"],
                    ended_at=ended_at,
                    entries=all_entries,
                    slack_token=SLACK_BOT_TOKEN,
                    channel_id=SLACK_CALL_CHANNEL_ID,
                ))
                slack_posted = True
            log.info("reaped idle conversation %s (>%dmin no activity)", cid, REAPER_IDLE_TIMEOUT_SEC // 60)
            log_call_event(
                LOG_DIR, cid, "reaped",
                reason="idle_timeout", duration_s=int(ended_at - conv["started_at"]),
                cancelled_tasks=cancelled, slack_posted=slack_posted,
            )


async def _start_reaper(app: web.Application) -> None:
    app["reaper_task"] = asyncio.create_task(_reap_stale_conversations())


async def _stop_reaper(app: web.Application) -> None:
    task = app.get("reaper_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ---- app -------------------------------------------------------------------

@web.middleware
async def no_cache_static_middleware(request: web.Request, handler):
    """Force iOS PWA / Safari to revalidate the SPA shell on every load.

    Without this, the realtime tool name was cached as `ask_hermes` after the
    repo rename, breaking every voice session until the user wiped the PWA.
    """
    resp = await handler(request)
    path = request.path
    if path == "/" or path.startswith("/static/") or path.endswith(".webmanifest"):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


def make_app() -> web.Application:
    app = web.Application(middlewares=[tailnet_middleware, no_cache_static_middleware], client_max_size=8 * 1024 * 1024)
    app.on_startup.append(_start_reaper)
    app.on_cleanup.append(_stop_reaper)
    app.router.add_get("/api/health", health)
    app.router.add_post("/api/session", session_mint)
    app.router.add_post("/api/ask-agent", ask_agent)
    app.router.add_post("/api/client-event", client_event)
    app.router.add_get("/api/agent-task/{task_id}", agent_task)
    app.router.add_post("/api/text-turn", text_turn)
    app.router.add_post("/api/resume", resume_call)
    app.router.add_post("/api/end", end_call)
    # Static SPA - index.html and /static/*
    app.router.add_get("/", lambda r: web.FileResponse(HERE / "web" / "index.html"))
    app.router.add_get("/manifest.webmanifest", lambda r: web.FileResponse(HERE / "web" / "manifest.webmanifest"))
    app.router.add_static("/static", path=str(HERE / "web"), show_index=False)
    return app


def main() -> None:
    log.info("talk-to-your-context starting on %s:%s - model=%s voice=%s", HOST, PORT, OPENAI_REALTIME_MODEL, OPENAI_REALTIME_VOICE)
    web.run_app(make_app(), host=HOST, port=PORT, access_log=None)


if __name__ == "__main__":
    main()
