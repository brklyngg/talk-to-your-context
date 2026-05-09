# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A local voice sidecar that connects a browser to OpenAI Realtime (the voice/ears) and to a separate agent backend (the brain) via a single function tool. The Realtime model handles speech in/out and tool dispatch; substantive thinking happens behind `ask_agent`, which proxies to the agent over HTTP. This split is the central design choice — see `docs/ARCHITECTURE.md` for the rationale.

## Run / dev commands

```bash
# First-time setup
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit OPENAI_API_KEY + AGENT_API_BASE

# Run
python server.py       # serves http://127.0.0.1:8090 by default

# Live deploy (personal Mac Mini sidecar — symlinks source into ~/.hermes-custom/hermes-mini/ and kicks launchd)
./sync-to-deploy.sh
```

There is no test suite or linter wired up. Verification happens via:
- **Per-call NDJSON event logs** at `logs/calls/<conv_id>.ndjson` — append-only event-flow trace. First place to look when debugging the WebRTC / Realtime / `ask_agent` interactions.
- **Voice transcripts** at `$TRANSCRIPT_DIR/*.json` — for UX-quality audits (fragmentation, mis-transcription, language drift). Reading transcripts ≠ reading NDJSON; they reveal different classes of bug.

Syntax sanity-check before deploying:

```bash
python3 -m py_compile server.py transcripts.py auth.py events.py
node --check web/app.js
```

## Architecture (the parts that span multiple files)

**Three layers:**
1. **Browser client** (`web/app.js`, vanilla JS, no build step). Holds the WebRTC peer to OpenAI Realtime, the mic stream, the brown-noise icebreaker (used **only** for cap-swap and forced-reconnect — not for in-call tool waits), and forced-reconnect continuity state. Loaded directly via `index.html`.
2. **Sidecar** (`server.py`, aiohttp). Mints Realtime ephemeral sessions, owns the `CONVERSATIONS` in-memory dict, synchronously forwards `ask_agent` to the agent backend, exposes the resume/premint/handoff endpoints, persists transcripts.
3. **Agent backend** (external). Reached via `AGENT_API_BASE` (default `http://127.0.0.1:8642`). Speaks OpenAI-style chat-completions SSE. The sidecar streams `_agent_chat()` against it. Bring your own. (Phase 2: this contract migrates to remote MCP via Tailscale Funnel.)

**Two Realtime tools registered per session:** `ask_agent` (core) and `triage_verdict` (open-loops integration; described below).

**`ask_agent` flow:**
- Realtime emits a `function_call` for `ask_agent` over the data channel.
- Tool args carry routing telemetry: `intent_type` (lookup/action/drafting/reasoning/verification/other) and `freshness_required` bool. The model is told to answer in-session for clarifications/recaps/comparisons/refinements; only escalate when external/current/cross-session context is genuinely needed. Both fields are logged, not branched on — see `events.py:compute_routing_metrics`.
- Client POSTs `/api/ask-agent` → server forwards synchronously to the backend → returns the answer inline. No task IDs, no long-poll, no fast-path machinery. gpt-realtime-2's native preambles + async function calling keep the conversation flowing through the wait.
- The client renders a subtle "Consulting deep context: [topic]" status chip on the assistant's bubble while the call is in flight. Phrasing describes the action, not the architecture (no "asking your brain" / "calling the agent" framing — exposing the split-brain seam is bad UX).
- Liveness model is two-knob: `ASK_AGENT_IDLE_TIMEOUT_SEC` (default 45s) is the primary watchdog — it drives httpx's `read` timeout, so a backend that goes silent mid-stream raises `ReadTimeout` fast, while a slow-but-streaming call keeps running. `ASK_AGENT_TIMEOUT_SEC` (default 600s) is the outer runaway guard via `asyncio.wait_for`. Failures surface via the `_AGENT_UNREACHABLE_SENTINEL` envelope so prompt rule #10 fires; NDJSON `error_type` is `idle` / `runaway` / `timeout` so post-mortems can tell which fired.

**`triage_verdict` flow + open-loops integration:**
- A second function tool registered alongside `ask_agent`. Args: `loop_id`, `verdict ∈ {drop, park, act}`, optional `next_action`, `calendar_when`, `note`.
- At session-mint, `_load_open_loops_brief()` lazy-imports `~/.hermes-custom/open-loops/brief_format.py`, reads `~/.hermes/open-loops/today.json`, and returns a formatted brief if `generated_at` is today (NY tz). The brief is appended to the system prompt via `instructions_suffix`. Off-window calls get an empty suffix → normal Hermes session.
- Client routes `triage_verdict` function_calls to `POST /api/triage-verdict` → server validates and appends `{role: "triage_verdict", ...}` to the conv transcript → returns `{recorded: true}` immediately. Reconcile cron (in the open-loops repo) reads these entries from the persisted transcript and creates `[Hermes Draft]`-prefixed calendar events for `act` verdicts.
- The open-loops repo is an optional sibling. The proxy works fine without it (lazy import, returns `None` on failure).

**Forced-reconnect continuity machinery (load-bearing, non-obvious):**
Realtime sessions hard-die at the 60-min cap; mobile resumes (visibility, network blip, silent freeze, `session_expired`) all funnel through `triggerResume` → `/api/resume`. Without intervention, the new session is amnesic. Two mechanisms preserve continuity:

1. **Handoff note** — at any forced teardown, the dying Realtime model writes an ≤80-word continuation note for its successor (text-only response, 3.5s deadline, partial-buffer fallback). Persisted via `POST /api/handoff-note`. Client helper: `requestHandoffNote()`. On resume, the server bakes the note into the freshly-minted ephemeral's `instructions` via `_mint_realtime_session(instructions_suffix=…)`. Client `onDcOpen` then echoes the assembled `instructions` + `tools` back via `session.update` as defense against the documented `gpt-realtime` tools-drop quirk (`gpt-realtime-2` GA may have fixed it; kept as belt-and-suspenders).
2. **60-min cap pre-mint + brown-noise bridge** — at 58:30 the client POSTs `/api/premint-session` to cache a fresh ephemeral; cap-swap fires at `min(59:00, expires_at − 10s)` and reuses `/api/resume`. The `icebreaker` (procedural brown noise via WebAudio) fades in *before* peer teardown and out only after `pc.ontrack` first audio frame on the new peer. Client helpers: `schedulePremint`, `doPremint`, `gracefulCapSwap`, `endIcebreaker`.

(Recent-entries replay was removed in the May 2026 cutover — gpt-realtime-2's 128K context + handoff-note-baked-into-instructions is sufficient. Reintroduce a thin version only if NDJSON shows continuity gaps post-deploy.)

**Concurrency hazards already handled:**
- `responseInFlight` tracking + dedicated `response.text.delta`/`done` cases gate handoff-note requests (Realtime allows only one response at a time).
- Mute state preserved across resume by re-applying `track.enabled = false` after `attachPeer` adds tracks to the new peer.

## Key files

- `server.py` — sidecar; endpoints (`/api/session`, `/api/ask-agent`, `/api/text-turn`, `/api/resume`, `/api/premint-session`, `/api/handoff-note`, `/api/end`, `/api/client-event`, `/api/triage-verdict`, `/api/health`).
- `web/app.js` — browser client; WebRTC peer + dc message switch (`onDcMessage`), `attachPeer`, `onDcOpen`, `triggerResume`, `gracefulCapSwap`, `cleanupCall`, `showConsultingChip`/`updateConsultingChip` (status indicator), markdown renderer for displayed tool-answer bubbles.
- `events.py` — append-only NDJSON per-call event logger (`log_call_event`); `compute_routing_metrics` derives ask_agent count vs in-session local-answer turns.
- `transcripts.py` — end-of-call transcript persistence + optional Slack archive.
- `auth.py` — request auth + CIDR allowlist enforcement.

## Style

- Conventional commits (`feat:`, `fix:`, `refactor:`, `docs:`, `chore:`).
- `cleanupCall()` is for **true end** only. Forced reconnects must NOT call it — they need to preserve `convId`, `clientEntries`, the icebreaker, and the AudioContext across the gap. The dedicated `endIcebreaker()` helper exists so the resume path doesn't accidentally close the AudioContext.
- `voice` is **locked** after the first audio response. Never re-send it on `session.update` — doing so tears the session down.
- The `ask_hermes` legacy tool name path exists for back-compat with cached PWA installs. Don't remove without a migration plan.
- **UX language: describe the action, not the architecture.** Tool-call indicators describe what's happening for the user ("Consulting deep context") — never how the system is doing it ("asking your brain" / "calling the agent"). The split-brain design is an implementation detail; users see a single colleague.

## Security boundaries

The browser never sees `OPENAI_API_KEY` or `AGENT_API_KEY` — both stay server-side. `VOICE_ALLOWED_CIDR` defaults to loopback. Only widen it on a trusted network (Tailscale, WireGuard). Transcripts are plain JSON on disk; `TRANSCRIPT_DIR` is the knob.
