# Architecture

> TODO: fill in with prose.

## Components

- Browser (PWA, vanilla JS)
- OpenAI Realtime (WebRTC, direct from browser)
- Local sidecar (`server.py`, aiohttp)
- Backend agent (any HTTP service exposing `/v1/chat/completions`)
- Optional messaging adapter (Slack today; see [ADAPTERS.md](ADAPTERS.md))

## Flow

- Mint ephemeral session
- WebRTC SDP exchange
- Realtime model handles voice + small talk
- `ask_agent` tool delegates substantive questions to the backend
- Browser persists transcript on `/api/end`

## Sequence diagrams

> TODO: render with mermaid or ascii.

### Start call

> TODO

### Small-talk turn

> TODO

### Deep call (`ask_agent` tool roundtrip)

> TODO

### End + archive

> TODO

## Tradeoffs

> TODO: idle-watchdog choice, why no full session timeout, why Realtime + separate brain rather than monolithic, etc.
