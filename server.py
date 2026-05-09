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
from events import log_call_event, compute_routing_metrics  # noqa: E402
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
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2")
OPENAI_REALTIME_VOICE = os.getenv("OPENAI_REALTIME_VOICE", "alloy")
AGENT_API_BASE = os.getenv("AGENT_API_BASE", "http://127.0.0.1:8642").rstrip("/")
# Total timeout cap on a single ask_agent forward to the backend. The model's
# native preambles + async function calling fill any in-call wait; this only
# bounds the absolute worst case (Hermes hung, network wedged, etc.).
ASK_AGENT_TIMEOUT_SEC = float(os.getenv("ASK_AGENT_TIMEOUT_SEC", "90"))
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
#   entries: [...],            # transcript items (user/assistant/tool_question/tool_answer)
#   handoff_note: str?,        # ≤80-word continuation note from a dying session
# }
CONVERSATIONS: Dict[str, Dict[str, Any]] = {}

# Idle-reap window. Backgrounding no longer ends a call, so we use this as
# the cost guard instead of the prior 65-min started_at ceiling.
REAPER_INTERVAL_SEC = 60
REAPER_IDLE_TIMEOUT_SEC = 20 * 60


def _touch(conv: Dict[str, Any]) -> None:
    conv["last_activity_ts"] = time.time()


def _ensure_conv_shape(conv: Dict[str, Any]) -> None:
    """Backfill optional keys so existing in-memory conversations from older
    server versions don't KeyError after a code update."""
    conv.setdefault("entries", [])
    conv.setdefault("last_activity_ts", conv.get("started_at", time.time()))


ASK_AGENT_TOOL_SCHEMA = {
    "type": "function",
    "name": "ask_agent",
    "description": (
        "Consult the backend agent for context you don't already have from "
        "this call: external/current facts (calendar, email, files, web), "
        "cross-session memory, verification, drafting, or novel reasoning. "
        "Do NOT call for clarifications, recaps, comparisons, or refinements "
        "of context already in this call - answer those directly."
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
            },
            "intent_type": {
                "type": "string",
                "enum": ["lookup", "action", "drafting", "reasoning", "verification", "other"],
                "description": (
                    "Why you are escalating: lookup (memory/calendar/email/files), "
                    "action (the user wants something done), drafting (write/compose), "
                    "reasoning (novel multi-step thinking), verification (confirm a "
                    "fact's freshness or current state), other."
                ),
            },
            "freshness_required": {
                "type": "boolean",
                "description": (
                    "True if the answer must reflect the current external state "
                    "(today's calendar, latest email, current file contents, etc.). "
                    "False if a recent cached answer would be acceptable."
                ),
            },
        },
        "required": ["question", "intent_type", "freshness_required"],
    },
}

# Optional deployment-specific facts the realtime model can rely on without
# guessing. Inject things like "your filesystem-write tools are real" or
# "your logs live at <path>" so the model stops hallucinating refusals about
# its own capabilities. Leave empty for the generic public scaffold.
AGENT_DEPLOYMENT_NOTE = os.getenv("AGENT_DEPLOYMENT_NOTE", "").strip()

# Tool-call routing prompt. Tightened May 2026 for gpt-realtime-2 GA: the
# model has GPT-5-class reasoning and 128K context, plus native preambles
# ("let me check that") and async function calling that keep the conversation
# flowing during tool waits — so the prior hand-coded bridging rules are gone,
# replaced by a stronger bias toward in-session answers and a sharper anti-
# fabrication line.
_BASE_PROMPT = """\
You are a live voice assistant with access to the user's deep context via
the ask_agent tool, which routes to a backend agent loop with the user's
full memory, skills, calendar, email, files, and external integrations.

CRITICAL TOOL-CALL RULES:
1. Prefer answering from in-session context. If the user's turn can be
   fully answered from a prior ask_agent answer in this same call, or is a
   clarification, recap, comparison, refinement, or follow-up reasoning on
   context already established here, answer directly. Trust your reasoning;
   prior tool answers are in your context window — use them.
2. Call ask_agent ONLY when the answer requires something you do NOT already
   have: external/current facts (calendar, email, files, web), cross-session
   memory ("what did we say last time"), verification of freshness, drafting
   that needs the user's durable voice, novel reasoning that benefits from
   agent skills, or any user-specific fact not yet established here.
3. ANTI-FABRICATION: never invent user-specific facts. If you are unsure
   whether your in-session context actually covers a name, date, file,
   commitment, or decision the user is asking about, escalate via ask_agent
   rather than guess. A clean "I'd need to check" is better than a confident
   wrong answer; a tool call is better still.

ANSWER-QUALITY RULES:
4. If you don't have the info or aren't confident, say so directly. "I don't
   have that" or "I'd need to check" beats a guess.
5. Don't synthesize beyond what ask_agent returned. If the agent said it
   doesn't know, say you don't know. Don't fill gaps.
6. If ask_agent returns a list (meetings, emails, files, options), enumerate
   briefly first — "You have three: A, B, and C" — then synthesize. Don't
   blend distinct items into one fuzzy summary.

VERIFIED COMPLETION RULES:
7. Don't say "done", "sent", "created", "updated", or "saved" unless
   ask_agent's answer included a concrete handle — a file path, ID, link,
   or timestamp confirming the action. If the agent queued or drafted
   something, say "I asked for it" or "drafted", not "done".

STYLE RULES:
8. Lead with the answer — the number, the time, the yes/no, the decision.
   Reasons after, only if asked or load-bearing.
9. Phone-call tempo: ≤2 sentences per turn unless asked for more. Numbers
   spoken naturally ("ten thirty", not "10:30 colon zero zero").

TOOL-FAILURE HONESTY:
10. If ask_agent's answer starts with "Agent is temporarily unreachable" or
    is empty, say so plainly and ask the user to repeat. Never invent.

CAPABILITIES (TRUTH — DO NOT CONTRADICT):
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


async def _agent_chat_inner(conv_id: str, user_text: str) -> str:
    """Forward a single user turn to the backend agent and return the full text.

    The backend speaks chat-completions SSE; we consume the stream and assemble
    the answer. Total wall time is capped by ``_agent_chat`` via asyncio.wait_for,
    so this inner does not need its own total-timeout guard.
    """
    if not AGENT_API_KEY:
        raise RuntimeError("Agent API key not configured")
    # Per-chunk read timeout is loose because asyncio.wait_for caps total time.
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
    return "".join(chunks)


async def _agent_chat(conv_id: str, user_text: str) -> str:
    """Cap the inner forward at ASK_AGENT_TIMEOUT_SEC. Raises asyncio.TimeoutError
    on overrun — caller maps that to the structured-unreachable sentinel."""
    return await asyncio.wait_for(
        _agent_chat_inner(conv_id, user_text),
        timeout=ASK_AGENT_TIMEOUT_SEC,
    )


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


async def _mint_realtime_session(instructions_suffix: str | None = None) -> dict:
    """Mint an OpenAI Realtime ephemeral session. Raises on HTTP/network error.

    ``instructions_suffix`` is appended to the base ``VOICE_SYSTEM_PROMPT`` —
    used on resume to splice in a handoff-note continuation directive without
    requiring a follow-up session.update from the client.
    """
    instructions = VOICE_SYSTEM_PROMPT
    if instructions_suffix:
        instructions = instructions + "\n\n" + instructions_suffix
    body = {
        "model": OPENAI_REALTIME_MODEL,
        "voice": OPENAI_REALTIME_VOICE,
        "modalities": ["audio", "text"],
        "instructions": instructions,
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


async def ask_agent(request: web.Request) -> web.Response:
    """Synchronous forward to the backend agent. Returns the answer inline.

    gpt-realtime-2 has GPT-5-class reasoning and native preambles + async
    function calling, so the prior fast-path / long-poll / task-id machinery
    is gone — the model itself bridges the wait by speaking to the user.
    Total wait is capped at ``ASK_AGENT_TIMEOUT_SEC``; ``intent_type`` and
    ``freshness_required`` are logged for routing telemetry but not branched on.
    """
    body = await request.json()
    conv_id = body.get("conv_id")
    question = (body.get("question") or "").strip()
    intent_type = (body.get("intent_type") or "unknown").strip() or "unknown"
    freshness_required = bool(body.get("freshness_required"))
    conv = CONVERSATIONS.get(conv_id) if conv_id else None
    if conv is None:
        return web.json_response({"error": "unknown_conv_id"}, status=400)
    _ensure_conv_shape(conv)
    if not question:
        return web.json_response({"answer": "(empty question)"}, status=200)

    _touch(conv)
    started_at = time.time()
    prior_ask_count = sum(1 for e in conv["entries"] if e.get("role") == "tool_question")
    log.info("ask_agent [%s] (%s, fresh=%s): %s",
             conv_id, intent_type, freshness_required, question[:120])
    log_call_event(
        LOG_DIR, conv_id, "ask_agent_spawned",
        chars=len(question),
        intent_type=intent_type, freshness_required=freshness_required,
        prior_ask_count=prior_ask_count,
    )
    error_type: str | None = None
    try:
        answer = await _agent_chat(conv_id, question)
        status = "done"
    except asyncio.TimeoutError:
        answer = _structured_unreachable("timeout")["answer"]
        status = "error"
        error_type = "timeout"
    except httpx.TimeoutException:
        answer = _structured_unreachable("timeout")["answer"]
        status = "error"
        error_type = "timeout"
    except Exception as e:  # noqa: BLE001
        log.exception("ask_agent failed")
        answer = _structured_unreachable(type(e).__name__)["answer"]
        status = "error"
        error_type = type(e).__name__
    finished_at = time.time()
    latency_ms = int((finished_at - started_at) * 1000)
    log_call_event(
        LOG_DIR, conv_id, "ask_agent_done",
        latency_ms=latency_ms, chars=len(answer),
        status=status, error_type=error_type,
    )
    # Persist the question/answer pair to the transcript even if the conv was
    # popped mid-call by /api/end (the dict survives via this closure ref).
    conv["entries"].append({"role": "tool_question", "text": question, "ts": started_at})
    conv["entries"].append({"role": "tool_answer", "text": answer, "ts": finished_at})
    conv["last_activity_ts"] = finished_at
    return web.json_response({"answer": answer, "status": status})


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
    """Mint a fresh Realtime ephemeral session bound to the same conv.

    Continuity is delivered via the freshly-minted session's ``instructions``
    (the handoff-note continuation suffix is baked in server-side). 128K
    context + GPT-5-class reasoning means we don't need a recent-entries
    replay primer; the dying session's handoff note is the sole continuity
    signal.
    """
    body = await request.json()
    conv_id = body.get("conv_id")
    conv = CONVERSATIONS.get(conv_id) if conv_id else None
    if conv is None:
        return web.json_response({"expired": True})
    _ensure_conv_shape(conv)
    if not OPENAI_API_KEY:
        return web.json_response({"error": "no_openai_key"}, status=500)
    note = (conv.get("handoff_note") or "").strip()
    suffix = None
    if note:
        suffix = (
            "Continuation context from earlier in this same call: "
            + note
            + "\n\nCritical: do not greet, do not acknowledge any pause, do "
            + "not say 'as I was saying' or similar filler. Continue exactly "
            + "where you left off in topic and tone. Wait for the user's next "
            + "utterance before responding."
        )
    try:
        data = await _mint_realtime_session(instructions_suffix=suffix)
    except httpx.HTTPStatusError as e:
        log.error("resume mint failed: %s %s", e.response.status_code, e.response.text[:300])
        return web.json_response({"error": "ephemeral_mint_failed", "detail": e.response.text[:300]}, status=502)
    except Exception as e:  # noqa: BLE001
        log.exception("resume mint exception")
        return web.json_response({"error": "ephemeral_mint_exception", "detail": str(e)}, status=502)
    _touch(conv)
    log_call_event(
        LOG_DIR, conv_id, "resumed",
        handoff_note_chars=len(note),
    )
    return web.json_response({
        "conv_id": conv_id,
        "session": data,
        "resumed": True,
        "handoff_note": note or None,
    })


async def premint_session(request: web.Request) -> web.Response:
    """Pre-mint a fresh ephemeral for an existing conv ahead of the 60-min cap.

    Does NOT touch conv state, does NOT mark any agent task delivered. The
    actual swap-in fires a regular /api/resume call to claim missed work.
    """
    body = await request.json()
    conv_id = body.get("conv_id")
    conv = CONVERSATIONS.get(conv_id) if conv_id else None
    if conv is None:
        return web.json_response({"expired": True})
    if not OPENAI_API_KEY:
        return web.json_response({"error": "no_openai_key"}, status=500)
    try:
        data = await _mint_realtime_session()
    except httpx.HTTPStatusError as e:
        log.error("premint mint failed: %s %s", e.response.status_code, e.response.text[:300])
        return web.json_response({"error": "ephemeral_mint_failed", "detail": e.response.text[:300]}, status=502)
    except Exception as e:  # noqa: BLE001
        log.exception("premint mint exception")
        return web.json_response({"error": "ephemeral_mint_exception", "detail": str(e)}, status=502)
    log_call_event(LOG_DIR, conv_id, "session_preminted")
    return web.json_response({"conv_id": conv_id, "session": data})


async def handoff_note(request: web.Request) -> web.Response:
    """Persist the dying Realtime session's continuation note onto the conv,
    so the next resume can splice it into the new session's instructions."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad_json"}, status=400)
    conv_id = body.get("conv_id")
    note = (body.get("note") or "").strip()
    generated_in_ms = body.get("generated_in_ms")
    conv = CONVERSATIONS.get(conv_id) if conv_id else None
    if conv is None:
        return web.json_response({"error": "no_conv"}, status=404)
    if note:
        conv["handoff_note"] = note
        conv["handoff_note_ts"] = time.time()
        log_call_event(
            LOG_DIR, conv_id, "handoff_note_saved",
            chars=len(note), generated_in_ms=generated_in_ms,
        )
    return web.json_response({"ok": True})


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
    all_entries = sorted(conv["entries"] + client_entries, key=lambda e: e.get("ts", 0))
    metrics = compute_routing_metrics(LOG_DIR, conv_id)
    path = write_transcript(conv_id, conv["started_at"], all_entries, metrics=metrics)
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
        slack_posted=slack_posted,
        ask_agent_count=metrics["ask_agent_count"],
        local_answer_turns=metrics["local_answer_turns"],
        ask_ratio=metrics["ask_ratio"],
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
            ended_at = time.time()
            all_entries = sorted(conv.get("entries", []), key=lambda e: e.get("ts", 0))
            metrics = compute_routing_metrics(LOG_DIR, cid)
            try:
                write_transcript(cid, conv["started_at"], all_entries, metrics=metrics)
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
                slack_posted=slack_posted,
                ask_agent_count=metrics["ask_agent_count"],
                local_answer_turns=metrics["local_answer_turns"],
                ask_ratio=metrics["ask_ratio"],
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
    app.router.add_post("/api/text-turn", text_turn)
    app.router.add_post("/api/resume", resume_call)
    app.router.add_post("/api/premint-session", premint_session)
    app.router.add_post("/api/handoff-note", handoff_note)
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
