# Talk to Your Context

A live, interruptible voice line to your own agent. Browser <-> OpenAI Realtime <-> a tiny local sidecar <-> whatever agent already holds your context.

> Status: early. Local-first. Bring your own agent backend.

## Contents

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) - how the pieces talk
- [docs/ADAPTERS.md](docs/ADAPTERS.md) - plugging in messaging backends
- [docs/PRODUCT_ANALYSIS.md](docs/PRODUCT_ANALYSIS.md) - requirements and compromises
- [content/](content/) - blog / launch drafts (TODO)

## Install

> TODO: fill in.

```bash
git clone <this repo>
cd talk-to-your-context
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then edit
python server.py
```

## Security

This sidecar can mint OpenAI Realtime sessions and proxy turns to your agent. Treat it like any service that holds an API key:

- Keep `.env` and `.agent-api-key` out of git (already in `.gitignore`).
- Default `VOICE_ALLOWED_CIDR` is loopback only. Don't widen it without a network you trust (Tailscale, WireGuard, etc.).
- The browser never sees `OPENAI_API_KEY` or `AGENT_API_KEY`; both stay server-side.
- Transcripts are written to disk in plain JSON. Set `TRANSCRIPT_DIR` accordingly.
