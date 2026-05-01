The brainstorming partner I always wanted is

> an expert whose time is valuable, who I would almost certainly annoy with my very basic and dumb questions and my questioning of the realities that they're assuming.

So I worked around it for years. I shipped a thing this week that finally fixes it for me, and I'm open-sourcing it.

It's a live voice + text interface to my own agent. Browser PWA + a small Python sidecar. Realtime model handles the conversational feel (barge-in, turn-taking); when a question is substantive, it routes through my agent's full loop — skills, retrieval, memory, tools — so the answer comes back from my actual working context, not a stranger.

The thing I underestimated: transcripts get summarized back into the agent's memory after every call. Each conversation leaves a residue. After two weeks it stopped feeling like a tool.

Honest caveat: deep-context calls lag 5–20 seconds. Real retrieval and a real agent loop take real wall time. That's almost certainly why consumer Live Voice products don't ship this architecture — the latency would feel broken to a general audience. For brainstorming, I'll wait eight seconds for an answer that knows my world.

Karpathy's framing has been on my mind: "what's in the context window is your lever over the interpreter." The leverage from AI right now isn't from typing prompts faster. It's from building up a context layer the model can plug into. That layer is the engineering.

[github.com/brklyngg/talk-to-your-context] — MIT. Default backend is Hermes; adapter stubs for Telegram/Discord/Matrix if you don't run Slack.

Full write-up: [crunchy.tools link]

---

*Notes for Gary:*
- ~270 words. Within the 200–300 target.
- Opens with the same human-blocker hook as the blog (per sweep recommendation). The "dumb questions" pull-quote does heavy lifting.
- Bullets gone — sweep flagged listicle formatting on LinkedIn as a voice-skill anti-pattern. Replaced with prose.
- Karpathy quote attributed; "leverage from AI isn't typing prompts faster" is the close.
- No hashtags, no "agree?" CTA, no engagement bait.
- The "anti-harness" angle is *not* in this draft — it's a longer thought that needs the blog to land. LinkedIn keeps tighter scope.
