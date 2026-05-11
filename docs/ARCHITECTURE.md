# Architecture

A live, interruptible voice line to your own agent. The pieces:

- **Browser PWA** (`web/`, vanilla JS) — UI + WebRTC peer to OpenAI.
- **OpenAI Realtime API** — voice (`gpt-realtime-2`, GPT-5-class reasoning, 128K context, native parallel function calling, native preambles + async function calling). Browser holds a direct WebRTC peer with OpenAI; audio never transits the sidecar.
- **Sidecar** (`server.py`, aiohttp) — mints OpenAI ephemeral sessions, dispatches the granular toolkit to direct backends, streams `deep_research`, owns conversation state, persists transcripts.
- **Dossier** (`dossier.py` + `~/.hermes/dossier/today.json`) — structured "today's standing context" (open loops, calendar, recent decisions, hot people, last call's working state) regenerated on boot and after every call. Rendered as markdown and injected into every session's instructions. This is what makes the voice agent feel deeply contextual from second 1 instead of "generic until deep dive."
- **Direct backends** (`backends/`) — `supabase` (Mission Control cards/journal), `gws` (Calendar/Gmail via the `gws-as.sh` wrapper), `notes` (Obsidian vault via ripgrep), `cache` (TTL memo). No LLM in the dispatch path — narrow tool calls land in 50ms–1.5s.
- **Your agent backend** — anything exposing an OpenAI-compatible `/v1/chat/completions` SSE endpoint. Receives a per-conversation `X-Session-Id: voice-{conv_id}` header. Now reached only via `deep_research` (the one slow tool), the dossier refresher, and the post-call working-state extractor. (Phase D: small tools migrate to remote MCP transport via Tailscale Funnel — see end of doc.)
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

## Flow: tool turns (the May 2026 cutover)

Substantive turns split into two paths. The model is told to prefer the narrowest applicable tool and fan small ones out in parallel; `deep_research` is reserved for novel reasoning, drafting, or synthesis no narrow tool covers.

### Narrow tools (50ms–1.5s, direct backend)

1. User speaks. Realtime model picks the right tool — often several in parallel — based on the dossier + this call's context.
2. Browser POSTs `/api/tool/<name>` with `{conv_id, args}`. Each call gets its own per-tool consulting chip ("Pulling calendar…", "Searching email…").
3. Sidecar routes to `backends/<module>.<fn>` via the dispatch table in `_TOOL_DISPATCH`. Result memoized via `backends.cache.memoize` with per-tool TTL.
4. Structured JSON returned to browser → `sendFunctionOutput(call_id, JSON.stringify(result))` → Realtime model addresses fields directly ("the ten o'clock with Pat") instead of paraphrasing prose.
5. Sidecar appends a structured tool turn to `conv["entries"]` and the per-call NDJSON (`tool_call_spawned` / `tool_call_done` with `tool`, `latency_ms`, `cache_hit`, `error_type`).

### `deep_research` (30–240s, streaming SSE)

1. User asks something that genuinely requires the slow path. Model calls `deep_research(prompt, scope, expected_seconds)` and tells the user roughly how long ("this'll take about a minute, I'll narrate as I go").
2. Browser POSTs `/api/deep-research`, reads the SSE stream.
3. Sidecar streams chunks from `_agent_chat_stream`, detects markdown section boundaries (`\n## ` headers), and emits `{type:"milestone", section, text}` events on a 3-second server-side throttle.
4. Browser injects each milestone into the Realtime conversation as a system message: `{type:"conversation.item.create", item:{role:"system", content:[{type:"input_text", text:"[research-finding] section=…: …"}]}}`. The voice model narrates progress between utterances; the consulting chip ticks through latest section.
5. On the final `{type:"done", answer}`, browser sends `function_call_output` with the assembled answer. Model speaks the synthesis.

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

## Phase D (deferred): remote-MCP transport for narrow tools

The narrow toolkit is implemented as function tools today (browser POSTs to `/api/tool/<name>`). gpt-realtime-2 natively supports remote MCP servers in the same session — OpenAI dispatches MCP calls directly without browser round-trip. Phase D swaps the small-tool transport: stand up an MCP server endpoint in `server.py` over Tailscale Funnel, configure the session with `{type:"mcp", server_url, allowed_tools, require_approval:"never", headers}`, remove the `/api/tool/<name>` routes and the browser dispatch table for migrated tools. `deep_research` and `triage_verdict` stay function tools (MCP doesn't stream responses; `deep_research` needs streaming).

**Decision gate**: ship Phase D only if Phase B's per-tool latency p50 ≥ 300ms (suggesting WebRTC round-trip is a meaningful share) AND `in_context_followup_rate` plateaus below 0.8. If backend latency already dominates, defer.

When delivered, this also redefines PRD criterion #9 from "portable across chat-completions backends" to "portable across MCP-speaking backends."

## Phase E (future): mid-call dossier refresh + `note_to_self`

After Phase D, expose `get_dossier()` and `note_to_self(text)` as MCP tools so the model can refresh working memory mid-call and persist observations durably. Closes the loop on "the model can edit its own working memory."
