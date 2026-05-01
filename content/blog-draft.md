# Context is the new engineering

*A blog post + a freebie. The repo is at the bottom.*

---

## The brainstorming partner I couldn't actually have

There's a kind of conversation I always wanted and could never really get.

The setup is something like this: I'm trying to learn something hard — a piece of physics behind something I'm building, an intricate piece of math I never bothered with in school, an architectural decision I can feel is wrong but can't yet say why. What I want is to talk it through with someone who actually knows the territory and who has the exact context for what I'm working on. Not a tutor. Not a Stack Overflow thread. A *brainstorming partner*.

The problem is that the right person for that conversation is, by definition,

> an expert whose time is valuable, who I would almost certainly annoy with my very basic and dumb questions and my questioning of the realities that they're assuming.

So the conversation never happens. Or it happens once, badly, and then I stop reaching out because I don't want to be the guy.

This was a real blocker for me — as a product builder, as a curious person, as a professional. I worked around it for years. Now I don't have to.

[GARY: optional half-paragraph — the "and now we have a solution" pivot. Keep it short; the rest of the post is the explanation.]

## Why this works now

The honest answer is not "AI got smarter." Frontier models have been smart enough for this for a while. What changed is that the *context* layer around the model finally got buildable.

Andrej Karpathy framed it cleanly in his recent Sequoia talk:

> "What's in the context window is your lever over the interpreter."

Treat that literally. In the Software 3.0 framing, the context window *is* the program. The model is a stochastic interpreter. What you arrange in front of it — your notes, your skills, your retrieval, your memory, your tools, your verification — that's the code. The actual engineering happens before the model ever sees a token.

That reframing makes a lot of recent confusion settle. Why do some people get 10x leverage out of these tools while others write half a feature and call it a day? Karpathy answered that too — he said people who are very good at this can peak "a lot more than 10x," because context engineering and model tuning compound. That's not a prompt-typing-speed gap. It's an engineering gap.

There's a corollary that anti-hype people will appreciate. Frontier models are *jagged*: they can refactor a huge codebase or find a vulnerability and then turn around and tell you to walk to a car wash 50 meters away when the actual goal was to wash the car. Fluency isn't reliability. The cure isn't better prompts; it's better *supervision* — context, evals, retrieval, the whole layer that keeps a brilliant-but-spiky model pointed at the right circuit. Context engineering is what makes jagged intelligence usable.

Which is what makes this the engineering discipline of the next several years. Not bigger models. Better context.

## The thing I built

I built it for myself first, on my own machine. The personal version is called Hermes Mini. The open-source version I'm releasing today is called **Talk to Your Context**.

Architecturally it's a small stack:

- **Browser PWA ↔ OpenAI Realtime over WebRTC.** This handles barge-in, natural turn-taking, the conversational feel. The Realtime model is the fast, shallow brain — it's the part that makes it feel like talking to a person.
- **Function bridge to my actual agent.** When a question is substantive, the Realtime model calls `ask_agent`, which routes through the full agent loop — skills, retrieval, tools, memory. That's where the context lives.
- **A "thinking" affordance.** Brown noise + a visual cue while the deep call runs. Honest about the latency rather than hiding it.
- **Text-mode fallback.** For when I want to type, or for long answers I'd rather read than hear.
- **Transcript persistence + post-call ingestion.** Every call gets saved as JSON; a summary gets folded back into the agent's memory so the *next* call starts where this one ended. The thing learns.
- **Optional Slack archive.** Each call becomes a private Slack thread, searchable like any other channel.
- **Tailnet-only by default.** Loopback or Tailscale CIDR allowlist. Private by default. Not internet-exposed.

The defining design choice is splitting the brain. Realtime model for turn-taking; real agent for substance. Most consumer voice products won't ship this because of the visible latency on deep calls — and that's the right call for them. It is the wrong call for me, because I'm not asking the live voice what the weather is. I'm asking it to think with me.

[GARY: optional concrete moment — one real brainstorming session this enabled. Half a paragraph. The kind that lands harder than feature bullets.]

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

[GARY: closing line. Personal, short. The "this is the interface I wanted; if it's also the interface you wanted, we're in the same boat" energy from the earlier draft is still good if you want it. Or something simpler.]

---

*Notes for Gary on this re-cut:*
- ~1,250 words before your fills — comfortably in the 1,200–1,800 range.
- New opener per the sweep recommendation: human blocker first, tech second. "Annoying expert" pull-quote is the hook.
- Karpathy quote is now spine, not garnish. Used twice (lever, 10x). Phrased as "framed it cleanly" / "answered" — within the medium-authority caveat.
- Anti-harness section added per the "lean team, not harness" POV. This is the differentiator from the AI-twitter complexity arms race.
- Reframed away from productivity-tool toward cognition-extension / brainstorming-substrate.
- "Systems guy" identity beat folded into "what works".
- Pay-it-forward / open-source ethos beat in the closing.
- Removed: the original "Why live voice products dodge deep context" mini-section — its content got absorbed into "thing I built" + "what's still imperfect", so it was redundant.
- Pull-quotes used: dumb-questions line ✓, Karpathy lever ✓, Karpathy 10x ✓, harness-trap ✓ (paraphrased into the section). The team-transcripts-compounding line didn't fit because this post is about personal use; save it for a Crunchy Numbers post.
- Builder-metaphor cluster (vampire / zebra / Nintendo-64) deliberately not used here — those belong in their own short post per the sweep's "Top 3" #3.
