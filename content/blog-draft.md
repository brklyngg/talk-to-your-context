# Context is the new engineering

*A blog post + a freebie. The repo is at the bottom.*

[GARY: optional standfirst — one-line tease. Skip if it feels like padding.]

---

## I wanted to talk to my context

Pulling this verbatim from a note I dictated into Obsidian a couple weeks ago, mostly so I don't sand the edges off it before you see what actually started this:

> I always wanted to have deep in-depth technical conversations with an endlessly knowledgeable person that has the exact deep context needed to brainstorm something novel together. Often when I want this it's when I'm trying to learn something — I'm trying to understand the physics behind something, or how to do some intricate piece of math that I didn't bother to learn while I was in school because I had no direct use for it at the time. I really couldn't have that without bending the ear of somebody who is a very credible expert, whose time is valuable, who I would almost certainly annoy with my very basic and dumb questions and my questioning of the realities that they're assuming. So it doesn't make for a very effective or frequent brainstorming session. This was kind of a big blocker in my life as a product builder, and generally as a curious person or as a professional. And now we have a solution.

[GARY: connector — half a paragraph. The "and now we have a solution" pivot. Resist the urge to over-set-up.]

## The missing interface wasn't intelligence — it was context

[GARY: one paragraph. The point: GPT-5 / Claude / Gemini are smart enough; the bottleneck for the kind of conversation I wanted was that none of them knew what *I* was working on, what I'd already tried, what the current state of my thinking was. The "expert friend" experience was always blocked by setup cost.]

[GARY: one more paragraph. Your stack now: Hermes agent + LLM-wiki + skills + persistent project memory + Slack/Obsidian/Drive plumbing. It all adds up to a thing that knows your world. The agent isn't smarter than the underlying model. It's just *contextual*.]

## Why live voice products dodge deep context

[GARY: this is the technically interesting middle. Premise: ChatGPT Live Voice is great — low latency, natural turn-taking, barge-in. But it does not deeply inhabit your local tools, repos, notes, memory, Slack history, project state. There's a structural reason: if you let the voice model phone-home to a heavyweight context-aware agent on every turn, you eat 10–30s of latency on the deep questions. That ruins the conversational feel. So consumer products optimize for low-latency-shallow over high-latency-deep.]

[GARY: pivot — that's the right call for a consumer product. It is the wrong call for *me*, because I'm not trying to ask Live Voice what the weather is. I'm trying to brainstorm.]

## The architecture I landed on

The thing I built is called Hermes Mini personally and **Talk to Your Context** as the open-source version. It's a browser PWA + a small Python sidecar:

- **Browser ↔ OpenAI Realtime over WebRTC.** This handles barge-in, natural turn-taking, the conversational feel. The Realtime model is the fast, shallow brain.
- **Function bridge to my actual agent.** When a question is substantive, the Realtime model calls `ask_agent`, which routes the question through the full Hermes loop — skills, retrieval, tools, memory.
- **A "thinking" affordance.** Brown noise + a visual cue while the deep call runs. Honest about the latency rather than hiding it.
- **Text mode fallback.** For when I want to type or when the deep response is long.
- **Transcript persistence + post-call ingestion.** Every call gets saved as JSON; a summary gets ingested back into the agent's memory, so the *next* call starts with the *previous* call as context. The thing learns.
- **Optional Slack archive.** Each call posts to a private Slack thread so I can search past conversations the same way I search any work channel.
- **Tailnet-only by default.** Tailscale CIDR allowlist. Private by default. Not internet-exposed.

[GARY: half a paragraph on *why* this combination. The single design choice: split the brain. Fast model for turn-taking, real agent for substance. Most consumer products won't ship this because of the visible latency on deep calls. For my use case, the latency is fine — I'd rather wait 8 seconds for a real answer than get an instant shallow one.]

## What works surprisingly well

[GARY: 3–5 short paragraphs. Concrete. Examples from real calls.]

- The transcript-feeds-back-into-memory loop is the part I underestimated. Each call leaves a residue.
- Barge-in actually matters more than I thought. Being able to interrupt the model mid-thought is the difference between "expert friend" and "lecture."
- [GARY: one personal moment. The kind of brainstorm you couldn't have had with anyone else.]

## What's still imperfect

Honest list:

- **Deep-context calls lag.** 5–20 seconds depending on the question. There's no way around this with current architectures — context retrieval + agent loop takes real wall time. The brown-noise affordance helps but doesn't eliminate the friction.
- **Single-user.** Auth is local-first / Tailscale. Multi-user with proper isolation is a different product.
- **Local-first assumption.** Requires you to be running a local agent with an OpenAI-compatible API. This is a specific kind of person's setup. (Hermes is the documented default; the adapters layer means it's not the only option.)
- **Transcript ingestion is summary-quality.** The agent remembers the *gist* of past calls, not the verbatim. Good enough for me; might not be for everyone.

[GARY: one paragraph admitting these — and then saying why it's still useful. Worth-the-tradeoff framing.]

## Context management is the new engineering

[GARY: this is the thesis section. The goal: make the case clearly enough that someone who's been hand-prompting their way through the last year goes "oh, I see what's actually leveraged here."]

Andrej Karpathy frames this directly. He calls the context window "your lever over the interpreter" — meaning, in Software 3.0, what you put into context is the program. You're not writing code; you're arranging context.

> "What's in the context window is your lever over the interpreter."
> — Karpathy, *From Vibe Coding to Agentic Engineering*, Sequoia AI Ascent 2026

He goes further. In the same talk he says people who are very good at context engineering and model tuning can peak "a lot more than 10x" — because the upside compounds through orchestration, calibration, and workflow design, not raw prompting speed.

[GARY: one paragraph connecting this to the product. The reason "Talk to Your Context" works isn't because GPT-realtime is brilliant. It's because it's coupled to a context layer — the LLM-wiki, the skills, the persistent memory — that *I* built up over months. The interface is voice; the substance is the context. If you stripped the agent out and just talked to Realtime, you'd have a chatty assistant with no memory of your world.]

[GARY: optional aside on jagged intelligence. Karpathy's car-wash example is a good throwaway here — frontier models can refactor a huge codebase but tell you to walk to a car wash 50 meters away when the actual goal was to wash the car. Fluency isn't reliability. The cure is context, supervision, and knowing where the model's strong circuits actually are. Tie back: my agent isn't smarter than the base model; it's just better-supervised, better-contextualized, and routed through skills that have been tuned for the specific tasks I care about.]

[GARY: closing pull on the thesis. Something like: the people getting outsized leverage from AI right now aren't the people typing prompts faster. They're the people who built up a context layer over time — notes, skills, retrieval, evals, persistent memory — and figured out how to plug the model into it. That's an engineering discipline. It looks like writing prose because it produces text artifacts, but the loops are the same as any other system: design, test, observe, iterate.]

## Here's the repo

`github.com/brklyngg/talk-to-your-context` — MIT, give-it-away.

Default backend is Hermes (because that's what I run). The adapters layer means you can plug in any local agent that exposes an OpenAI-compatible chat completions endpoint — `ollama serve`, vLLM, your own thing. There are README stubs for swapping the Slack transcript archive to Telegram, Discord, Matrix, or email.

Setup is the standard clone-install-env pattern; full instructions in the README. It assumes you already have a running local agent. If you don't, the [Hermes docs] are the easiest starting point. [GARY: link]

If you build an adapter, send a PR.

[GARY: closing line. Personal, short. Something like: this is the interface I wanted; if it's also the interface you wanted, we're in the same boat.]

---

*Draft notes for Gary:*
- Word count target: 1,200–1,800. Skeleton above is roughly 700 of mine + your filler. You'll land in range easily.
- Karpathy is `authority: medium / opinion / verify=true` per the wiki — phrased as "Karpathy frames" / "He goes further" rather than "Karpathy proved." Keep it that way on polish.
- The Obsidian quote at top is voice-dictated — I cleaned grammar but kept the cadence. Decide whether to clean further or leave it raw as "this is what the unprocessed thought looked like."
- I left the car-wash + chess examples as optional. They earn their place if you're going essay-long; cut them if you're going essay-tight.
- The "more than 10x" line is a direct quote from Karpathy. Worth keeping verbatim.
