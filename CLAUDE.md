# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A local voice sidecar that connects a browser to OpenAI Realtime (voice/ears) and to a structured **dossier of standing context** plus a granular **direct-backend toolkit** (Supabase, Google Workspace, Obsidian) so the voice model is deeply contextual from second 1, not "generic until deep dive." A single slow path (`deep_research`) reaches the agent backend for novel reasoning. See `docs/ARCHITECTURE.md` for the rationale.

## Run / dev commands

```bash
# First-time setup
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # edit OPENAI_API_KEY + AGENT_API_BASE + DOSSIER_PATH

# Run
python server.py       # serves http://127.0.0.1:8090 by default

# Live deploy (personal Mac Mini — symlinks source into ~/.hermes-custom/hermes-mini/ and kicks launchd)
./sync-to-deploy.sh
```

No test suite or linter. Verification:
- **Per-call NDJSON** at `logs/calls/<conv_id>.ndjson` — append-only event trace. First place to look for WebRTC / Realtime / tool interactions.
- **Voice transcripts** at `$TRANSCRIPT_DIR/*.json` — for UX-quality audits.
- **`in_context_followup_rate`** in `events.py:compute_routing_metrics` — the headline metric. Fraction of user turns immediately after a tool answer that were served WITHOUT another tool call. Targets: ≥0.6 dossier-only, ≥0.8 full toolkit.

Syntax sanity-check:

```bash
python3 -m py_compile server.py dossier.py events.py transcripts.py auth.py backends/*.py
node --check web/app.js
```

## Architecture (the parts that span multiple files)

**Four layers:**

1. **Browser** (`web/app.js`, vanilla JS, no build step). WebRTC peer to OpenAI Realtime, mic stream, brown-noise icebreaker (cap-swap / forced-reconnect only — not in-call tool waits), per-tool consulting chips, SSE reader for `deep_research`, forced-reconnect continuity state. Loaded via `index.html`.
2. **Sidecar** (`server.py`, aiohttp). Mints Realtime ephemerals with the dossier baked into `instructions`. Dispatches `/api/tool/<name>` to `backends/*` (no LLM in path) and `/api/deep-research` to the streaming agent path. Owns `CONVERSATIONS` in-memory dict, persists transcripts.
3. **Dossier** (`dossier.py` + `~/.hermes/dossier/today.json`). Structured snapshot of today's standing context — open loops, calendar, recent decisions, hot people, last call's working state. Rendered as markdown and prepended to every session's instructions via `_mint_realtime_session(instructions_suffixes=[…])`. Regenerated on server boot and after every call ends (post-call extractor → refresh chain).
4. **Agent backend** (external). Reached via `AGENT_API_BASE` (default `http://127.0.0.1:8642`). Speaks OpenAI-style chat-completions SSE. Now reached only by: `deep_research`, dossier refresh, post-call working-state extraction, `/api/text-turn`. `X-Session-Id: voice-{conv_id}` header threads continuity.

**Tools registered per session (in `TOOLKIT_SCHEMAS`):**

| Tool | Latency | Transport | Backend |
|---|---|---|---|
| `lookup_open_loop(id)` | ~150ms | function | `backends.supabase` |
| `recent_decisions(days)` | ~100ms | function | `backends.supabase` |
| `mission_control_card(id)` | ~150ms | function | `backends.supabase` |
| `search_notes(query, k)` | 100–3000ms | function | `backends.notes` (ripgrep) |
| `calendar(when, account)` | ~500ms | function | `backends.gws` (gws-as.sh) |
| `gmail_search(query, account, limit)` | 500–1500ms | function | `backends.gws` |
| `deep_research(prompt, scope, expected_seconds)` | 30–240s | SSE | agent backend |

`deep_research` covers both *thinking* (reasoning/drafting/synthesis) and *doing* (actions with side effects: filesystem writes, Gmail drafts, Calendar writes, scripts) — the agent backend has the tools. Scope arg is `action|drafting|reasoning|synthesis`. For "save this to ~/Desktop/foo.md" or similar, the model calls `deep_research` with `scope=action` and the agent executes.
| `triage_verdict(loop_id, verdict, …)` | <50ms | function | `/api/triage-verdict` |

Per-tool TTL cache in `server.py:_TOOL_TTL` keyed on `(tool, args_hash)` via `backends/cache.py`.

**Narrow-tool flow:**
- Realtime emits `function_call` over the data channel. Browser POSTs `/api/tool/<name>` with `{conv_id, args}`. Sidecar dispatches through `_TOOL_DISPATCH` table → memoized `backends/*` call → JSON return. Sidecar persists a structured tool turn to the transcript; browser feeds JSON verbatim to Realtime as `function_call_output` so the model addresses fields directly instead of paraphrasing prose. Per-tool consulting chips fade in/out per `call_id`, supporting native parallel function calls on gpt-realtime-2.

**`deep_research` flow:**
- Browser POSTs `/api/deep-research` and reads the SSE stream. Sidecar (`_agent_chat_stream`) streams agent chunks, detects markdown section boundaries (`\n## ` headers), and emits `{type:"milestone", section, text}` events on a 3-second server-side throttle. Each milestone in the browser does two things: (1) appends `[research-finding] section=…: …` to conversation history via `conversation.item.create` (silent), and (2) fires an out-of-band `response.create` with `response.conversation: "none"`, `output_modalities: ["audio"]`, and an instruction to narrate that one finding in a single short sentence. The OOB mode is what keeps the narration from colliding with the pending function call's response slot. Narration is throttled separately from milestone ingest (≥10s between spoken updates) so longer researches don't get chatty. On `{type:"done"}` browser sends the assembled answer as `function_call_output`. Liveness: `ASK_AGENT_IDLE_TIMEOUT_SEC` (45s) is the primary read-watchdog via httpx; `ASK_AGENT_TIMEOUT_SEC` (600s) is the runaway guard. Errors surface via `_AGENT_UNREACHABLE_SENTINEL` so prompt rule fires.

**Dossier refresh + post-call working-state extraction:**
- `dossier.refresh_dossier()`: one agent call with a JSON-only directive → atomic write to `DOSSIER_PATH`. 30-min debounce; `force=True` bypasses. Schema: `{open_loops, calendar_today, recent_decisions, hot_people, last_handoff_summary, working_state_from_last_call}`.
- `dossier.extract_working_state(conv_id, entries)`: one agent call after a call ends → `{decisions, commitments, open_questions, deltas}` merged into the dossier. This is the cross-call continuity loop — the next session's instructions carry what we just decided/committed/learned.
- Local-date gating (`DOSSIER_TZ`, default America/New_York): when the on-disk dossier was generated on a prior local date (first call after midnight, etc.) it's *still* served to the in-flight session — `load_dossier()` prepends a `> Standing context — snapshot from <date>; today's regen in progress.` header rather than returning `None`. Mint never blocks on dossier and additionally kicks a background `refresh_dossier(force=True)` so the next session lands fresh (self-healing on day rollover). `session_minted` carries `dossier_stale`, `dossier_chars`, `instructions_chars`, and `instructions_hash` (sha1[:12]) so prompt drift across calls is auditable.

**`triage_verdict` (opt-in via `?mode=triage`):**
- `_load_open_loops_brief()` lazy-imports `~/.hermes-custom/open-loops/brief_format.py`, calls `render()`, then runs the result through `_triage_guardrails()` which prepends two override blocks: (a) tool-use rules — only `triage_verdict` is allowed in triage; `lookup_open_loop` will 400 because the brief's `ol_*` IDs are a separate ID space from Mission Control UUIDs; (b) a presentation rule that overrides the brief's "BLUF in ≤8 words" with the universal CONTEXT-SUFFICIENCY rule (1–3 sentences of grounding before each verdict ask, phrased naturally per-loop). If `render()` returns `None` because the brief is from yesterday, the wrapper passes the on-disk JSON back through with `generated_at` spoofed to bypass the date gate, prepended with a staleness header — same pattern as the dossier's stale-load fallback. Client routes `triage_verdict` function_calls to `POST /api/triage-verdict`. Reconcile cron (open-loops repo) creates `[Hermes Draft]`-prefixed calendar events for `act` verdicts.

**Forced-reconnect continuity (load-bearing, non-obvious):**
Realtime sessions hard-die at the 60-min cap; mobile resumes (visibility, network blip, silent freeze, `session_expired`) all funnel through `triggerResume` → `/api/resume`. Two mechanisms preserve continuity:

1. **Handoff note** — dying Realtime model writes an ≤80-word continuation note for its successor (text-only response, 3.5s deadline, partial-buffer fallback). Persisted via `POST /api/handoff-note`. On resume, server bakes the note into the freshly-minted ephemeral's `instructions` alongside the dossier suffix. Client `onDcOpen` echoes assembled `instructions` + `tools` via `session.update` as defense against the legacy `gpt-realtime` tools-drop quirk.
2. **60-min cap pre-mint + brown-noise bridge** — at 58:30 client POSTs `/api/premint-session`; cap-swap fires at `min(59:00, expires_at − 10s)` and reuses `/api/resume`. Icebreaker (procedural brown noise via WebAudio) fades in *before* peer teardown and out only after `pc.ontrack` first audio frame on the new peer. Client helpers: `schedulePremint`, `doPremint`, `gracefulCapSwap`, `endIcebreaker`.

**Hard cutover on legacy `/api/ask-agent`:** returns 410 `{error:"client_outdated", reload_required:true}`. The `no_cache_static_middleware` already forces shell revalidation; one reload restores normal operation. Stale-cache fallthrough into the slow path is exactly the regression the toolkit refactor exists to kill.

**Concurrency hazards handled:**
- `responseInFlight` tracking gates handoff-note requests (Realtime allows only one response at a time).
- Mute state preserved across resume by re-applying `track.enabled = false` after `attachPeer`.
- Parallel function calls each get their own `call_id`-keyed chip in `consultingChips`; independent fade-out.

## Key files

- `server.py` — sidecar; routes (`/api/session`, `/api/tool/{name}`, `/api/deep-research`, `/api/text-turn`, `/api/resume`, `/api/premint-session`, `/api/handoff-note`, `/api/end`, `/api/client-event`, `/api/triage-verdict`, `/api/health`). `/api/ask-agent` returns 410.
- `dossier.py` — structured dossier schema, `render_markdown`, `load_dossier`, `refresh_dossier`, `extract_working_state`, fire-and-forget schedulers.
- `backends/` — `supabase` (Mission Control), `gws` (Calendar/Gmail), `notes` (Obsidian/ripgrep), `cache` (TTL memo). Pure async functions; designed so Phase D can lift them into an MCP server unchanged.
- `web/app.js` — WebRTC peer + DC switch (`onDcMessage`), per-tool consulting chips, `dispatchToolCall` table, `handleNarrowTool`, `handleDeepResearch` (SSE reader + system-message milestone injection), `handleTriageVerdict`, forced-reconnect machinery.
- `events.py` — NDJSON logger; `compute_routing_metrics` derives per-tool counters/latencies, `deep_research_ratio`, `in_context_followup_rate`.
- `transcripts.py` — end-of-call persistence + optional Slack archive.
- `auth.py` — request auth + CIDR allowlist.

## Style

- Conventional commits (`feat:`, `fix:`, `refactor:`, `docs:`, `chore:`).
- `cleanupCall()` is for **true end** only. Forced reconnects must NOT call it — they preserve `convId`, `clientEntries`, the icebreaker, and the AudioContext across the gap. `endIcebreaker()` exists so the resume path doesn't accidentally close the AudioContext.
- `voice` is **locked** after the first audio response. Never re-send it on `session.update` — doing so tears the session down.
- **UX language: describe the action, not the architecture.** Tool-call chips describe what's happening for the user ("Searching email…", "Researching: …") — never how the system is doing it. The dossier/toolkit/agent split is an implementation detail; users see a single colleague.
- **CONTEXT-SUFFICIENCY (`_BASE_PROMPT` rule 9b, load-bearing):** Gary is managing many parallel threads. Whenever the model introduces *any* item — open loop, calendar event, email, card, research finding, draft for review — it must give 2–4 sentences of disambiguating grounding (what it is, why it surfaced, what's open) before asking for a decision. Never lead with "Item N: drop, park, or act?" — that puts the cognitive load on the user. Any new tool/feature that surfaces items inherits this contract; the prompt rule already covers it but UI affordances should not undercut it (e.g., don't add a chip that says only "Card 7?" with no context).
- New backends go in `backends/` as pure async functions; register in `server.py:_TOOL_DISPATCH` with a TTL in `_TOOL_TTL`. The tool schema lives next to its peers in `server.py` and goes into `TOOLKIT_SCHEMAS`. Browser dispatch picks it up via the `NARROW_TOOLS` set and `TOOL_CHIPS` phrasing table.

## Security boundaries

The browser never sees `OPENAI_API_KEY`, `AGENT_API_KEY`, or `SUPABASE_SERVICE_KEY` — all server-side. `VOICE_ALLOWED_CIDR` defaults to loopback; widen only on a trusted network (Tailscale, WireGuard). Transcripts and dossier are plain JSON on disk (`TRANSCRIPT_DIR`, `DOSSIER_PATH`).
