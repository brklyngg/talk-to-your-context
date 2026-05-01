The brainstorming partner I always wanted is

> an expert whose time is valuable, who I would almost certainly annoy with my very basic and dumb questions and my questioning of the realities that they're assuming.

So I worked around it for years. I shipped a thing this week that finally fixes it for me, and I'm open-sourcing it.

It's a live voice + text interface to my own agent. Browser PWA + a small Python sidecar. Realtime model handles the conversational feel — barge-in, turn-taking — while substantive questions route through the agent's full loop: skills, retrieval, memory, tools. The answer comes back from my actual working context, not a stranger.

The thing I underestimated: transcripts get summarized back into the agent's memory after every call. Each conversation leaves a residue. After two weeks it stopped feeling like a tool.

Honest caveat: deep-context calls lag 5–20 seconds. Real retrieval and a real agent loop take real wall time. That's almost certainly why consumer Live Voice products don't ship this architecture — the latency would feel broken to a general audience. For brainstorming, I'll wait eight seconds for an answer that knows my world.

I wrote earlier this week about why context is the lever rather than the model itself. This is what that frame looks like when you turn it into something you can actually use.

Repo + write-up in first comment.

---

*Notes for Gary:*
- ~250 words. Within the 200–300 target.
- Post-bifurcation: Karpathy direct-quote removed from the close (prereq post already used it). Replaced with a one-line callback to the prereq + framing of this post as the working-product instance.
- "Earlier this week" assumes the Karpathy-for-Finance post lands first. If timing slips, change to "I wrote recently about why context is the lever."
- Bullets gone — voice-skill anti-pattern flag. Prose throughout.
- No hashtags, no "agree?" CTA, no engagement bait.
- Anti-harness angle deliberately not in this LinkedIn — keeps scope tight; that's blog territory.
