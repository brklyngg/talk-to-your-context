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
import sys
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
import dossier  # noqa: E402
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
# Liveness model for ask_agent forwards:
#   - ASK_AGENT_IDLE_TIMEOUT_SEC: primary watchdog, surfaced via httpx's `read`
#     timeout. Raises httpx.ReadTimeout if no SSE bytes arrive within the
#     window mid-stream — catches a hung backend without killing legitimately
#     slow streaming work.
#   - ASK_AGENT_TIMEOUT_SEC: runaway guard via asyncio.wait_for. Catches the
#     pathological "backend dribbles forever" case.
ASK_AGENT_TIMEOUT_SEC = float(os.getenv("ASK_AGENT_TIMEOUT_SEC", "600"))
ASK_AGENT_IDLE_TIMEOUT_SEC = float(os.getenv("ASK_AGENT_IDLE_TIMEOUT_SEC", "45"))
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


# Granular tool schemas. Each fans out to a direct backend (Supabase / gws /
# Obsidian fs) and returns small structured JSON — no LLM in the dispatch
# path. The model prefers these over `deep_research` for any factual lookup;
# `deep_research` is reserved for novel reasoning, drafting, or synthesis no
# narrow tool covers.
LOOKUP_OPEN_LOOP_SCHEMA = {
    "type": "function",
    "name": "lookup_open_loop",
    "description": (
        "Look up a single open loop / Mission Control card by its UUID. Prefer "
        "this over `deep_research` whenever the user references a specific loop. "
        "The dossier in your instructions lists today's open loops with IDs."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Card UUID (e.g. from §open_loops in the dossier)"},
        },
        "required": ["id"],
    },
}

RECENT_DECISIONS_SCHEMA = {
    "type": "function",
    "name": "recent_decisions",
    "description": (
        "List recent decisions / committed actions from the journal. Use for "
        "'what did we decide about X' or 'what's been going on this week.' "
        "Returns structured rows you can quote directly."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "days": {"type": "integer", "description": "Look-back window (1–30)", "default": 7},
        },
        "required": [],
    },
}

SEARCH_NOTES_SCHEMA = {
    "type": "function",
    "name": "search_notes",
    "description": (
        "Full-text search the user's Obsidian vault. Use for 'what did I write "
        "about X', 'find the note on Y', or to surface adjacent context. "
        "Returns top-k matches with snippets."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "k": {"type": "integer", "description": "Max results (1–20)", "default": 5},
        },
        "required": ["query"],
    },
}

CALENDAR_SCHEMA = {
    "type": "function",
    "name": "calendar",
    "description": (
        "Read calendar events for a window. Use for 'what's on for today', "
        "'tomorrow's meetings', or specific dates. Always prefer this over "
        "`deep_research` for scheduling questions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "when": {
                "type": "string",
                "description": (
                    "Accepted: 'today', 'tomorrow', 'this week', YYYY-MM-DD, "
                    "or 'YYYY-MM-DD/YYYY-MM-DD' for a range. Defaults to today."
                ),
            },
            "account": {
                "type": "string",
                "description": "One of: gurevich.gary@gmail.com, gary@crunchy.tools, gary@flowocity.ai. Defaults to personal.",
            },
        },
        "required": [],
    },
}

GMAIL_SEARCH_SCHEMA = {
    "type": "function",
    "name": "gmail_search",
    "description": (
        "Search Gmail with a query. Use Gmail's native search syntax "
        "(`from:foo`, `subject:bar`, `newer_than:3d`). Returns top messages "
        "with from/subject/snippet."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Gmail search syntax"},
            "account": {
                "type": "string",
                "description": "One of: gurevich.gary@gmail.com, gary@crunchy.tools, gary@flowocity.ai.",
            },
            "limit": {"type": "integer", "description": "Max results (1–25)", "default": 10},
        },
        "required": ["query"],
    },
}

MISSION_CONTROL_CARD_SCHEMA = {
    "type": "function",
    "name": "mission_control_card",
    "description": (
        "Fuller view of a Mission Control card: title, status, description, "
        "extended context, position. Use when you need more detail than "
        "`lookup_open_loop` returns."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Card UUID"},
        },
        "required": ["id"],
    },
}

DEEP_RESEARCH_TOOL_SCHEMA = {
    "type": "function",
    "name": "deep_research",
    "description": (
        "Slow agent path for anything no narrow tool covers — including ACTIONS "
        "with side effects (write/save files, draft and save emails, create "
        "calendar events, edit notes, run scripts) AND novel reasoning, "
        "drafting, or synthesis. The agent backend has full filesystem access, "
        "Gmail draft / Calendar write, and shell tools — use this tool for any "
        "user request that requires *doing* something on Gary's Mac, not just "
        "looking something up. Typically 30–240 seconds. Tell the user roughly "
        "how long ('this'll take about a minute, I'll narrate as I go'). Partial "
        "findings stream in via `[research-finding]` system messages — narrate "
        "them; don't claim completion until you receive the function_call_output. "
        "When the work includes an action, the function_call_output will include "
        "a concrete handle (file path, message id, event link) — only then say 'done'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": (
                    "Full natural-language request, with all relevant context. "
                    "For actions, state the goal AND the success criteria — e.g. "
                    "'Save this draft to ~/Desktop/foo.md and tell me the final path.' "
                    "The agent will interpret and execute; you don't need to "
                    "describe how."
                ),
            },
            "scope": {
                "type": "string",
                "enum": ["action", "drafting", "reasoning", "synthesis"],
                "description": (
                    "action: side-effecting work (filesystem, email draft, "
                    "calendar). drafting: write a document/message. reasoning: "
                    "novel multi-step thinking. synthesis: combine sources."
                ),
            },
            "expected_seconds": {
                "type": "integer",
                "description": "Your honest estimate; used for user expectations",
            },
        },
        "required": ["prompt", "scope", "expected_seconds"],
    },
}

TOOLKIT_SCHEMAS = [
    LOOKUP_OPEN_LOOP_SCHEMA,
    RECENT_DECISIONS_SCHEMA,
    SEARCH_NOTES_SCHEMA,
    CALENDAR_SCHEMA,
    GMAIL_SEARCH_SCHEMA,
    MISSION_CONTROL_CARD_SCHEMA,
    DEEP_RESEARCH_TOOL_SCHEMA,
]


TRIAGE_VERDICT_TOOL_SCHEMA = {
    "type": "function",
    "name": "triage_verdict",
    "description": (
        "Record Gary's verdict on an open loop the moment a decision is reached. "
        "DEFAULT TO 'drop' if Gary signals indifference, fatigue, or vague intent. "
        "Only use 'park' for items he explicitly defers with a reason. "
        "Only use 'act' when he commits to a concrete next step with a date/time. "
        "Strategic discipline: maintenance/curiosity loops should drop unless they "
        "concretely unlock revenue, distribution, authority, or compounding capability."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "loop_id": {"type": "string", "description": "The ol_<hash> ID from the briefing"},
            "verdict": {"type": "string", "enum": ["drop", "park", "act"]},
            "next_action": {"type": "string", "description": "For 'act' only: the concrete next step Gary stated"},
            "calendar_when": {"type": "string", "description": "For 'act' only: ISO datetime or natural language"},
            "note": {"type": "string", "description": "Optional brief context"},
        },
        "required": ["loop_id", "verdict"],
    },
}


def _load_open_loops_brief() -> str | None:
    """Load today's open-loops brief if fresh; else None.

    Imports lazily so the open-loops repo is only required when actually used.
    """
    try:
        ol_path = os.path.expanduser("~/.hermes-custom/open-loops")
        if ol_path not in sys.path:
            sys.path.insert(0, ol_path)
        from brief_format import render  # type: ignore
        return render()
    except Exception:
        return None


# Optional deployment-specific facts the realtime model can rely on without
# guessing. Inject things like "your filesystem-write tools are real" or
# "your logs live at <path>" so the model stops hallucinating refusals about
# its own capabilities. Leave empty for the generic public scaffold.
AGENT_DEPLOYMENT_NOTE = os.getenv("AGENT_DEPLOYMENT_NOTE", "").strip()

# Tool-call routing prompt. Rewritten May 2026 for the granular toolkit cutover:
# narrow tools hit direct backends (sub-second), `deep_research` is the only
# slow path. gpt-realtime-2's native parallel function calling means small
# lookups should fan out, not serialize. The dossier in your instructions
# carries today's standing context — address it directly.
_BASE_PROMPT = """\
You are a live voice assistant with deep context about the user. Your
DOSSIER (appended to these instructions) carries today's open loops,
calendar, recent decisions, hot people, and the working state from your
previous call. ADDRESS IT DIRECTLY — don't re-fetch what's already there.

TOOLKIT:
- Narrow read tools (sub-second): `lookup_open_loop`, `recent_decisions`,
  `search_notes`, `calendar`, `gmail_search`, `mission_control_card`.
- One slow tool: `deep_research` — covers BOTH (a) novel reasoning,
  drafting, synthesis, AND (b) any ACTION with side effects: write/save
  files, draft and save emails, create calendar events, edit notes, run
  scripts. The agent backend has full filesystem access, Gmail draft /
  Calendar write, and shell tools. Use this tool for any "do X for me"
  request, not just "think about X."

TOOL-CALL DOCTRINE:
1. Prefer the narrowest applicable read tool for factual lookups (calendar,
   email, notes, cards, decisions). Don't escalate read-only questions to
   `deep_research`.
2. Any user request that requires *doing* something (saving a file,
   drafting an email and saving it, creating a calendar event, editing
   a note, etc.) goes through `deep_research` with scope="action". The
   agent will perform the action and return a concrete handle.
3. Fan out small tools in parallel when a single turn needs multiple
   lookups. The API supports it — don't serialize "let me check your
   calendar… ok now let me check your email."
4. Prefer in-session reasoning when the answer is already in the dossier
   or a prior tool result this call. Don't re-fetch what's in your context.
5. `deep_research` is the slow path. When you call it: TELL the user
   roughly how long ("this'll take about a minute, I'll narrate as I go").
   Partial findings stream in as `[research-finding] section=…` system
   messages — narrate them as they arrive; don't claim completion until
   you receive the function_call_output.
6. ANTI-FABRICATION: never invent user-specific facts. If you're unsure
   whether the dossier or a prior tool result covers a name, date, file,
   commitment, or decision, escalate to a tool rather than guess. "I'd
   need to check" beats a confident wrong answer; a tool call is better.

ANSWER-QUALITY RULES:
7. If a tool returns `{"error": …}`, say so plainly ("I couldn't reach
   your calendar — try again in a sec"). Never invent a fallback.
8. If a tool returns a list, enumerate briefly first ("you have three:
   A, B, C"), then synthesize. Don't blend distinct items into one
   fuzzy summary.
9. Don't synthesize beyond what the tool returned. If the data isn't
   there, say it isn't there.

VERIFIED COMPLETION RULES:
10. Don't say "done", "sent", "created", "updated", or "saved" unless a
    tool response included a concrete handle — a file path, ID, link, or
    timestamp confirming the action. Drafted ≠ done. If you asked for an
    action via `deep_research` and the response didn't include a handle,
    say "I asked for it" rather than "done."

STYLE RULES:
11. Lead with the answer — the number, the time, the yes/no, the decision.
    Reasons after, only if asked or load-bearing.
12. Phone-call tempo: ≤2 sentences per turn unless asked for more. Numbers
    spoken naturally ("ten thirty", not "10:30 colon zero zero").

CAPABILITIES (TRUTH — DO NOT CONTRADICT):
- Questions about your model, voice, logs, or where data lives can be
  answered via `deep_research` if the dossier doesn't cover them. Don't
  improvise refusals like "I don't have access to that".

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


async def _agent_chat_stream(conv_id: str, user_text: str):
    """Async generator yielding agent SSE chunks as they arrive.

    Used by `/api/deep-research` to emit section milestones mid-stream and
    by `_agent_chat_collect` to assemble a final string (dossier refresh,
    post-call extraction, /api/text-turn).

    Liveness model:
      - httpx `read` timeout = ASK_AGENT_IDLE_TIMEOUT_SEC (raises ReadTimeout
        if the backend goes silent mid-stream).
      - Caller wraps in asyncio.wait_for(ASK_AGENT_TIMEOUT_SEC) for the outer
        runaway guard.
    """
    if not AGENT_API_KEY:
        raise RuntimeError("Agent API key not configured")
    timeout = httpx.Timeout(
        connect=10.0, read=ASK_AGENT_IDLE_TIMEOUT_SEC, write=10.0, pool=10.0,
    )
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
                    yield piece


async def _agent_chat_collect(conv_id: str, user_text: str) -> str:
    """Drain the stream into a single string. Idle/timeout exceptions bubble."""
    chunks: list[str] = []
    async for piece in _agent_chat_stream(conv_id, user_text):
        chunks.append(piece)
    return "".join(chunks)


async def _agent_chat(conv_id: str, user_text: str) -> str:
    """Cap at ASK_AGENT_TIMEOUT_SEC. Raises asyncio.TimeoutError on overrun."""
    return await asyncio.wait_for(
        _agent_chat_collect(conv_id, user_text),
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


async def _mint_realtime_session(
    instructions_suffixes: list[str | None] | None = None,
) -> dict:
    """Mint an OpenAI Realtime ephemeral session. Raises on HTTP/network error.

    Uses the May 2026 GA endpoint (/v1/realtime/client_secrets). The session
    config is nested under ``session`` and audio under ``audio.input`` /
    ``audio.output``. ``instructions_suffixes`` is a list of optional suffixes
    appended in order to ``VOICE_SYSTEM_PROMPT`` (``None``/empty entries are
    skipped). Typical compose: ``[dossier_md, triage_brief?, handoff_note?]``.

    Returns a legacy-compatible shape so the existing client code keeps reading
    ``session.client_secret.value`` and ``session.client_secret.expires_at``.
    """
    parts = [VOICE_SYSTEM_PROMPT]
    for s in (instructions_suffixes or []):
        if s and s.strip():
            parts.append(s.strip())
    instructions = "\n\n".join(parts)
    transcription_prompt = (
        "Gary Gurevich, Crunchy Numbers, Crunchy Tools, Flowocity, Hermes, "
        "Jerome, Claude, OpenClaw, Paperclip, Rillet, Pat Leahy, "
        "Y Combinator, Obsidian, Supabase, Vercel, Tailscale, fractional CFO, "
        "P&L, GL, RFS, AI-native agency."
    )
    body = {
        "session": {
            "type": "realtime",
            "model": OPENAI_REALTIME_MODEL,
            "instructions": instructions,
            "tools": TOOLKIT_SCHEMAS + [TRIAGE_VERDICT_TOOL_SCHEMA],
            "tool_choice": "auto",
            "output_modalities": ["audio"],
            "max_output_tokens": 1500,
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "noise_reduction": {"type": "near_field"},
                    "transcription": {
                        "model": "gpt-4o-mini-transcribe",
                        "language": "en",
                        "prompt": transcription_prompt,
                    },
                    "turn_detection": {
                        "type": "semantic_vad",
                        "eagerness": "low",
                        "create_response": True,
                        "interrupt_response": True,
                    },
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "voice": OPENAI_REALTIME_VOICE,
                },
            },
        }
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            "https://api.openai.com/v1/realtime/client_secrets",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        r.raise_for_status()
        ga = r.json()
    # Reshape GA response to the legacy contract the client expects.
    inner = ga.get("session") or {}
    return {
        "client_secret": {
            "value": ga.get("value"),
            "expires_at": ga.get("expires_at"),
        },
        "model": inner.get("model") or OPENAI_REALTIME_MODEL,
    }


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
    # Triage mode is opt-in via ?mode=triage. Default sessions stay user-driven.
    mode = (request.query.get("mode") or "").strip().lower()
    # Dossier first (today's standing context); triage brief second when
    # opted in (mode-specific doctrine layered over the dossier).
    dossier_md, dossier_meta = dossier.load_dossier()
    # Self-healing: when load returns None (missing or yesterday's date),
    # kick a background refresh so the next session gets a fresh one.
    # This mint stays unblocked — it serves without the dossier suffix.
    if not dossier_meta["loaded"] and AGENT_API_KEY:
        log.info("dossier stale/missing at mint — kicking background refresh")
        asyncio.create_task(dossier.refresh_dossier(
            agent_base=AGENT_API_BASE, agent_key=AGENT_API_KEY, force=True,
        ))
    triage_suffix = _load_open_loops_brief() if mode == "triage" else None
    try:
        data = await _mint_realtime_session(
            instructions_suffixes=[dossier_md, triage_suffix],
        )
    except httpx.HTTPStatusError as e:
        log.error("ephemeral mint failed: %s %s", e.response.status_code, e.response.text[:300])
        return web.json_response({"error": "ephemeral_mint_failed", "detail": e.response.text[:300]}, status=502)
    except Exception as e:  # noqa: BLE001
        log.exception("ephemeral mint exception")
        return web.json_response({"error": "ephemeral_mint_exception", "detail": str(e)}, status=502)
    log_call_event(
        LOG_DIR, conv_id, "session_minted",
        model=OPENAI_REALTIME_MODEL, voice=OPENAI_REALTIME_VOICE,
        mode=mode or "default", brief_injected=bool(triage_suffix),
        dossier_loaded=dossier_meta["loaded"],
        dossier_chars=dossier_meta["chars"],
        dossier_age_h=dossier_meta["age_h"],
        working_state_present=dossier_meta["working_state_present"],
    )
    return web.json_response({"conv_id": conv_id, "session": data, "mode": mode or "default"})


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


async def ask_agent_gone(request: web.Request) -> web.Response:
    """Hard cutover marker for stale PWA caches.

    The pre-cutover client called this route with `{conv_id, question, ...}`
    as the single fat tool. Stale-cache fallthrough into the slow path is
    exactly the regression this refactor exists to kill, so we 410 and ask
    the client to reload. The no-cache middleware (`no_cache_static_middleware`)
    forces shell revalidation on the next visit; one reload restores normal
    operation.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    conv_id = body.get("conv_id") or "unknown"
    log.warning("legacy /api/ask-agent hit — client outdated (conv=%s)", conv_id)
    log_call_event(LOG_DIR, conv_id, "legacy_ask_agent_blocked")
    return web.json_response(
        {
            "error": "client_outdated",
            "reload_required": True,
            "answer": (
                "Your voice client is out of date — please reload the page. "
                "The toolkit was upgraded."
            ),
        },
        status=410,
    )


# Dispatch table: tool name → (backend module, function name). The handler
# below imports lazily so missing backend deps (e.g. ripgrep absent) don't
# crash mint — they only surface when the tool fires.
_TOOL_DISPATCH = {
    "lookup_open_loop":     ("backends.supabase", "lookup_open_loop"),
    "recent_decisions":     ("backends.supabase", "recent_decisions"),
    "mission_control_card": ("backends.supabase", "mission_control_card"),
    "search_notes":         ("backends.notes",    "search_notes"),
    "calendar":             ("backends.gws",      "calendar"),
    "gmail_search":         ("backends.gws",      "gmail_search"),
}

# Per-tool cache TTLs (seconds). Calendar and Gmail need shorter windows so
# "what's on for the next hour" reflects late additions; static-ish queries
# (notes, recent_decisions) can ride a longer tail.
_TOOL_TTL = {
    "calendar": 120.0,
    "gmail_search": 90.0,
    "lookup_open_loop": 180.0,
    "mission_control_card": 180.0,
    "recent_decisions": 300.0,
    "search_notes": 300.0,
}


async def tool_dispatch(request: web.Request) -> web.Response:
    """Generic dispatch for the granular toolkit.

    Browser POSTs `{conv_id, name, args}` (or path param `/api/tool/<name>`).
    Handler looks up the backend, runs it with a TTL cache, returns JSON,
    and appends a tool turn to the conversation transcript.
    """
    name = request.match_info.get("name", "")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    conv_id = body.get("conv_id")
    args = body.get("args") if isinstance(body.get("args"), dict) else {}
    if not name:
        return web.json_response({"error": "missing_tool"}, status=400)
    conv = CONVERSATIONS.get(conv_id) if conv_id else None
    if conv is None:
        return web.json_response({"error": "unknown_conv_id"}, status=400)
    _ensure_conv_shape(conv)
    entry = _TOOL_DISPATCH.get(name)
    if entry is None:
        return web.json_response({"error": "unknown_tool", "name": name}, status=404)
    module_name, fn_name = entry
    _touch(conv)
    started_at = time.time()
    log_call_event(
        LOG_DIR, conv_id, "tool_call_spawned",
        tool=name, args_keys=list(args.keys()),
    )
    error_type: str | None = None
    cache_hit = False
    try:
        from importlib import import_module
        mod = import_module(module_name)
        fn = getattr(mod, fn_name)
        from backends import cache
        result, cache_hit = await cache.memoize(
            name, args, lambda: fn(**args), ttl_sec=_TOOL_TTL.get(name),
        )
    except TypeError as e:
        # Bad args (missing required, wrong types). Surface to the model as a
        # structured error so it can re-call with the right shape.
        result = {"error": "bad_args", "detail": str(e)[:200]}
        error_type = "bad_args"
    except Exception as e:  # noqa: BLE001
        log.exception("tool dispatch failed (%s)", name)
        result = {"error": "tool_failed", "detail": type(e).__name__}
        error_type = type(e).__name__
    finished_at = time.time()
    latency_ms = int((finished_at - started_at) * 1000)
    chars = len(json.dumps(result, default=str)) if result is not None else 0
    log_call_event(
        LOG_DIR, conv_id, "tool_call_done",
        tool=name, latency_ms=latency_ms, chars=chars,
        cache_hit=cache_hit, error_type=error_type,
    )
    # Persist a tool turn so the transcript has the structured I/O even
    # though the model only ever sees its own paraphrase in audio.
    conv["entries"].append({
        "role": "tool_question",
        "text": f"{name}({json.dumps(args, default=str)})",
        "ts": started_at,
    })
    conv["entries"].append({
        "role": "tool_answer",
        "text": json.dumps(result, default=str)[:6000],
        "ts": finished_at,
    })
    conv["last_activity_ts"] = finished_at
    return web.json_response({"ok": True, "result": result})


async def deep_research(request: web.Request) -> web.Response:
    """The one slow path. Streams from the agent backend, emits milestone
    events on markdown section boundaries, and assembles the final answer.

    Response is SSE: each line `data: <json>\\n\\n`. Browser injects each
    milestone into the Realtime conversation as a system message so the
    voice model can narrate progress between utterances. On `done`, the
    browser feeds the assembled answer back as `function_call_output`.
    """
    body = await request.json()
    conv_id = body.get("conv_id")
    prompt = (body.get("prompt") or "").strip()
    scope = (body.get("scope") or "reasoning").strip()
    expected_seconds = body.get("expected_seconds")
    conv = CONVERSATIONS.get(conv_id) if conv_id else None
    if conv is None:
        return web.json_response({"error": "unknown_conv_id"}, status=400)
    _ensure_conv_shape(conv)
    if not prompt:
        return web.json_response({"error": "empty_prompt"}, status=400)
    _touch(conv)
    started_at = time.time()
    log_call_event(
        LOG_DIR, conv_id, "deep_research_spawned",
        chars=len(prompt), scope=scope, expected_seconds=expected_seconds,
    )

    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
    await resp.prepare(request)

    async def _send(payload: dict) -> None:
        line = "data: " + json.dumps(payload, default=str) + "\n\n"
        await resp.write(line.encode("utf-8"))

    chunks: list[str] = []
    last_milestone_ts = 0.0
    pending_section = ""
    pending_buffer = ""

    async def _emit_milestone(section: str, text: str) -> None:
        nonlocal last_milestone_ts
        now = time.time()
        if now - last_milestone_ts < 3.0:
            return
        last_milestone_ts = now
        snippet = text.strip().replace("\n", " ")[:120]
        if not snippet:
            return
        await _send({"type": "milestone", "section": section or "progress", "text": snippet})

    status = "done"
    error_type: str | None = None
    try:
        async for piece in _agent_chat_stream(conv_id, prompt):
            chunks.append(piece)
            pending_buffer += piece
            # Section boundary: line starting with `## ` (Markdown H2).
            while "\n## " in pending_buffer or pending_buffer.startswith("## "):
                if pending_buffer.startswith("## "):
                    idx = 0
                else:
                    idx = pending_buffer.find("\n## ") + 1
                head = pending_buffer[:idx].strip()
                if head and pending_section:
                    await _emit_milestone(pending_section, head)
                rest = pending_buffer[idx:]
                # Extract the new section heading text up to the next newline.
                newline = rest.find("\n")
                if newline < 0:
                    pending_section = rest[3:].strip()[:60]
                    pending_buffer = ""
                    break
                pending_section = rest[3:newline].strip()[:60]
                pending_buffer = rest[newline + 1:]
    except asyncio.TimeoutError:
        status = "error"; error_type = "runaway"
    except httpx.ReadTimeout:
        status = "error"; error_type = "idle"
    except httpx.TimeoutException:
        status = "error"; error_type = "timeout"
    except Exception as e:  # noqa: BLE001
        log.exception("deep_research stream failed")
        status = "error"; error_type = type(e).__name__
    # Flush any trailing buffered section text as a final milestone before done.
    if pending_buffer.strip() and pending_section:
        await _emit_milestone(pending_section, pending_buffer.strip())

    answer = "".join(chunks).strip()
    if status == "error" and not answer:
        answer = _AGENT_UNREACHABLE_SENTINEL + " - please ask the user to repeat that."
    finished_at = time.time()
    latency_ms = int((finished_at - started_at) * 1000)
    log_call_event(
        LOG_DIR, conv_id, "deep_research_done",
        latency_ms=latency_ms, chars=len(answer),
        status=status, error_type=error_type,
        milestones_emitted=int(last_milestone_ts > 0),
    )
    conv["entries"].append({"role": "tool_question", "text": prompt, "ts": started_at})
    conv["entries"].append({"role": "tool_answer", "text": answer, "ts": finished_at})
    conv["last_activity_ts"] = finished_at
    await _send({"type": "done", "answer": answer, "status": status, "error_type": error_type})
    await resp.write_eof()
    return resp


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
    handoff_suffix = None
    if note:
        handoff_suffix = (
            "Continuation context from earlier in this same call: "
            + note
            + "\n\nCritical: do not greet, do not acknowledge any pause, do "
            + "not say 'as I was saying' or similar filler. Continue exactly "
            + "where you left off in topic and tone. Wait for the user's next "
            + "utterance before responding."
        )
    # On resume we still want the dossier present — same standing context as
    # the original mint; the handoff note layers on top for in-call continuity.
    dossier_md, _ = dossier.load_dossier()
    try:
        data = await _mint_realtime_session(
            instructions_suffixes=[dossier_md, handoff_suffix],
        )
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
    # Pre-mint inherits the dossier (handoff note layered server-side during
    # the actual swap-in via /api/resume).
    dossier_md, _ = dossier.load_dossier()
    try:
        data = await _mint_realtime_session(instructions_suffixes=[dossier_md])
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
    # Extract working state from this call, then refresh the dossier so the
    # next session sees both. Ordering matters: extract → refresh; never
    # blocks the live response.
    dossier.schedule_extract(
        conv_id, all_entries,
        agent_base=AGENT_API_BASE, agent_key=AGENT_API_KEY,
    )
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
            # Same extract → refresh chain as end_call. Idle-reaped calls still
            # produce signal worth carrying forward to the next session.
            dossier.schedule_extract(
                cid, all_entries,
                agent_base=AGENT_API_BASE, agent_key=AGENT_API_KEY,
            )
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


async def _start_dossier_refresh(app: web.Application) -> None:
    """Fire-and-forget dossier refresh at server boot.

    Never blocks startup — mint reads whatever's on disk and gracefully
    skips when stale, so a slow agent backend at boot doesn't keep the
    server from accepting calls.
    """
    if not AGENT_API_KEY:
        return
    asyncio.create_task(
        dossier.refresh_dossier(agent_base=AGENT_API_BASE, agent_key=AGENT_API_KEY)
    )


async def _stop_reaper(app: web.Application) -> None:
    task = app.get("reaper_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ---- app -------------------------------------------------------------------

async def triage_verdict(request: web.Request) -> web.Response:
    """Record a structured triage_verdict tool call into the conv transcript.

    Returns immediately. Reconcile cron reads these entries from the
    persisted voice transcript and creates calendar events for `act` verdicts.
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"error": "bad_json"}, status=400)
    conv_id = body.get("conv_id")
    conv = CONVERSATIONS.get(conv_id) if conv_id else None
    if conv is None:
        return web.json_response({"error": "unknown_conv_id"}, status=400)
    _ensure_conv_shape(conv)
    verdict = (body.get("verdict") or "").lower()
    if verdict not in ("drop", "park", "act"):
        return web.json_response({"error": "bad_verdict"}, status=400)
    loop_id = (body.get("loop_id") or "").strip()
    if not loop_id:
        return web.json_response({"error": "missing_loop_id"}, status=400)
    entry = {
        "role": "triage_verdict",
        "ts": time.time(),
        "loop_id": loop_id,
        "verdict": verdict,
        "next_action": body.get("next_action") or "",
        "calendar_when": body.get("calendar_when") or "",
        "note": body.get("note") or "",
    }
    conv["entries"].append(entry)
    conv["last_activity_ts"] = entry["ts"]
    log_call_event(LOG_DIR, conv_id, "triage_verdict", loop_id=loop_id, verdict=verdict)
    return web.json_response({"recorded": True})


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
    app.on_startup.append(_start_dossier_refresh)
    app.on_cleanup.append(_stop_reaper)
    app.router.add_get("/api/health", health)
    app.router.add_post("/api/session", session_mint)
    # Legacy `/api/ask-agent` is hard-cut to 410 so stale PWA caches can't
    # silently regress into the slow path — see `ask_agent_gone`.
    app.router.add_post("/api/ask-agent", ask_agent_gone)
    app.router.add_post("/api/tool/{name}", tool_dispatch)
    app.router.add_post("/api/deep-research", deep_research)
    app.router.add_post("/api/client-event", client_event)
    app.router.add_post("/api/text-turn", text_turn)
    app.router.add_post("/api/resume", resume_call)
    app.router.add_post("/api/premint-session", premint_session)
    app.router.add_post("/api/handoff-note", handoff_note)
    app.router.add_post("/api/end", end_call)
    app.router.add_post("/api/triage-verdict", triage_verdict)
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
