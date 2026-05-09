# Product Analysis

> TODO: fill in.

## Requirements

- [ ] Live voice
- [ ] Interruptible
- [ ] Full context (delegates to your real agent)
- [ ] Persistent memory across calls
- [ ] Local-first
- [ ] Simple deploy
- [ ] Cheap standby
- [ ] Messaging archive
- [ ] Portable across MCP-speaking agent backends (voice locks to OpenAI Realtime)

## Compromises

- [ ] TODO
- [ ] TODO
- [ ] TODO

## Portability scope (May 2026 update)

Criterion #9 originally read "Portable across agent backends" and meant any
chat-completions SSE endpoint. As of the gpt-realtime-2 cutover, voice is
locked to OpenAI Realtime — that's where the model quality lives, and Realtime
has no cross-vendor equivalent we'd accept on parity. The agent backend
remains portable, but the contract is moving toward MCP: today the sidecar
forwards to chat-completions; the next phase exposes a remote MCP surface
(`ask_hermes`) that any MCP-speaking backend can implement.
