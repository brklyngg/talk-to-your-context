I built the interface I wanted: live voice with my actual working context.

[GARY: one-line setup. Why I built it: I wanted brainstorming-with-an-expert-friend conversations, not Q&A. Off-the-shelf voice products are great at conversation but don't know what I'm working on, what I've already tried, or where I'm stuck. The bottleneck wasn't intelligence — it was context.]

What it does:
— Browser PWA + WebRTC to OpenAI Realtime for natural turn-taking and barge-in
— Function call into my Hermes agent for the substantive questions, so it answers from my actual notes/skills/memory/Slack history
— Brown-noise "thinking" affordance during deep calls so the latency is honest, not hidden
— Transcript persistence + post-call summary fed back into agent memory, so the next call starts where the last one ended

Honest caveat: deep-context calls lag 5–20 seconds. Real retrieval + agent loop takes real time. That's probably why ChatGPT Live Voice doesn't ship this architecture — the latency would feel broken to a general audience. For the brainstorming use case, the trade is worth it. I'll wait eight seconds for an answer that knows my world.

I'm open-sourcing it: [github.com/brklyngg/talk-to-your-context]. MIT. Default backend is Hermes; adapters scaffolded for Slack archive (and stubs for Telegram/Discord/Matrix).

[GARY: thesis close — one or two sentences. The frame Karpathy makes ("the context window is your lever") is starting to feel right. The leverage from AI isn't from typing prompts faster; it's from building up a context layer the model can plug into. Writing this off as "just AI stuff" misses where the actual engineering is moving.]

[Blog: link to crunchy.tools]

---

*Draft notes for Gary:*
- Target 200–300 words. Above is ~230 with your fills, which lands clean.
- No hashtags, no "agree?" CTA, no listicle formatting — voice skill anti-patterns.
- "Brown-noise thinking affordance" is the kind of concrete detail the voice skill says is good — could only Gary write this.
- The Karpathy framing in the close is paraphrased ("the context window is your lever") not quoted — feels lighter for LinkedIn. Quote it directly if you want.
