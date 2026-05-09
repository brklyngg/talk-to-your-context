# Claude Code Handoff: Reduce Deep-Context Call Frequency with an Optimized Running Log

**Created:** 2026-05-05 09:40 EDT  
**Repo:** `/Users/jeromebot/VibeCoding/talk-to-your-context`  
**Audience:** Claude Code CLI working in the repo  
**Goal:** diagnose and improve the architecture so Talk to Your Context keeps the “deep context” magic while avoiding unnecessary `ask_agent` calls on every substantive voice turn.

---

## 0. How to use this handoff

Claude Code should treat this as context and product intent, not a prescriptive implementation spec. The user wants Claude Code to understand the *why* and the *what* deeply enough to choose the *how* after inspecting the code.

Recommended Claude Code entry prompt:

```text
Read docs/CLAUDE_CODE_HANDOFF_DEEP_CONTEXT_RUNNING_LOG.md, then inspect the repo. Diagnose the current ask_agent/deep-context routing architecture and propose an implementation plan to reduce unnecessary deep-context calls by maintaining an optimized running log / session context layer. Do not overbuild. Preserve the product thesis: live voice for turn-taking, real agent for substance, transcripts that feed back into memory. Start with a plan, risks, and test strategy before editing.
```

If proceeding to implementation, prefer a small, reversible change set with instrumentation first.

---

## 1. Product thesis: what this is supposed to feel like

The product is **Talk to Your Context**: a live, interruptible voice/text interface into the user’s actual working context.

The personal version is **Hermes Mini**. The open-source version is this repo.

The key user motivation came from Gary’s spoken notes / fieldstones:

> “I always wanted to have deep in-depth technical conversations with an endlessly knowledgeable person that has the exact deep context needed to brainstorm something novel together.”

> “An expert whose time is valuable, who I would almost certainly annoy with my very basic and dumb questions and my questioning of the realities that they’re assuming.”

> “This was a big blocker in my life as a product builder, and generally as a curious person or as a professional, and now we have a solution.”

This should not feel like a generic voice assistant. The “magic” is not the voice. The magic is that the conversation can access the user’s real context: notes, skills, project memory, Slack history, prior calls, decisions, and current work.

The product framing from the blog draft:

- Off-the-shelf live voice products are good at the conversation part.
- They are bad at talking to **my context**.
- Without context, the assistant is a smart stranger.
- With context, it starts to feel like a colleague.

A key line from the product draft:

> “The product is not ‘voice AI’; it is a live interface into Gary’s own accumulated working context.”

---

## 2. Current architecture: split brain

Current code is intentionally a “split brain” architecture:

1. **Browser PWA ↔ OpenAI Realtime over WebRTC**
   - Handles speech, latency, barge-in, turn-taking, natural conversational feel.
   - This is the fast/shallow brain.

2. **Local sidecar (`server.py`, aiohttp)**
   - Mints OpenAI Realtime ephemeral sessions.
   - Proxies deep turns to the backend agent.
   - Persists transcripts.

3. **Backend agent via OpenAI-compatible `/v1/chat/completions`**
   - Holds full memory, skills, tools, retrieval, integrations.
   - This is the slow/deep brain.

4. **Transcript persistence + post-call ingestion**
   - `transcripts.py` writes per-call JSON.
   - `/api/end` sends the call transcript back into the same agent session so the agent can save the gist to memory.
   - Optional Slack archival posts a scannable thread.

Relevant files:

- `server.py`
- `transcripts.py`
- `web/app.js`
- `docs/ARCHITECTURE.md`
- `docs/PRODUCT_ANALYSIS.md`
- `content/blog-draft.md`
- `content/product-release.md`
- `content/linkedin-post.md`

---

## 3. The architectural issue to diagnose

### Current behavior

`server.py` defines the realtime system prompt with a very strong tool-use rule:

```python
CRITICAL TOOL-CALL RULES:
1. For ANY substantive question - calendar, email, memory, projects, drafting,
   research, anything beyond pure small talk - call ask_agent.
...
4. If you didn't call ask_agent and the user asks something substantive,
   you'll be wrong. Trust the brain. Default to consulting.
```

`ASK_AGENT_TOOL_SCHEMA` also says:

```python
"Consult your agent for memory, calendar, email, projects, drafting, "
"research, or any substantive question. Use for everything that isn't "
"small talk."
```

The browser receives OpenAI Realtime function-call events and runs:

- `handleAskAgent()` in `web/app.js`
- which calls `/api/ask-agent`
- which calls `_agent_chat()` in `server.py`
- which streams from the backend agent via `/v1/chat/completions`
- using `X-Session-Id: voice-<conv_id>`

### Why this is expensive / slow

The current instruction effectively turns **every meaningful voice turn** into a deep agent call.

That has benefits:

- It protects correctness.
- It prevents the realtime model from hallucinating about user-specific context.
- It makes the product demonstrably “talk to your context,” not “talk to a generic model.”

But it also creates a performance trap:

- Deep calls can take 5–20 seconds, sometimes longer.
- The product becomes less interruptible if every substantive follow-up waits on the deep agent.
- Many turns in a brainstorming session do **not** actually need a fresh retrieval/tool/memory pass.
- Some turns only need the current call’s evolving context: what the user just said, what the agent just answered, the current hypothesis, constraints, open questions, and decisions.
- Calling the full agent every time may duplicate work the session already knows.

From the product copy:

> “Deep-context calls lag. Five to twenty seconds depending on the question. There’s no way around this with current architectures — context retrieval plus agent loop takes real wall time.”

This remains true for genuinely deep calls. The problem is not that deep calls are slow; the problem is that the system may be **calling deep context too often** because there is no intermediate session memory layer.

### Product-level diagnosis

The architecture has only two modes today:

1. **Fast realtime model with almost no durable/contextual authority**
2. **Full backend agent call with full latency/cost**

What’s missing is a third layer:

3. **An optimized running log / session context layer**

This running log should let the system answer or route many follow-ups without re-running the whole deep-agent loop every time.

---

## 4. Why a running log is the right middle layer

Gary’s actual need is not “always consult the giant brain.” It is:

> “Talk to an endlessly knowledgeable person that has the exact deep context needed to brainstorm something novel together.”

In a real conversation with an expert, the expert does not re-open every book, inbox, calendar, and project file for every sentence. They keep a **working mental model** of the conversation:

- what we are trying to solve
- what assumptions are live
- what has already been established
- what the user cares about
- what options are on the table
- what open questions remain
- what “good” means for this session

That working mental model is the missing running log.

A good running log should make the system feel *more* like a colleague, not less:

- It remembers the local conversation better than raw transcript replay.
- It reduces repeated deep calls.
- It gives the realtime model enough grounded context to handle lightweight follow-ups.
- It gives the backend agent a compressed high-signal state when a deep call is necessary.
- It becomes the seed for post-call ingestion.

This maps to Gary’s broader context-engineering thesis:

> Karpathy’s frame: “what’s in the context window is your lever over the interpreter.”

The running log is the session-level context window lever.

---

## 5. What the optimized running log should contain

The running log should be short, structured, and updated continuously or at safe checkpoints. It should not be a raw transcript and should not grow unbounded.

Suggested shape:

```markdown
# Running Session Log

## Goal / active task
- What the user is trying to accomplish right now.

## User-specific context already established in this call
- Durable facts or constraints stated by the user.
- Preferences relevant to the current task.
- Project names, file paths, dates, people, decisions.

## Current working model
- The assistant’s best understanding of the problem, architecture, tradeoffs, or plan.

## Decisions made
- Explicit yes/no decisions or chosen direction.

## Open questions / unknowns
- Things that still require a deep call, tool lookup, user clarification, or later verification.

## Deep context already fetched
- Short summaries of prior `ask_agent` answers from this call.
- What source/tool/memory those answers were based on, if known.

## Do-not-repeat / routing notes
- Facts or answers that should not trigger another deep call unless the user asks for verification/freshness.
```

Keep this small. A useful target is **500–1,500 tokens**, not a growing transcript.

---

## 6. Routing model: when to call `ask_agent`

The end state should not be “never call `ask_agent`.” Deep context is the product. The goal is to avoid **unnecessary** deep calls.

### Always call `ask_agent` when the user asks for:

- personal/project memory not already in the running log
- calendar/email/Gmail/Slack/Drive/current external facts
- file contents or codebase facts not already loaded
- calculations, verification, current state, or tool-backed facts
- drafting that must reflect Gary’s durable voice/context beyond the current call
- “what did we say last time?” or cross-session recall
- anything the running log explicitly marks as unknown

### Usually do **not** call `ask_agent` when:

- the user asks a follow-up that can be answered from the running log
- the user asks for a clarification of the immediately previous answer
- the user is brainstorming or refining an idea already in the session context
- the user asks “say that again,” “what do you mean,” “compare option A vs B” where A/B were just established
- the user gives additional constraints and asks for a quick revised framing
- the user asks for a short recap of the current call so far

### Possible routing contract

Instead of “call ask_agent for any substantive question,” move toward:

> Use the running log first. Call `ask_agent` only when the answer requires context, tools, memory, or verification not already represented in the running log.

But the realtime model must still avoid pretending to know things it does not know.

---

## 7. Possible implementation directions Claude Code should evaluate

These are options, not commands. Inspect the code and pick the smallest robust path.

### Option A — prompt-only routing improvement

Update `VOICE_SYSTEM_PROMPT` and tool description to distinguish:

- local session follow-up
- running-log answer
- deep-agent lookup

Pros:
- Very small change.
- Low risk.

Cons:
- Realtime model still may not have access to a structured running log unless injected somehow.
- Hard to measure.
- Prompt-only changes are brittle.

### Option B — server-maintained running log injected into Realtime session

Maintain `CONVERSATIONS[conv_id]["running_log"]` on the server and send updates to the Realtime session via data channel `session.update` or per-response instructions.

Questions to investigate:

- Can the browser safely send `session.update` with updated instructions/context after each turn?
- Should the server generate the running log or should the browser/realtime model do it?
- How to avoid exposing secrets? The running log is session-local and should only include what is already said/fetched in this call.

Pros:
- Gives realtime model actual compressed context.
- Enables better routing.

Cons:
- Needs careful event flow design.
- Need to avoid prompt bloat and stale instructions.

### Option C — explicit `update_running_log` tool

Add a lightweight tool the realtime model can call to update a server-side running log, separate from `ask_agent`.

Tools might become:

- `ask_agent(question, reason)` — expensive deep call
- `update_running_log(delta)` — cheap local update
- possibly `get_running_log()` — if needed, though the model should already receive updates

Pros:
- Makes the running log an explicit architectural primitive.
- Allows instrumentation of how often the model updates log vs asks agent.

Cons:
- Tool-calling overhead may be awkward.
- The realtime model may overuse or underuse the tool.

### Option D — backend summarizer after each turn

After each completed user/assistant/tool turn, call a cheap local summarizer or small model to update the running log.

Pros:
- Higher-quality structured running log.
- Can be model/provider configurable.

Cons:
- Adds another model call; may undermine efficiency unless cheap/local.
- More moving parts.

### Option E — only optimize `ask_agent` payload

Even if every substantive turn still calls `ask_agent`, pass the running log along with the new question so the backend agent can avoid re-deriving session context.

Current `_agent_chat()` sends only:

```python
{"role": "user", "content": user_text}
```

A better payload could include:

```text
Current voice-session running log:
...

Latest user turn:
...
```

Pros:
- Reduces backend confusion/repeated work.
- Simple to implement once running log exists.

Cons:
- Does not by itself reduce call frequency.

### Likely best first version

A pragmatic first version may combine:

1. Add basic structured `running_log` state per `conv_id`.
2. Update it deterministically from recorded entries and tool answers, not via another model call at first.
3. Inject the latest running log into `ask_agent` payload.
4. Revise the realtime prompt/tool description to call `ask_agent` only when needed beyond the running log.
5. Add instrumentation: count total user turns, `ask_agent` calls, average latency, and reason/category if available.
6. Keep transcript persistence and post-call ingestion exactly as-is, but include the final running log in the transcript payload or adjacent metadata.

---

## 8. Instrumentation needed before judging success

Do not optimize blind. Add logging/metrics sufficient to answer:

- How many user voice turns happened in a call?
- How many `ask_agent` calls happened?
- What percentage of user turns triggered deep calls?
- What was average / p50 / p95 `ask_agent` latency?
- How many calls timed out or hit the idle watchdog?
- How often did the user interrupt during/after deep calls?
- What kinds of questions triggered `ask_agent`?
- Did the running log reduce duplicate calls?

Possible log fields:

```json
{
  "conv_id": "...",
  "event": "ask_agent_completed",
  "question_chars": 123,
  "latency_ms": 8421,
  "answer_chars": 932,
  "running_log_chars": 1800,
  "turn_index": 7
}
```

For privacy, avoid logging secrets or full sensitive content unless already in transcript files controlled by `TRANSCRIPT_DIR`.

---

## 9. Acceptance criteria

A good solution should satisfy these constraints:

### Product feel

- Voice remains live, interruptible, and lightweight.
- Deep calls still have an honest thinking affordance.
- The assistant still knows when to use the real agent.
- The user can have a natural brainstorming session without waiting 5–20 seconds for every follow-up.

### Context quality

- Follow-ups from the same call preserve context.
- The running log includes goals, constraints, decisions, fetched deep context, and open questions.
- The backend agent receives enough session context on deep calls to avoid repeated setup.
- Post-call ingestion has access to the final running log and/or transcript.

### Performance

- Reduce `ask_agent` calls per substantive conversation without sacrificing correctness.
- Avoid unbounded context growth.
- Avoid adding expensive extra model calls unless demonstrably worth it.

### Safety / correctness

- The realtime model must not hallucinate personal/project facts.
- If it needs unavailable information, it must call `ask_agent`.
- Calendar/email/file/current facts remain tool-backed via the backend agent.
- Transcript persistence remains idempotent and reliable.

### Simplicity

- No baroque multi-agent harness.
- No unnecessary orchestration framework.
- Keep the repo’s current local-first, adapter-friendly shape.

The product philosophy from the blog draft is important:

> “Talk to Your Context is small on purpose. Most of the complexity I cared about lives in the context layer — the LLM-wiki, the skills, the persistent memory — not in the agent topology.”

---

## 10. Code landmarks

### `server.py`

Important areas:

- `CONVERSATIONS`: currently `conv_id -> {started_at, entries}`.
- `ASK_AGENT_TOOL_SCHEMA`: currently encourages deep calls for almost everything beyond small talk.
- `VOICE_SYSTEM_PROMPT`: current routing rules are too broad for optimized performance.
- `_agent_chat(conv_id, user_text)`: sends latest question to backend agent with `X-Session-Id: voice-<conv_id>`.
- `ask_agent()`: records `tool_question` and `tool_answer` in conversation entries.
- `text_turn()`: pure backend-agent path for text mode.
- `end_call()`: merges browser transcript with server tool turns, writes JSON, schedules ingestion and Slack archive.

### `web/app.js`

Important areas:

- `clientEntries`: browser-side transcript entries.
- `fnCallBuffers`: captures Realtime function-call arguments.
- `handleAskAgent()`: calls `/api/ask-agent`, shows tool bubbles, feeds answer back to Realtime.
- `recordEntry()`: stores transcript turns for `/api/end`.
- `endCallBeacon()` / `endCall()`: posts final transcript to `/api/end`.

### `transcripts.py`

Important areas:

- `write_transcript()`: writes JSON.
- `ingest_into_agent()`: sends entire transcript back to the backend agent with “please save the gist to memory.”
- `post_to_slack()`: optional searchable Slack archive.

---

## 11. Suggested implementation plan shape

Claude Code should produce its own plan after inspection, but this is the expected task shape:

### Phase 1 — Measure current behavior

- Add timing around `_agent_chat()` / `ask_agent()`.
- Add per-conversation counters.
- Persist summary metrics in transcript JSON metadata or logs.
- Do not change routing yet unless trivial.

### Phase 2 — Add session running-log state

- Extend conversation state from:

```python
{"started_at": ..., "entries": []}
```

to something like:

```python
{
  "started_at": ...,
  "entries": [],
  "running_log": {...},
  "metrics": {...}
}
```

- Add helper functions for deterministic updates.
- Start simple: append/update compact bullets from user turns and tool answers.
- Cap size.

### Phase 3 — Use running log in deep calls

- Modify `_agent_chat()` or caller to include the running log plus latest user turn.
- Ensure the backend agent understands that the running log is session-local context, not durable fact unless verified.

### Phase 4 — Improve Realtime routing prompt

Replace “call ask_agent for any substantive question” with a more nuanced rule:

- Use running session context for local follow-ups.
- Call `ask_agent` for unavailable, tool-backed, personal/project, cross-session, or verification-dependent facts.
- Do not answer from generic knowledge when user-specific context is required.

If the running log is not visible to the Realtime model, do not rely on prompt-only routing.

### Phase 5 — Include final running log in post-call persistence

- Write running log into transcript JSON metadata.
- Include it in the memory-ingestion prompt so the agent ingests a clean summary rather than only raw transcript.
- Consider Slack top-message summary, but do not overdo Slack formatting in first pass.

### Phase 6 — Verify with realistic scenarios

Test scenarios should include:

1. Small talk: should not call `ask_agent`.
2. Personal/project memory question: should call `ask_agent`.
3. Follow-up to a just-fetched answer: should not call `ask_agent` unless verification/freshness is needed.
4. Brainstorming/refinement: should rely on running log when possible.
5. Current/email/calendar/file query: should call `ask_agent`.
6. End call: transcript + running log persist and ingest.

---

## 12. Risks and non-goals

### Risks

- Realtime model may over-trust its local running log and hallucinate context.
- Running log may become stale or misleading if not updated after tool answers.
- Too much injected context can hurt realtime responsiveness.
- Extra summarizer calls can erase the latency gains.
- A complex router can become the very “agent harness” the product argues against.

### Non-goals for first pass

- Multi-user auth.
- Full vector database / RAG layer inside this repo.
- Replacing backend agent memory.
- Building a general agent orchestration framework.
- Perfect long-term memory extraction.
- Full Slack Block Kit archive redesign.

---

## 13. User motivation to preserve

The user is not trying to shave milliseconds for its own sake. The performance work matters because the product is a **brainstorming substrate**.

The core experience should be:

- I can speak naturally.
- I can interrupt.
- The assistant knows my world.
- It does not make me repeat myself.
- It does not call the giant deep agent when the current conversation already has enough context.
- When it *does* need the giant deep agent, it is honest about the wait and gives me an answer grounded in my actual context.
- At the end, the call leaves a residue: the next call starts smarter.

Relevant product lines:

> “The transcript-feeds-back-into-memory loop matters more than the voice does.”

> “Each call leaves a residue.”

> “After two weeks the agent feels less like a tool and more like a colleague who was on the last call too.”

That is the product bar. Optimize toward that feeling.

---

## 14. Final instruction for Claude Code

Please inspect the current repo before deciding implementation. The suspected root issue is architectural, not just a slow API call:

> the system has a fast shallow brain and a slow deep brain, but no optimized session-level working memory in between.

Your job is to design and implement the smallest robust middle layer that lets Talk to Your Context keep deep context as its differentiator while reducing unnecessary deep-agent roundtrips.
