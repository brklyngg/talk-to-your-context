# Handoff: voice call shows "Agent is temporarily unreachable" — chase the upstream cause

**Created:** 2026-05-09 ~14:35 ET (Jerome — TTYC's Mac Mini sidecar)
**Symptom Gary saw:** voice connected, model fired `ask_agent` for "save a picture of a fuzzy bear on my Mac mini desktop", model said "Got it." then the bubble showed *"Agent is temporarily unreachable - please ask the user to repeat that."*
**Branch:** `refactor/gpt-realtime-2-cutover` (latest commit `175c3cb`)

## Stop — read this before touching the timeout

The obvious fix is "the sidecar's 90s total-wait cap on `ask_agent` is too aggressive; bump it / restore the idle-watchdog pattern." That's a real fix. **It's not the right first move.** The deeper question is *why did Hermes need >90s to handle "save a fuzzy bear image to ~/Desktop"?* Answer that upstream question first; then decide whether the timeout needs adjusting at all, or whether the timeout already correctly *surfaced* a Hermes-side problem.

Gary's principle: identify root causes and fix underlying issues; don't add knobs that just hide downstream symptoms longer. The timeout caught something. Find out what.

## Equally important: the cost constraint Gary has explicitly called out

Even if Hermes legitimately needs minutes for a deep task, **the architecture must not require the OpenAI Realtime peer to be running the whole time the user is waiting.** Realtime is billed per audio-minute (mic-active). Pre-cutover, the system was specifically designed so a long Hermes task did NOT keep the user paying Realtime minutes for silence:

- Server spawned `asyncio.Task` for the agent call → returned `{task_id, status: "running"}` to the client immediately.
- Client could long-poll OR disconnect entirely (background the iPhone, lock the screen, kill the WebRTC peer); the server-side task kept running.
- On the next `/api/resume` (visibility return, network blip, cap-swap), the server returned a `completed_while_away` list and the answer was replayed into the new Realtime session.

The cutover (commits `1649602` and `26348ba`) deleted that machinery: `_run_agent_task`, `pollAgentTask`, the `agent_tasks` dict, `delivered_to_realtime`, `still_working`, `completed_while_away`. I assumed gpt-realtime-2's native preambles + async function calling would substitute. **They don't, on the cost dimension.** They keep the model talking conversationally during the wait — which is still paid audio minutes. They don't let the user disconnect.

So the constraint for any fix is: **avoid forcing the Realtime peer to stay live during long Hermes work.** Letting it stay live is fine for short tasks (a few seconds); it stops being fine somewhere in the 10-30s range.

This is an architectural axis the simple "bump the timeout" fix completely ignores. Don't ignore it.

## Evidence (don't skip this)

NDJSON for conv `oLh4L7oiNz2syxUu` (file: `~/.hermes-custom/hermes-mini/logs/calls/oLh4L7oiNz2syxUu.ndjson`):

```
ask_agent_spawned  intent_type=action  freshness_required=false  chars=232
  question prefix: "Create and save an image of a fuzzy bear on the Mac Mini Desktop..."
ask_agent_done     latency_ms=90004    status=error    error_type=timeout
ended              ask_agent_count=1   local_answer_turns=2   ask_ratio=0.333
```

What `latency_ms=90004` means: the sidecar's `asyncio.wait_for(_agent_chat_inner, timeout=90)` cap fired at exactly 90.004s. The SSE stream from Hermes was still open at that moment. The sidecar cancelled it, returned `_structured_unreachable("timeout")` to the model, model spoke rule #10's sentinel.

`server.log` 14:30:56 → 14:33:01:
- `14:30:56,064` — sidecar opens `POST http://127.0.0.1:8642/v1/chat/completions` (200 OK on stream open).
- `14:32:26` — sidecar's 90s cap fires; stream cancelled.
- `14:32:50` — Gary clicks End.
- `14:33:01` — second `POST /v1/chat/completions` returns 200. **This is the post-call `ingest_into_agent` background task firing on `/api/end`, NOT a retry of `ask_agent`.** Don't confuse the two.

So: the request reached Hermes, Hermes started streaming, but didn't produce a final answer within 90s.

## Diagnose upstream FIRST — Hermes / brain layer

Open the right logs and answer these questions in order. Don't proceed to the sidecar fix until you have answers.

### Q1: What did Hermes actually do during those 90s?

Hermes log paths (this Mac Mini has multiple Hermes-related dirs — be careful which you read):
- `~/.hermes/` is the canonical Hermes Agent install (the brain backend on `:8642`).
  - `~/.hermes/logs/` — try this first. Gateway / agent-loop logs.
  - `~/.hermes/sessions/` — per-session conversation state. The X-Session-Id used by the sidecar is `voice-oLh4L7oiNz2syxUu` (note `voice-` prefix — see `server.py:_agent_chat_inner`). Find that session's directory.
- `~/.hermes-custom/hermes-mini/` is the **TTYC sidecar** install (NOT the Hermes brain). Don't get these confused.

Look for the `voice-oLh4L7oiNz2syxUu` session in Hermes' state. What model did it use? What skills/tools did it invoke? How long did each tool call take? Did it loop? Did it ever finish, or was it still working when the sidecar cancelled?

### Q2: Was the right tool even available?

The user asked to "save a picture of a fuzzy bear on my Mac mini desktop." This needs:
- An image-generation capability (or a way to fetch a bear image from the web).
- A filesystem-write capability targeting `~/Desktop`.

Check Hermes' enabled skills/tools for this session. Per project memory: Hermes has full filesystem access on this machine and can write to `~/Desktop`. But does it have an image-generation skill enabled? If not, the task is impossible and Hermes was probably wandering trying to satisfy it. **A 90s "wandering" failure is materially different from a 90s "doing real work that legitimately takes that long" failure.** The sidecar fix is appropriate for the second; for the first, the right fix is at the Hermes skill/prompt layer.

### Q3: Was Hermes' model layer healthy at that moment?

Hermes' model chain (per project CLAUDE.md): primary `openai-codex/gpt-5.5` via ChatGPT Plus Codex OAuth (5hr/week quota), first fallback `ollama/gemma4:e4b`, last resort `openrouter/google/gemini-3.1-pro-preview`. If the OAuth token expired or the weekly quota was exhausted, Hermes might have been falling back mid-task — fallback transitions can stall.

Check at the time of the failure (14:30:56 → 14:32:26 ET on 2026-05-09):
```bash
openclaw models status         # may exist; project CLAUDE.md hints at openclaw tooling
hermes status                  # if available
tail ~/.hermes/logs/*.log      # whatever the right path is
```

Look for `model_fallback`, `auth_refresh`, `quota_exceeded`, `billing_backoff` events around 14:30 ET on 2026-05-09.

### Q4: Was Hermes silent the whole time, or producing output that just didn't add up to a final answer?

This matters because it tells you what the *sidecar* should have observed and how to interpret the timeout. Two cases:

- **Case A: Hermes was emitting SSE chunks the whole time.** Then the sidecar's idle watchdog (pre-cutover behavior, before commit `1649602`) would have kept the stream alive past 90s and eventually got a final answer. The cutover replaced idle-gap timeout with total-wall-clock timeout, which kills legit long calls.
- **Case B: Hermes opened the stream but went silent (no chunks, no keepalive).** Then the sidecar correctly gave up. The bug is upstream — Hermes hung. Fixing it at the sidecar just makes the user wait longer for a stuck Hermes.

The Hermes session log will tell you which case you're in. **Check this before changing the sidecar.**

### Q5: Has this conversation pattern (action-class ask_agent, multi-step task) ever worked end-to-end on this build?

`compute_routing_metrics` summarises NDJSON; `python -c "from events import compute_routing_metrics; ..."` over the last few days of `logs/calls/*.ndjson`. Filter `intent_type=action` calls and look at their `latency_ms` distribution. If action-class calls reliably took >90s pre-cutover too, the cutover just turned a slow-but-working flow into a fast-failing one — Hermes' action-class capability has been slow for a while and we haven't been measuring. That's a separate finding worth surfacing to Gary.

## Once you've answered Q1–Q5: decide what to fix

Map the answers to the right intervention. **Crucially**, the right architectural shape may need to address BOTH the Hermes-side issue AND the cost constraint above. Don't ship a fix that resolves "the user gets an answer" while regressing "the user pays Realtime minutes the whole time Hermes is thinking."

| Q4 answer | Q2 answer | Likely fix layer | Cost-aware shape |
|---|---|---|---|
| Case A (Hermes streaming, legit slow) | Tools available | **Architecture.** Restoring the idle-watchdog pattern alone is insufficient — that just keeps the Realtime peer alive longer, which is exactly what the cost constraint says not to do. The right fix is to re-introduce some form of "Hermes work survives client disconnect," matching the pre-cutover design's intent. | See "Long-task architectures" below. |
| Case B (Hermes silent / hung) | Tools available | **Hermes.** Investigate why Hermes hung mid-stream. Could be model-fallback stall, tool deadlock, skill bug. | Sidecar's 90s cap is correctly surfacing a Hermes hang. Don't extend it. |
| Either case | Image-gen tool NOT enabled / configured | **Hermes skill config.** Enable an image-gen skill or update Hermes' prompt so it refuses image tasks honestly instead of wandering. | The current user-visible failure ("Agent is temporarily unreachable") is *worse than the truth* ("I can't do that with my current tools"). |
| Either | Hermes model layer unhealthy (quota, OAuth, fallback) | **OpenClaw / model layer.** Refresh OAuth, fix the gateway, audit the fallback chain. | Sidecar timeout is downstream. |

### Long-task architectures (for Case A)

If Hermes legitimately needs minutes and the right fix is a sidecar/architecture change, evaluate these in order:

1. **Restore "task survives client disconnect" (closest to pre-cutover).** Re-introduce a server-side asyncio.Task for `ask_agent` that keeps running after the client disconnects. The client gets a `task_id` immediately and either long-polls (cheap on bandwidth, expensive on Realtime minutes if the model keeps talking) OR backgrounds the call entirely; on resume, the answer is delivered via `completed_while_away` replay or `instructions`-suffix continuation. **Caveats:** this resurrects a chunk of the cutover's deleted machinery; do it as the smallest reintroduction (a single per-call result cache keyed by call_id, not the full pre-cutover task lifecycle).
2. **Fire-and-forget delivery via Slack/Telegram.** For genuinely long tasks (>30-60s), the model says "I'll work on this and ping you when done," then disconnects the Realtime peer. Hermes finishes async; result lands in the user's Slack/Telegram (TTYC already has a Slack adapter wired). User reads or hears it later. Best for tasks that don't need conversational follow-up. Most cost-efficient.
3. **Trim Hermes-side first.** Before changing TTYC architecture, push for Hermes-side improvements: faster image-gen path, eager partial returns, short-circuiting when the model knows it has the answer. The architectural fix is only worth doing if Hermes' floor on action-class tasks is genuinely multiple-of-ten seconds.
4. **Hybrid.** Sync for sub-10s tasks; auto-promote to background-task pattern for longer ones, with a heartbeat from Hermes letting the sidecar know "still working" so the user can be told (and choose to hang up if they prefer).

Whichever you pick, **measure the cost impact first.** Look at the `voice-transcripts/*.json` and NDJSON `ended` events for `duration_s` distribution. If most calls are <60s today, fix #1 + a 60s threshold gives a clean win. If many calls are running long, fix #2 or #4 is needed.

### Don't paper over with a bigger total cap

The "obvious" fix — bumping `ASK_AGENT_TIMEOUT_SEC` to 300 or 600 — is the wrong move because:
- It doesn't address why Hermes is slow (Q1–Q5 unanswered).
- It actively makes the cost problem worse (longer waits = more Realtime minutes burned).
- If Hermes is actually hung (Case B), it just makes the user wait 5 minutes for the failure instead of 90s.

If a total cap is needed at all, it should be a *runaway guard* (e.g., 10 min) on top of an idle-gap watchdog, not a replacement for proper architecture.

## What I built that's relevant (so you don't waste time re-deriving)

The cutover (commit `1649602`) replaced this pre-cutover SSE pattern:
```python
# pre-cutover: idle-gap watchdog inside _agent_chat
async for line in _iter_sse_with_idle_watchdog(r, ASK_AGENT_IDLE_TIMEOUT):
    ...
```
with this post-cutover total-wait cap:
```python
# post-cutover: total wall-clock cap on the whole forward
return await asyncio.wait_for(
    _agent_chat_inner(conv_id, user_text),
    timeout=ASK_AGENT_TIMEOUT_SEC,  # default 90
)
```

`_iter_sse_with_idle_watchdog` was deleted in commit `1649602`. To restore the idle-gap pattern (if Q4 says Case A): `git show 1649602^:server.py | sed -n '/_iter_sse_with_idle_watchdog/,/^$/p'`.

Live `.env`s on this machine:
- `~/VibeCoding/talk-to-your-context/.env` — local dev placeholder.
- `~/.hermes-custom/hermes-mini/.env` — what the deployed sidecar reads. Has `ASK_AGENT_TIMEOUT_SEC=90`.

`sync-to-deploy.sh:73` — `add_if_missing ASK_AGENT_TIMEOUT_SEC '90'`. Update default if/when the sidecar fix lands.

## Verification before claiming "fixed"

Reproducing this means asking the model to do **the same task** that triggered the failure, on this machine:

> "Save a picture of a fuzzy bear on my Mac mini desktop."

Whatever the upstream fix is (Hermes skill, model layer, sidecar architecture, or a combination), the verification has TWO axes that must both pass:

**Functional:**
- Prompt produces a fuzzy-bear file at `~/Desktop/<some-name>.png` within a reasonable time, OR the model honestly says "I don't have the tools to do that" (better than a misleading "temporarily unreachable" message).
- No regression on simple ask_agent calls (single-skill lookup, e.g., "what's on my calendar tomorrow"). These should complete in <10s.
- Post-call `ingest_into_agent` still fires and writes to Hermes memory.

**Cost / architecture:**
- For a task that takes Hermes >30s, **the user must be able to disconnect / background the call without losing the work.** Test: kick off a long task, immediately hit End or background the iPhone, then return — the answer should still be available (via Slack thread, replay, or some other delivery channel that doesn't require the Realtime peer to have stayed live).
- `duration_s` from the NDJSON `ended` event should not balloon post-fix. If average call duration goes from ~60s to several minutes, the fix kept the user paying Realtime minutes through Hermes' deep work — that's the regression Gary is calling out.
- 48h NDJSON watch on `error_type` distribution AND `duration_s` distribution AND `latency_ms` p95 across `intent_type=action` vs `intent_type=lookup` calls. If action calls get longer (in `duration_s`) instead of detached (call ends quickly + answer arrives via background channel), the fix is wrong on the cost axis.

## Risks / things not to break

- **Don't bump `ASK_AGENT_TIMEOUT_SEC` as the fix.** That extends paid Realtime minutes for every long task; it makes the cost problem worse, not better.
- **Don't extend the cap to "infinity" without an idle watchdog.** A truly hung Hermes connection should fail fast. Idle-gap-bounded is the right liveness model.
- **Don't paper over a Hermes-side hang by waiting longer.** If Q4 is Case B, the right fix is in Hermes, not here.
- **Don't regress the "background = pause" UX.** Any fix should preserve (or restore) the property that the user can lock the iPhone screen during a long Hermes task without losing the result.
- **Don't merge the branch to main while the action-class flow is still failing.** This branch is on a feature branch (`refactor/gpt-realtime-2-cutover`) on purpose. Merge after fix lands and verifies on both axes (functional + cost).
- **Don't widen `VOICE_ALLOWED_CIDR`** to debug from another machine. Tailscale CGNAT (`100.64.0.0/10`) is already permitted in the deploy `.env`.

## Quick file/line reference

- `server.py:289-339` — `_agent_chat_inner` and `_agent_chat`. The 90s `asyncio.wait_for` is in `_agent_chat`.
- `server.py:58` — `ASK_AGENT_TIMEOUT_SEC` env-var read.
- `server.py:_agent_chat_inner` — sets `X-Session-Id: voice-{conv_id}`. That's the session ID to grep in Hermes' state for Q1.
- `commit 1649602` (`refactor: cut ask_agent proxy machinery; sync forward + gpt-realtime-2`) — diff against this commit's parent to see exactly what changed in the SSE forward pattern.
- NDJSON: `~/.hermes-custom/hermes-mini/logs/calls/oLh4L7oiNz2syxUu.ndjson` — the failing call.
- `events.py:compute_routing_metrics` — programmatic summarisation of NDJSON; useful for the 48h watch.

## Deploy steps once you have a fix

```bash
cd ~/VibeCoding/talk-to-your-context
# … make the upstream fix (Hermes-side, sidecar-side, or both) …
python3 -m py_compile server.py transcripts.py auth.py events.py
node --check web/app.js
git add <files>
git commit -m "<conventional message naming the actual root cause you fixed>"
./sync-to-deploy.sh   # auto-migrates ~/.hermes-custom/hermes-mini/.env
curl -s http://127.0.0.1:8090/api/health | python3 -m json.tool
git push origin refactor/gpt-realtime-2-cutover
```

Then ask Gary to retry the same fuzzy-bear prompt on his iPhone.
