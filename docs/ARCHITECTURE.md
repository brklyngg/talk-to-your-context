# Architecture

A live, interruptible voice line to your own agent. The pieces:

- **Browser PWA** (`web/`, vanilla JS) — UI + WebRTC peer to OpenAI.
- **OpenAI Realtime API** — voice (`gpt-realtime-2`, GPT-5-class reasoning, 128K context, native preambles + async function calling). Browser holds a direct WebRTC peer with OpenAI; audio never transits the sidecar.
- **Sidecar** (`server.py`, aiohttp) — mints OpenAI ephemeral sessions, synchronously forwards `ask_agent`, owns conversation state, persists transcripts.
- **Your agent backend** — anything exposing an OpenAI-compatible `/v1/chat/completions` SSE endpoint. Receives a per-conversation `X-Session-Id: voice-{conv_id}` header so it can track memory across resumes. (Phase 2: this contract migrates to remote MCP via Tailscale Funnel — see end of doc.)
- **Optional messaging adapter** — Slack today; see [ADAPTERS.md](ADAPTERS.md).

## The durable conversation

The system of record is `CONVERSATIONS[conv_id]` on the sidecar. WebRTC peers and OpenAI Realtime sessions are **disposable** — they get rebuilt across iOS PWA backgrounding, 5G ↔ wifi handoff, and the 60-min Realtime hard cap. The durable values:

- **`conv_id`** (sidecar-owned) — survives backgrounding, network drops, and unbounded resumes.
- **`entries[]`** — transcript items (`user`, `assistant`, `tool_question`, `tool_answer`, `user_text`, `assistant_text`).
- **`handoff_note`** — ≤80-word continuation note from a dying session, used to prime the next session's instructions.

Backgrounding is a **pause**, not an end. Only the explicit End button or `pagehide` close the conversation. A 20-minute idle reaper guards against forgotten calls.

## Lifecycle

```
┌── browser PWA ──┐                 ┌── sidecar (server.py) ─────────────┐
│ WebRTC peer    ─┼── ephemeral ────│ /api/session         mint conv_id  │
│ (disposable)   │     key per      │ /api/ask-agent       sync forward  │
│                │     session      │ /api/text-turn       typing path   │
│                │                  │ /api/resume          fresh peer key│
│                │                  │ /api/premint-session 60-min cap    │
│                │                  │ /api/handoff-note    save note     │
│                │                  │ /api/end             teardown      │
└─────────┬──────┘                  │                                    │
          │ conv_id (durable)       │ CONVERSATIONS[conv_id] = {         │
          │                         │   started_at, last_activity_ts,    │
          │                         │   entries[], handoff_note?         │
          │                         │ }                                  │
          │                         │ logs/calls/<conv>.ndjson           │
          │                         │ 20-min idle reaper                 │
          ▼                         └────────────────────────────────────┘
   visibilitychange→hidden : do nothing (just mark timeline)
   visibilitychange→visible: if peer dead → POST /api/resume → mint new key
   pc.connectionState=failed for >3s : same resume flow
   silent-freeze (>12s no inbound RTP) : same resume flow
   58:30 + min(59:00, expires-10s)     : pre-mint + cap-swap
   pagehide / End button   : POST /api/end (true close)
```

## Flow: substantive turn (`ask_agent`)

1. User speaks. Realtime model decides to call `ask_agent` (per system prompt) and emits a native preamble ("let me check that").
2. Browser POSTs `/api/ask-agent` with `{conv_id, question, call_id, intent_type, freshness_required}`.
3. Sidecar synchronously forwards to the backend's `/v1/chat/completions` and returns the answer inline. Total wait capped at `ASK_AGENT_TIMEOUT_SEC` (default 90s).
4. While the call is in flight, the browser renders a "Consulting deep context: [topic]" status chip on the assistant's bubble (live-updates as `response.function_call_arguments.delta` streams). The chip phrasing describes the action, not the architecture — never exposes the split-brain seam.
5. Async function calling means the model continues the conversation through the wait. No client-side polling, no in-call icebreaker, no hand-coded bridging.
6. When the answer arrives, browser feeds it to the realtime peer via `function_call_output`. The model speaks it. The chip fades out; the answer renders in a markdown bubble for display.
7. Sidecar appends both turns to `conv["entries"]` via the local conv ref — survives concurrent `/api/end` pop.

## Flow: backgrounding / forced reconnect

1. User backgrounds the iPhone (or wifi blips, or silent freeze, or 60-min cap fires). The WebRTC peer dies.
2. `visibilitychange→hidden` writes a `paused` marker into `clientEntries`. Server is NOT notified.
3. User returns. `visibilitychange→visible` (or watchdog / connectionStateChange) triggers `triggerResume`.
4. Client requests a handoff-note from the dying peer if still open (≤3.5s deadline; partial buffer fallback). Posts to `/api/handoff-note`.
5. Client tears down the dead peer (NOT `cleanupCall` — preserves `convId`, `clientEntries`, the icebreaker, and the AudioContext across the gap).
6. `POST /api/resume` mints a fresh OpenAI Realtime session, with the handoff-note baked into `instructions` server-side. Returns `{session, handoff_note}`.
7. Browser rebuilds the WebRTC peer (`attachPeer`). On data-channel open: cold-start path sends a one-line greet response.create; resume path waits for the user's next utterance — semantic_vad triggers naturally, no primer needed.
8. Brown-noise icebreaker fades in before peer teardown and out after `pc.ontrack` first audio frame on the new peer (5s hard cap).

(Pre-cutover, the resume payload also carried `recent_entries` for replay and `completed_while_away`/`still_working` for in-flight tool answers. Removed in May 2026: 128K context + handoff note is sufficient for single-session continuity, and async function calling means in-flight tool calls die with the peer.)

## Flow: 60-min cap

OpenAI Realtime sessions hard-die at the 60-min cap. The cap-swap dance:

1. At call start (and on every reattach), client schedules `doPremint` for 58:30.
2. `doPremint` POSTs `/api/premint-session` to mint a fresh ephemeral and stash it client-side. Schedules `gracefulCapSwap` at `min(59:00, expires_at − 10s)`.
3. `gracefulCapSwap` fades the icebreaker in, requests a handoff-note, calls `/api/resume` for fresh continuity, swaps in the pre-minted ephemeral, attaches the new peer. The user hears no seam.

## Flow: 5G ↔ wifi handover / silent freeze

Triggered by `pc.onconnectionstatechange === "failed"|"disconnected"` (debounced 3s) or by the inbound-bytes watchdog (>12s with state="connected" but no new RTP). Funnels into `triggerResume`. We don't attempt ICE restart — full reconnect is ~1-2s end-to-end and one recovery path covers all forced-reconnect cases.

## Diagnostic log

Per-call NDJSON at `logs/calls/<conv_id>.ndjson`. One line per event. `cat | jq` queryable.

Events:

- `session_minted`, `session_preminted`, `resumed`, `handoff_note_saved`
- `ask_agent_spawned`, `ask_agent_done` (with `intent_type`, `freshness_required`, `latency_ms`)
- `text_turn`
- `tool_error`, `legacy_background_end_ignored`
- `client_dc_opened`, `client_session_cap_swap`, `client_resume_first_audio`
- `reaped`, `ended`

`compute_routing_metrics` (`events.py`) derives the headline split: `ask_agent_count` (escalations) vs `local_answer_turns` (in-session answers) vs `ask_ratio`. Watch this post-cutover — gpt-realtime-2's improved reasoning should reduce the ratio significantly.

## Reaper

Polls every 60s. Conversations with no activity for 20min are reaped: transcript written, Slack thread posted with `reason=idle_timeout`, NDJSON `reaped` event. The dominant cost (mic-active minutes on `gpt-realtime-2`) stops within ~5s of backgrounding anyway; the 20-min cap protects against truly-forgotten calls.

## End / archive

`/api/end` (idempotent) writes the transcript to `TRANSCRIPT_DIR`, fires-and-forgets `ingest_into_agent` so the backend absorbs the call into memory, posts a Slack thread.

## Tradeoffs

- **No ICE restart.** Full reconnect (mint a fresh ephemeral, build a new peer) is ~1-2 s and works for both backgrounding and network handover. ICE restart is an optimization that requires SDP renegotiation surface OpenAI hasn't documented — not worth the complexity.
- **Sync ask_agent (no task IDs / long-poll).** gpt-realtime-2's native preambles + async function calling fill the wait. The pre-cutover task-survival machinery (server-side asyncio.Task surviving client disconnect, `still_working` reattach on resume, `delivered_to_realtime` claim flag) is gone. Soft regression: a forced reconnect mid-tool-call leaves the question hanging — model may need to re-ask. Watch NDJSON for orphaned `ask_agent_spawned` events without matching `ask_agent_done`; reintroduce a thin "result by call_id cache, hold 60s post-disconnect" if real.
- **Realtime + separate brain rather than monolithic.** Voice latency and persona stay tight on `gpt-realtime-2`; substantive cognition stays with the agent backend (full memory, skills, tool access). The two are bridged by the `ask_agent` proxy today.
- **No mid-call Slack updates.** Slack thread is a single post at end-of-call (or reap). Per-turn pings are noisy and the user can read the transcript locally.

## Phase 2 (deferred): native remote MCP

The next architectural move is to expose `ask_hermes` as a remote MCP server (HTTP/SSE transport) and switch the Realtime session config to `tools: [{type: "mcp", server_url: ...}]`. OpenAI's servers then dispatch the tool call directly to the MCP endpoint, saving the WebRTC data-channel round-trip (~30-100 ms per call).

Constraints: native remote MCP requires a public URL OpenAI's servers can reach — incompatible with TTYC's local-first default. The deployment-mode answer is **Tailscale Funnel**: expose `https://<tailnet>.ts.net/mcp` with API-key header auth, MCP server runs in `server.py` alongside the existing routes. Local-first principle is preserved in spirit (data stays on the machine; only the request channel is public).

When delivered, this also redefines PRD criterion #9 from "portable across chat-completions backends" to "portable across MCP-speaking backends."
