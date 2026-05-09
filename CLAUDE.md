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
1. **Browser client** (`web/app.js`, vanilla JS, no build step). Holds the WebRTC peer to OpenAI Realtime, the mic stream, the brown-noise icebreaker comfort bed, and all client-side continuity state. Loaded directly via `index.html`.
2. **Sidecar** (`server.py`, aiohttp). Mints Realtime ephemeral sessions, owns the `CONVERSATIONS` in-memory dict, runs `ask_agent` tasks, exposes the resume/premint/handoff endpoints, persists transcripts.
3. **Agent backend** (external). Reached via `AGENT_API_BASE` (default `http://127.0.0.1:8642`). Speaks an OpenAI-style chat-completions SSE protocol. The sidecar streams `_agent_chat()` against it. Bring your own.

**`ask_agent` flow (the core loop):**
- Realtime emits a `function_call` for `ask_agent` over the data channel.
- Client POSTs `/api/ask-agent` → server spawns an async task (`_run_agent_task`), returns either inline answer (fast path, ≤1.5s) or `{status: "running", task_id}`.
- Client long-polls `/api/agent-task/{task_id}` (25s windows) and feeds the answer back via `sendFunctionOutput()`.
- Server-side task survives client disconnect; `delivered_to_realtime` flag tracks claim state.

**Forced-reconnect continuity machinery (load-bearing, non-obvious):**
Realtime sessions hard-die at the 60-min cap; mobile resumes (visibility, network blip, silent freeze, `session_expired`) all funnel through `triggerResume` → `/api/resume`. Without intervention, the new session is amnesic. Three mechanisms preserve continuity:

1. **Handoff note** — at any forced teardown, the dying Realtime model writes an ≤80-word continuation note for its successor (text-only response, 2.5s deadline, partial-buffer fallback). Persisted via `POST /api/handoff-note`. Client helper: `requestHandoffNote()`. Server handler: `handoff_note`.
2. **Recent-entries replay** — `/api/resume` returns the last ~3K tokens of `conv["entries"]` with role normalization (`user_text`/`tool_question` → `user`, etc.). Client replays as `conversation.item.create` items in `onDcOpen` *before* the existing `completed_while_away` loop so `task_id` dedupe runs first. Server helper: `_recent_entries`. Single source of truth: the `instructions` patch + replay; the heuristic primer is **skipped** when `handoff_note` is present.
3. **60-min cap pre-mint + brown-noise bridge** — at 58:30 the client POSTs `/api/premint-session` to cache a fresh ephemeral; cap-swap fires at `min(59:00, expires_at − 10s)` and reuses the existing `/api/resume` to claim missed work. The `icebreaker` (procedural brown noise via WebAudio) fades in *before* peer teardown and out only after `pc.ontrack` first audio frame on the new peer. Client helpers: `schedulePremint`, `doPremint`, `gracefulCapSwap`, `endIcebreaker`.

**Concurrency hazards already handled:**
- `responseInFlight` tracking + dedicated `response.text.delta`/`done` cases gate handoff-note requests (Realtime allows only one response at a time).
- `pollAbortGen` counter (bumped in `triggerResume`) lets in-flight `pollAgentTask` loops bound to dead `call_id`s self-terminate; resume's `still_working` list re-attaches them with `callId=null`.
- `activeToolCalls = 0` reset on resume prevents zombie counter from hiding the icebreaker mid-answer.
- Mute state preserved across resume by re-applying `track.enabled = false` after `attachPeer` adds tracks to the new peer.

## Key files

- `server.py` — sidecar; all server-side endpoints (`/api/session`, `/api/ask-agent`, `/api/agent-task/{task_id}`, `/api/text-turn`, `/api/resume`, `/api/premint-session`, `/api/handoff-note`, `/api/end`, `/api/client-event`, `/api/health`).
- `web/app.js` — browser client; WebRTC peer + dc message switch (`onDcMessage`), `attachPeer`, `onDcOpen`, `triggerResume`, `gracefulCapSwap`, `cleanupCall`.
- `events.py` — append-only NDJSON per-call event logger (`log_call_event`).
- `transcripts.py` — end-of-call transcript persistence + optional Slack archive.
- `auth.py` — request auth + CIDR allowlist enforcement.

## Style

- Conventional commits (`feat:`, `fix:`, `refactor:`, `docs:`, `chore:`).
- `cleanupCall()` is for **true end** only. Forced reconnects must NOT call it — they need to preserve `convId`, `clientEntries`, `deliveredTaskIds`, the icebreaker, and the AudioContext across the gap. The dedicated `endIcebreaker()` helper exists so the resume path doesn't accidentally close the AudioContext.
- `voice` is **locked** after the first audio response. Never re-send it on `session.update` — doing so tears the session down. The `sessionPatch` builder in `onDcOpen` already omits it.
- The `ask_hermes` legacy tool name path exists for back-compat with cached PWA installs. Don't remove without a migration plan.

## Security boundaries

The browser never sees `OPENAI_API_KEY` or `AGENT_API_KEY` — both stay server-side. `VOICE_ALLOWED_CIDR` defaults to loopback. Only widen it on a trusted network (Tailscale, WireGuard). Transcripts are plain JSON on disk; `TRANSCRIPT_DIR` is the knob.
