# Talk to Your Context

A live voice and text interface for your own context agent. Browser PWA + Python sidecar. You bring the agent — Hermes is the documented default, but anything with an OpenAI-compatible chat-completions endpoint works. The point isn't another chat UI; the point is brainstorming-grade conversation with a system that already knows your notes, skills, memory, and tools.

## What it does

- Live voice via OpenAI Realtime + WebRTC — natural turn-taking, barge-in
- Substantive questions route through your agent's full loop (skills, retrieval, memory, tools)
- "Thinking" affordance during deep-context calls so latency is honest, not hidden
- Text-mode fallback
- Transcript persistence + post-call summary ingested back into agent memory — each call leaves a residue
- Optional Slack archive (adapter stubs for Telegram, Discord, Matrix, email)
- Tailnet/loopback only by default — private, not internet-exposed

## Setup

Clone, `python -m venv .venv && pip install -r requirements.txt`, copy `.env.example` to `.env` and fill in your agent endpoint + OpenAI key, then `python server.py`. Open `http://localhost:8090`. Full setup in the README.

## Limitations (worth saying upfront)

Deep calls lag 5–20 seconds — real retrieval + agent loop takes real wall time. Single-user. Local-first. Transcript ingestion is summary-quality, not verbatim. Built lean on purpose: no multi-agent harness, no orchestration arms race. One agent + good context layer + the right skills.

MIT license. PRs welcome — especially adapters for non-Slack archive backends.

---

*Notes for Gary:*
- ~210 words. README launch-copy register.
- Repositioned from "talk to it like a friend" → "brainstorming-grade conversation" per sweep's cognition-extension reframe.
- Added the "each call leaves a residue" line — it's the most underestimated feature, per the blog draft.
- Added one anti-harness line in Limitations: "built lean on purpose." Not the full disclaimer (that's blog territory), but enough to signal the lean-team POV.
- Limitations section honest, no hype, no oversell.
