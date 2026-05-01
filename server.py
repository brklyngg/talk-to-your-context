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

# Active conversations: conv_id -> {started_at: float, entries: [...]}
CONVERSATIONS: Dict[str, Dict[str, Any]] = {}

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

# Customize this for your agent. The default below is intentionally generic;
# it tells the realtime model how to behave on a phone-call-tempo voice line
# and when to delegate to ask_agent vs. handle small talk itself.
VOICE_SYSTEM_PROMPT = """\
You are a live voice assistant. Your voice is the OpenAI realtime model.
Your *brain* is consulted via the ask_agent tool, which routes to a backend
agent loop with full memory, skills, and external integrations.

CRITICAL TOOL-CALL RULES:
1. For ANY substantive question - calendar, email, memory, projects, drafting,
   research, anything beyond pure small talk - call ask_agent.
2. When you call ask_agent, DO NOT speak. No bridging phrases like "let me
   check" or "one moment" or "okay". Stay completely silent. The user will
   hear a low ambient hum during the tool call (a brown-noise icebreaker idle).
3. After the tool result arrives, speak the answer naturally and briefly.
   Phone-call tempo: <=2 sentences per turn unless asked for more. Numbers
   spoken naturally ("ten thirty", not "10:30 colon zero zero"). No markdown.
4. If you didn't call ask_agent and the user asks something substantive,
   you'll be wrong. Trust the brain. Default to consulting.

PERSONA:
Warm, dry-witted, and concise. Sound like a sharp colleague who doesn't waste
time. Avoid corporate hedging. Don't say "I can help you with that" - just help.
"""


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
        "model": OPENAI_REALTIME_MODEL,
        "voice": OPENAI_REALTIME_VOICE,
        "openai_key": bool(OPENAI_API_KEY),
    })


async def session_mint(request: web.Request) -> web.Response:
    if not OPENAI_API_KEY:
        return web.json_response({"error": "no_openai_key"}, status=500)
    conv_id = _new_conv_id()
    CONVERSATIONS[conv_id] = {"started_at": time.time(), "entries": []}
    body = {
        "model": OPENAI_REALTIME_MODEL,
        "voice": OPENAI_REALTIME_VOICE,
        "modalities": ["audio", "text"],
        "instructions": VOICE_SYSTEM_PROMPT,
        "tools": [ASK_AGENT_TOOL_SCHEMA],
        "tool_choice": "auto",
        "turn_detection": {"type": "server_vad", "threshold": 0.5, "silence_duration_ms": 500},
        "input_audio_format": "pcm16",
        "output_audio_format": "pcm16",
        "input_audio_transcription": {"model": "whisper-1"},
        # Per-response output ceiling. Session-level acts as default for every
        # response.create unless overridden. ~1500 tokens ~= 30s of speech.
        "max_response_output_tokens": 1500,
    }
    try:
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
            data = r.json()
    except httpx.HTTPStatusError as e:
        log.error("ephemeral mint failed: %s %s", e.response.status_code, e.response.text[:300])
        return web.json_response({"error": "ephemeral_mint_failed", "detail": e.response.text[:300]}, status=502)
    except Exception as e:  # noqa: BLE001
        log.exception("ephemeral mint exception")
        return web.json_response({"error": "ephemeral_mint_exception", "detail": str(e)}, status=502)
    return web.json_response({"conv_id": conv_id, "session": data})


async def ask_agent(request: web.Request) -> web.Response:
    body = await request.json()
    conv_id = body.get("conv_id")
    question = (body.get("question") or "").strip()
    if not conv_id or conv_id not in CONVERSATIONS:
        return web.json_response({"error": "unknown_conv_id"}, status=400)
    if not question:
        return web.json_response({"answer": "(empty question)"}, status=200)
    log.info("ask_agent [%s]: %s", conv_id, question[:120])
    try:
        answer = await _agent_chat(conv_id, question)
    except httpx.TimeoutException:
        answer = "I'm taking longer than expected - try again or rephrase."
    except Exception as e:  # noqa: BLE001
        log.exception("ask_agent failed")
        answer = f"I'm having trouble reaching the agent - {type(e).__name__}. Try again in a moment."
    # Record both turns into the transcript
    CONVERSATIONS[conv_id]["entries"].append({"role": "tool_question", "text": question, "ts": time.time()})
    CONVERSATIONS[conv_id]["entries"].append({"role": "tool_answer", "text": answer, "ts": time.time()})
    return web.json_response({"answer": answer})


async def text_turn(request: web.Request) -> web.Response:
    """Dual-mode: pure agent turn (no realtime model). Slower but full agent voice."""
    body = await request.json()
    conv_id = body.get("conv_id")
    user_text = (body.get("text") or "").strip()
    if not conv_id or conv_id not in CONVERSATIONS:
        return web.json_response({"error": "unknown_conv_id"}, status=400)
    if not user_text:
        return web.json_response({"answer": ""}, status=200)
    try:
        answer = await _agent_chat(conv_id, user_text)
    except Exception as e:  # noqa: BLE001
        log.exception("text_turn failed")
        return web.json_response({"error": str(e)}, status=502)
    CONVERSATIONS[conv_id]["entries"].append({"role": "user_text", "text": user_text, "ts": time.time()})
    CONVERSATIONS[conv_id]["entries"].append({"role": "assistant_text", "text": answer, "ts": time.time()})
    return web.json_response({"answer": answer})


async def end_call(request: web.Request) -> web.Response:
    # Idempotent: browser may call this from visibilitychange, pagehide, AND
    # the explicit End button - all on the same conv_id. Already-ended is fine.
    try:
        body = await request.json()
    except Exception:
        body = {}
    conv_id = body.get("conv_id")
    client_entries = body.get("entries") or []
    if not conv_id:
        return web.json_response({"ok": True, "noop": "no_conv_id"})
    conv = CONVERSATIONS.pop(conv_id, None)
    if conv is None:
        return web.json_response({"ok": True, "noop": "already_ended"})
    # Merge browser-captured transcript with server-side tool turns
    all_entries = sorted(conv["entries"] + client_entries, key=lambda e: e.get("ts", 0))
    path = write_transcript(conv_id, conv["started_at"], all_entries)
    ended_at = time.time()
    # Fire-and-forget memory ingestion
    asyncio.create_task(ingest_into_agent(
        conv_id, all_entries,
        agent_base=AGENT_API_BASE,
        agent_key=AGENT_API_KEY,
    ))
    if SLACK_BOT_TOKEN and SLACK_CALL_CHANNEL_ID:
        asyncio.create_task(post_to_slack(
            conv_id=conv_id,
            started_at=conv["started_at"],
            ended_at=ended_at,
            entries=all_entries,
            slack_token=SLACK_BOT_TOKEN,
            channel_id=SLACK_CALL_CHANNEL_ID,
        ))
    return web.json_response({"ok": True, "transcript": str(path)})


# ---- session reaper --------------------------------------------------------

# OpenAI Realtime auto-closes sessions at ~60min; reap server-side state shortly
# after so abandoned tabs don't leak entries forever. Also surfaces how often
# abandonment happens via INFO logs.
REAPER_INTERVAL_SEC = 300
REAPER_MAX_AGE_SEC = 65 * 60


async def _reap_stale_conversations() -> None:
    while True:
        await asyncio.sleep(REAPER_INTERVAL_SEC)
        cutoff = time.time() - REAPER_MAX_AGE_SEC
        stale = [cid for cid, c in CONVERSATIONS.items() if c.get("started_at", 0) < cutoff]
        for cid in stale:
            CONVERSATIONS.pop(cid, None)
            log.info("reaped stale conversation %s (>65min, no /api/end)", cid)


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

def make_app() -> web.Application:
    app = web.Application(middlewares=[tailnet_middleware], client_max_size=8 * 1024 * 1024)
    app.on_startup.append(_start_reaper)
    app.on_cleanup.append(_stop_reaper)
    app.router.add_get("/api/health", health)
    app.router.add_post("/api/session", session_mint)
    app.router.add_post("/api/ask-agent", ask_agent)
    app.router.add_post("/api/text-turn", text_turn)
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
