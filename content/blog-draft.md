# I built the live voice interface I wanted: talk to your context

*A blog post + a freebie. The repo is at the bottom.*

> Companion to an earlier post on Karpathy's framing for finance teams. That one argues *why* context is the lever; this one shows what one feels like as a working product.

---

## The brainstorming partner I couldn't actually have

There's a kind of conversation I always wanted and could never really get.

The setup is something like this: I'm trying to learn something hard — a piece of physics behind something I'm building, an intricate piece of math I never bothered with in school, an architectural decision I can feel is wrong but can't yet say why. What I want is to talk it through with someone who actually knows the territory and who has the exact context for what I'm working on. Not a tutor. Not a Stack Overflow thread. A *brainstorming partner*.

The problem is that the right person for that conversation is, by definition,

> an expert whose time is valuable, who I would almost certainly annoy with my very basic and dumb questions and my questioning of the realities that they're assuming.

So the conversation never happens. Or it happens once, badly, and then I stop reaching out because I don't want to be the guy.

This was a real blocker for me — as a product builder, as a curious person, as a professional. I worked around it for years. Now I don't have to.

[GARY: optional half-paragraph — the "and now we have a solution" pivot. Keep it short.]

## Why I built it instead of using something off the shelf

I wrote earlier this week about [Karpathy's framing](LINK_TO_KARPATHY_FOR_FINANCE) — the context window is the lever, frontier intelligence is jagged, and the leverage compounds for the people who build the layer around the model rather than chasing whichever model is currently leading. If you haven't read that, the short version is: the engineering happens *before* the model sees a token, in the context layer.

This post is what that frame looks like when you turn it into a working product.

The off-the-shelf live-voice products are excellent at the *conversation* part. Low latency, natural turn-taking, barge-in. What none of them do is talk to *my context* — my notes, my agent's skills, my project memory, my Slack history, the residue of every other call I've had on the same problem. Without that, they're a smart stranger. With it, they're a colleague.

So I built the smaller half of the gap myself.

[GARY: optional bridge sentence here. The section can also land on "the smaller half of the gap myself" and move on.]

## The thing I built

The personal version is called Hermes Mini. The open-source version I'm releasing today is **Talk to Your Context**.

Architecturally it's a small stack:

- **Browser PWA ↔ OpenAI Realtime over WebRTC.** Handles barge-in, natural turn-taking, conversational feel. The Realtime model is the fast, shallow brain.
- **Function bridge to my actual agent.** When a question is substantive, the Realtime model calls `ask_agent`, which routes through the full agent loop — skills, retrieval, tools, memory. That's where the context lives.
- **A "thinking" affordance.** Brown noise + a visual cue while the deep call runs. Honest about the latency rather than hiding it.
- **Text-mode fallback.** For when I want to type, or for long answers I'd rather read than hear.
- **Transcript persistence + post-call ingestion.** Every call gets saved as JSON; a summary gets folded back into the agent's memory so the *next* call starts where this one ended. The thing learns.
- **Optional Slack archive.** Each call becomes a private Slack thread, searchable like any other channel.
- **Tailnet-only by default.** Loopback or Tailscale CIDR allowlist. Private by default. Not internet-exposed.

The defining design choice is splitting the brain. Realtime model for turn-taking; real agent for substance. Most consumer voice products won't ship this because of the visible latency on deep calls — and that's the right call for them. It is the wrong call for me, because I'm not asking the live voice what the weather is. I'm asking it to think with me.

[GARY: optional concrete moment — one real brainstorming session this enabled. Half a paragraph. Lands harder than feature bullets.]

## What works, and what doesn't

A few things I underestimated:

- **The transcript-feeds-back-into-memory loop matters more than the voice does.** Each call leaves a residue. After two weeks the agent feels less like a tool and more like a colleague who was on the last call too.
- **Barge-in is what makes it brainstorming, not lecture.** Being able to interrupt mid-sentence is the difference between a conversation and a podcast. I didn't realize how much that mattered until I had it.
- **The systems mindset transfers.** I'm a CPA-turned-builder. Internal controls, business systems, finance ops — that's the lane I came from. Building a context layer is the same kind of work: you're designing the machinery around something stochastic so it produces reliable output. Operators get this in their bones; engineers sometimes have to learn it.

What's still imperfect, said honestly:

- **Deep-context calls lag.** Five to twenty seconds depending on the question. There's no way around this with current architectures — context retrieval plus agent loop takes real wall time. The brown-noise affordance helps but doesn't eliminate the friction. This is probably why ChatGPT Live Voice doesn't ship this architecture: the latency would feel broken to a general audience. For the brainstorming use case, I'll wait eight seconds for an answer that knows my world.
- **Single-user.** Auth is local-first. Multi-user with proper isolation is a different product.
- **Local-first assumption.** You need to be running a local agent with an OpenAI-compatible API. That's a specific kind of person's setup. (Hermes is the documented default; the adapters layer means it's not the only option.)
- **Transcript ingestion is summary-quality, not verbatim.** Good enough for me. Might not be for everyone.

## A note on agent harnesses

While I'm here: it has never been more tempting to build a crazy complex agent harness — multi-agent orchestration, sub-agents that hire sub-agents, a 47-step ReAct loop with tool-routing on every turn. **It's a trap.**

The right model is leaner. Hire agents the way you'd add new roles to a lean team. One agent with a good context layer and the right three skills will out-perform a baroque multi-agent harness on almost any real task — and you can actually debug it when it goes sideways. Talk to Your Context is small on purpose. Most of the complexity I cared about lives in the *context* layer (the LLM-wiki, the skills, the persistent memory) — not in the agent topology.

That distinction is the one I'd want a reader to take away. Context is leverage. Harnesses are mostly LARP-ing.

## Here's the repo

[`github.com/brklyngg/talk-to-your-context`](https://github.com/brklyngg/talk-to-your-context) — MIT, give-it-away.

Default backend is Hermes (because that's what I run). The adapters layer means you can plug in any local agent that exposes an OpenAI-compatible chat-completions endpoint — `ollama serve`, vLLM, your own thing. There are README stubs for swapping the Slack transcript archive to Telegram, Discord, Matrix, or email. If you build an adapter, send a PR.

I'm releasing it as a freebie because the tools that taught me to build were freebies. Open source is its own pay-it-forward economy and I'd like to be in it. If you make something better with this, I want to hear about it.

[GARY: closing line. Personal, short.]

---

*Notes for Gary on this re-cut (post-bifurcation):*
- The Karpathy thesis (jagged intelligence, car-wash, chess, "context window is the lever," 10x ceiling, "outsource thinking not understanding") has been removed from this post. It now lives in the **Karpathy for Finance Teams** post, which is assumed published before this one drops.
- Replaced the deleted "Why this works now" section with a one-paragraph bridge that links to that post (`LINK_TO_KARPATHY_FOR_FINANCE` placeholder — fill once that crunchy.tools URL exists).
- Removed the Karpathy quote from the LinkedIn close (the prereq post already used it; double-billing the same source on LinkedIn within a week dilutes both posts).
- Anti-harness section stays — it's product-philosophy adjacent, not Karpathy translation.
- Result: tighter post, sharper product focus, no double-billing on the thesis. Post is now ~1,000 words before your fills (was ~1,250). The shorter shape suits the product-release frame.
- Pull-quotes still used here: the "annoying expert" line and the harness-trap paraphrase. Both Karpathy quotes moved to the prereq post.
