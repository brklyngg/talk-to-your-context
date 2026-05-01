# Talk to Your Context

A live voice and text interface for your local context agent. Browser PWA + Python sidecar. You bring the agent (Hermes is the documented default, but anything with an OpenAI-compatible chat-completions endpoint works); this gives you a way to talk to it like a brilliant friend who happens to remember your world.

## What it does
- Live voice via OpenAI Realtime + WebRTC — natural turn-taking, barge-in
- Substantive questions route to your agent's full loop (skills, retrieval, memory, tools)
- "Thinking" affordance during deep-context calls so latency is honest, not hidden
- Text-mode fallback
- Transcript persistence + post-call summary ingested back into agent memory
- Optional Slack archive (adapter stubs for Telegram, Discord, Matrix, email)
- Tailnet/loopback only by default — private, not internet-exposed

## Setup
Clone, `python -m venv .venv && pip install -r requirements.txt`, copy `.env.example` to `.env` and fill in your agent endpoint + OpenAI key, then `python server.py`. Open `http://localhost:8090`. Full setup in the README.

## Limitations (worth saying upfront)
Deep calls lag 5–20 seconds — real retrieval + agent loop takes real wall time. Single-user. Local-first. Transcript ingestion is summary-quality, not verbatim. This is a builder's tool, not a polished consumer product.

MIT license. PRs welcome — especially adapters for non-Slack archive backends.

---

*Draft notes for Gary:*
- ~200 words. Plain README launch-copy register. Factual, no hype.
- Limitations section is per voice-skill humble-tone rule. Don't oversell.
- "A brilliant friend who happens to remember your world" is the only flourish — pulls double duty as positioning.
- Setup section is deliberately terse; full instructions in README.
