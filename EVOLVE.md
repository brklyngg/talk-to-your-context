# Workflow Evolution Log

- [communication style] Gary answers AskUserQuestion options with prose questions, not just selections — he wants dialog, not multiple choice. When his answer reveals a confusion, explain architecture (with a diagram if useful) before re-asking.
- [process patterns] Verify the target repo against the working directory before trusting a handoff/spec doc — handoff doc pointed at ~/.hermes/hermes-agent but the actual project was talk-to-your-context. Gary will redirect with a one-liner like "the handoff doc fucked up"; flag the mismatch on first sign of confusion rather than digging further into the wrong target.
- [quality bar] Single feature commit on main, but only push after explicit confirmation in-message — the harness blocks `git push origin main` even with prior plan-mode approval. Treat each push as a fresh authorization.
