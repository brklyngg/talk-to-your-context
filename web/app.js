// Talk to Your Context - browser app. WebRTC <-> OpenAI Realtime, with ask_agent tool.
// Free of frameworks; vanilla DOM + fetch + RTCPeerConnection.

const $ = (id) => document.getElementById(id);
const transcriptEl = $("transcript");
const statusEl = $("status");
const dotEl = $("health-dot");
const talkBtn = $("talk");
const endBtn = $("end");
const muteBtn = $("mute");
const textToggle = $("text-toggle");
const textMode = $("text-mode");
const textForm = $("text-form");
const textInput = $("text-input");

// State
let pc = null;             // RTCPeerConnection
let dc = null;             // data channel "oai-events"
let micStream = null;
let convId = null;
let started = false;
let micMuted = false;
let audioCtx = null;
let icebreaker = null;     // {fadeIn, fadeOut, dispose} — restored as a comfort
                           // signal during ask_agent waits. Defaults to procedural
                           // brown noise; transparently swaps to /static/ambient.mp3
                           // (Suno track) when present and idle.
let ambientBuffer = null;  // decoded AudioBuffer cached when /static/ambient.mp3 served
let activeToolCalls = 0;
const clientEntries = [];  // {role, text, ts, latencyMs?} for end-of-call upload
let pendingAssistantBubble = null;
const turnTiming = { userDoneTs: null, firstTokenTs: null };

// Connection-state debounce (5G <-> wifi handover, transient blips)
let connDownSinceTs = null;
let resumeInFlight = false;
let watchdogTimer = null;

// Tasks delivered to the realtime peer; dedupe stale long-polls vs resume payloads.
const deliveredTaskIds = new Set();

// Last minted/resumed session config; echoed via session.update on dc-open so
// the model definitively sees instructions+tools (ephemeral-mint config alone
// is silently dropped by gpt-realtime in some flows).
let lastSessionConfig = null;
// Whether the current realtime response window contained a function_call.
// Reset on response.done so client_response_done can report it accurately.
let hadFunctionCallThisResponse = false;

// --- Forced-reconnect continuity state ---
// `responseInFlight` flips on response.created and back on response.done. The
// handoff-note request is gated on it being false (one outstanding response at
// a time per Realtime contract).
let responseInFlight = false;
// Single in-flight handoff request: { responseId, buffer, resolve, t0 }.
// `responseId` is null until response.created arrives, then bound to the real id
// so subsequent response.text.delta events route to the right buffer.
let activeHandoffRequest = null;
// Bumped whenever we tear down the peer for resume; in-flight pollAgentTask
// loops capture this generation at start and short-circuit if it changes.
let pollAbortGen = 0;
// Set true when the new peer's audio track first emits frames (pc.ontrack).
// Drives the brown-noise fade-out timing on resume.
let firstAudioFrameSeen = false;
// Wall-clock when the current Realtime session attached. Drives the 60-min cap
// pre-mint scheduling.
let sessionStartedAt = null;
// Pre-minted session payload + ephemeral expiry (Unix seconds).
let premintedSession = null;
let premintedExpiresAt = null;
let premintTimer = null;
let capSwapTimer = null;

function logClientEvent(event, payload = {}) {
  if (!convId) return;
  fetch("/api/client-event", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ conv_id: convId, event, ...payload }),
  }).catch(() => {});
}

// --------------- UI helpers ---------------

function setStatus(text, cls = "") {
  statusEl.textContent = text;
  statusEl.className = "status " + cls;
}
function setDot(cls) { dotEl.className = "dot " + cls; }

const pad2 = (n) => String(n).padStart(2, "0");
function timeMarkerLabel(ts) {
  const d = new Date(ts);
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}
function latencyLabel(ms) {
  if (typeof ms !== "number" || !isFinite(ms) || ms < 0) return "";
  const seconds = Math.round(Math.max(0, ms) / 100) / 10;
  return seconds < 10 ? `${seconds.toFixed(1).replace(/\.0$/, "")}s` : `${Math.round(seconds)}s`;
}
function composeMetaLabel(meta) {
  if (!meta || typeof meta.createdAt !== "number") return "";
  const parts = [timeMarkerLabel(meta.createdAt)];
  const total = latencyLabel(meta.latencyMs);
  if (total) parts.push(total);
  const first = latencyLabel(meta.firstTokenLatencyMs);
  if (first) parts.push(`first ${first}`);
  return parts.join(" · ");
}

function appendBubble(role, text, meta) {
  const div = document.createElement("div");
  div.className = "bubble " + role;
  const metaEl = document.createElement("div");
  metaEl.className = "bubble-meta";
  const label = composeMetaLabel(meta);
  metaEl.textContent = label;
  if (!label) metaEl.style.display = "none";
  const textEl = document.createElement("div");
  textEl.className = "bubble-text";
  textEl.textContent = text;
  div.appendChild(metaEl);
  div.appendChild(textEl);
  div._textEl = textEl;
  div._metaEl = metaEl;
  div._meta = meta ? { ...meta } : {};
  transcriptEl.appendChild(div);
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
  return div;
}

function setBubbleText(div, text) {
  if (div && div._textEl) div._textEl.textContent = text;
  else if (div) div.textContent = text;
}

function updateBubbleMeta(div, patch) {
  if (!div || !div._metaEl) return;
  div._meta = { ...(div._meta || {}), ...patch };
  const label = composeMetaLabel(div._meta);
  div._metaEl.textContent = label;
  div._metaEl.style.display = label ? "" : "none";
}

function recordEntry(role, text, extra) {
  if (!text) return;
  const entry = { role, text, ts: Date.now() / 1000 };
  if (extra && typeof extra.latencyMs === "number" && extra.latencyMs > 0) {
    entry.latencyMs = extra.latencyMs;
  }
  clientEntries.push(entry);
}

// Render a clearly-broken tool result distinctly. Helps Gary see at a glance
// that the realtime model would have hallucinated otherwise.
function isErrorAnswer(answer) {
  if (!answer) return true;
  return typeof answer === "string" && answer.startsWith("Agent is temporarily unreachable");
}

// --------------- Icebreaker (comfort bed during ask_agent) ---------------

function createBrownNoiseIcebreaker(ctx) {
  const len = ctx.sampleRate * 2;
  const buf = ctx.createBuffer(1, len, ctx.sampleRate);
  const ch = buf.getChannelData(0);
  let last = 0;
  for (let i = 0; i < len; i++) {
    const white = Math.random() * 2 - 1;
    last = (last + 0.02 * white) / 1.02;
    ch[i] = last * 3.5;
  }
  const src = ctx.createBufferSource();
  src.buffer = buf; src.loop = true;
  const lp = ctx.createBiquadFilter();
  lp.type = "lowpass"; lp.frequency.value = 200; lp.Q.value = 0.7;
  const master = ctx.createGain(); master.gain.value = 0;
  const lfo = ctx.createOscillator(); lfo.frequency.value = 0.3;
  const lfoGain = ctx.createGain(); lfoGain.gain.value = 0.05;
  src.connect(lp).connect(master).connect(ctx.destination);
  lfo.connect(lfoGain).connect(master.gain);
  src.start(); lfo.start();
  const fade = (target, dur = 0.25) =>
    master.gain.linearRampToValueAtTime(target, ctx.currentTime + dur);
  return {
    fadeIn: () => fade(0.35),
    fadeOut: () => fade(0.0),
    dispose: () => {
      try { src.stop(); lfo.stop(); src.disconnect(); lp.disconnect(); master.disconnect(); lfoGain.disconnect(); } catch {}
    },
  };
}

function createSampleIcebreaker(ctx, buffer) {
  const src = ctx.createBufferSource();
  src.buffer = buffer; src.loop = true;
  const master = ctx.createGain(); master.gain.value = 0;
  src.connect(master).connect(ctx.destination);
  src.start();
  // setTargetAtTime with τ=dur/3 reaches ~95% of target in `dur` seconds —
  // exponential decay, no audible dead-stop at the endpoint.
  const fade = (target, dur = 0.4) =>
    master.gain.setTargetAtTime(target, ctx.currentTime, dur / 3);
  return {
    fadeIn: () => fade(0.32),
    fadeOut: () => fade(0.0),
    dispose: () => {
      setTimeout(() => { try { src.stop(); src.disconnect(); master.disconnect(); } catch {} }, 600);
    },
  };
}

function maybeSwapToSampleIcebreaker() {
  if (!audioCtx || !ambientBuffer || !icebreaker) return;
  if (activeToolCalls !== 0) return;  // bed is currently faded in; defer
  const old = icebreaker;
  icebreaker = createSampleIcebreaker(audioCtx, ambientBuffer);
  old.dispose();
  logClientEvent("client_icebreaker_source", { kind: "sample" });
}

// --------------- Health ---------------

async function checkHealth() {
  try {
    const r = await fetch("/api/health");
    const d = await r.json();
    if (d.ok && d.agent && d.openai_key) { setDot("ok"); talkBtn.disabled = false; return true; }
    setDot("bad");
    setStatus(d.agent ? "no openai key" : "agent is down", "error");
    talkBtn.disabled = true;
    return false;
  } catch (e) {
    setDot("bad"); setStatus("backend unreachable", "error"); talkBtn.disabled = true; return false;
  }
}

// --------------- Realtime peer (extracted so resume can rebuild it) ---------------

async function attachPeer(session, resumeContext) {
  const ephemeral = session.session.client_secret.value;
  const model = session.session.model || "gpt-realtime";
  // Reset for the brown-noise bridge: flips true when first audio frame arrives.
  firstAudioFrameSeen = false;

  const newPc = new RTCPeerConnection();
  newPc.onconnectionstatechange = () => onConnectionStateChange(newPc);
  newPc.oniceconnectionstatechange = () => {
    // Surface a status hint but treat connectionState as authoritative.
    const s = newPc.iceConnectionState;
    if (s === "failed") setStatus("connection lost", "error");
  };
  const audioEl = document.createElement("audio");
  audioEl.autoplay = true;
  audioEl.playsInline = true;
  newPc.ontrack = (ev) => {
    audioEl.srcObject = ev.streams[0];
    firstAudioFrameSeen = true;
  };
  micStream.getTracks().forEach((t) => newPc.addTrack(t, micStream));
  // Preserve mute state across resume -- attachPeer is called with the user's
  // existing micStream, but the new peer doesn't know they had toggled mute.
  if (micMuted) {
    micStream.getAudioTracks().forEach((t) => { t.enabled = false; });
  }
  const newDc = newPc.createDataChannel("oai-events");
  newDc.addEventListener("open", () => onDcOpen(resumeContext));
  newDc.addEventListener("message", onDcMessage);

  setStatus(resumeContext ? "resuming..." : "connecting...");
  const offer = await newPc.createOffer();
  await newPc.setLocalDescription(offer);
  const sdpResp = await fetch(`https://api.openai.com/v1/realtime?model=${encodeURIComponent(model)}`, {
    method: "POST",
    headers: { Authorization: `Bearer ${ephemeral}`, "Content-Type": "application/sdp", "OpenAI-Beta": "realtime=v1" },
    body: offer.sdp,
  });
  if (!sdpResp.ok) throw new Error(`sdp ${sdpResp.status}: ${await sdpResp.text()}`);
  const answerSdp = await sdpResp.text();
  await newPc.setRemoteDescription({ type: "answer", sdp: answerSdp });

  // Swap globals only after success.
  pc = newPc;
  dc = newDc;
  sessionStartedAt = Date.now();
  schedulePremint();
  startConnectionWatchdog();
}

async function startCall() {
  if (started) return;
  talkBtn.disabled = true;
  setStatus("requesting mic...");
  try {
    micStream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }, video: false });
  } catch (e) {
    setStatus("mic denied", "error");
    talkBtn.disabled = false;
    appendBubble("system", "Microphone permission was denied. Enable it in browser settings.");
    return;
  }
  setStatus("minting session...");
  let session;
  try {
    const r = await fetch("/api/session", { method: "POST" });
    if (!r.ok) throw new Error(`session ${r.status}`);
    session = await r.json();
  } catch (e) {
    setStatus("session mint failed", "error");
    appendBubble("system", `Session mint failed: ${e.message}. Tap Talk to retry.`);
    talkBtn.disabled = false;
    return;
  }
  convId = session.conv_id;
  lastSessionConfig = session.session || null;
  try {
    await attachPeer(session, null);
  } catch (e) {
    setStatus("connect failed", "error");
    appendBubble("system", `WebRTC connect failed: ${e.message}`);
    cleanupCall();
    talkBtn.disabled = false;
    return;
  }

  // AudioContext + comfort bed during ask_agent waits. Brown noise is the default
  // (procedural, always available); if /static/ambient.mp3 is served, we swap to
  // it transparently once the buffer decodes and the bed is idle.
  audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  icebreaker = createBrownNoiseIcebreaker(audioCtx);
  fetch("/static/ambient.mp3", { cache: "no-cache" })
    .then((r) => (r.ok ? r.arrayBuffer() : Promise.reject(new Error("no ambient asset"))))
    .then((buf) => audioCtx.decodeAudioData(buf))
    .then((decoded) => { ambientBuffer = decoded; maybeSwapToSampleIcebreaker(); })
    .catch(() => { logClientEvent("client_icebreaker_source", { kind: "brown_noise" }); });

  started = true;
  endBtn.hidden = false;
  muteBtn.disabled = false;
  setDot("ok");
  setStatus("connected", "live");
}

function onDcOpen(resumeContext) {
  // Re-assert session config over the data channel. Tools registered only at
  // ephemeral-mint time are silently dropped by gpt-realtime in some flows
  // (documented community pattern); session.update on dc-open is the canonical
  // fix. NOTE: omit `voice` -- it is locked after first audio response and
  // including it on resume tears the session down. Strip nulls (the mint
  // endpoint accepts null fields like input_audio_transcription.language but
  // session.update rejects them as invalid_type).
  const cfg = lastSessionConfig || {};
  // Append handoff-note continuation suffix into instructions BEFORE building
  // the session.update patch -- single source of truth for resume continuity.
  let resumeInstructions = cfg.instructions;
  if (resumeContext?.handoff_note) {
    resumeInstructions = (cfg.instructions || "") +
      "\n\nContinuation context from earlier in this same call: " +
      resumeContext.handoff_note +
      "\n\nCritical: do not greet, do not acknowledge any pause, do not say " +
      "'as I was saying' or similar filler. Continue exactly where you left " +
      "off in topic and tone. Wait for the user's next utterance before responding.";
  }
  if (cfg.tools || cfg.instructions) {
    const stripNulls = (obj) => {
      if (!obj || typeof obj !== "object") return obj;
      const out = {};
      for (const [k, v] of Object.entries(obj)) {
        if (v !== null && v !== undefined) out[k] = v;
      }
      return out;
    };
    const sessionPatch = {
      modalities: cfg.modalities || ["audio", "text"],
      instructions: resumeInstructions,
      tools: cfg.tools || [],
      tool_choice: cfg.tool_choice || "auto",
      turn_detection: cfg.turn_detection,
      input_audio_noise_reduction: cfg.input_audio_noise_reduction,
      input_audio_format: cfg.input_audio_format,
      output_audio_format: cfg.output_audio_format,
      input_audio_transcription: stripNulls(cfg.input_audio_transcription),
      max_response_output_tokens: cfg.max_response_output_tokens,
    };
    send({ type: "session.update", session: stripNulls(sessionPatch) });
  }
  logClientEvent("client_dc_opened", {
    tools_in_session: (cfg.tools || []).map((t) => t.name),
    tool_choice: cfg.tool_choice || null,
    instructions_chars: (resumeInstructions || "").length,
    resume: !!resumeContext,
    session_update_sent: !!(cfg.tools || cfg.instructions),
    handoff_note_chars: (resumeContext?.handoff_note || "").length,
    recent_entries: (resumeContext?.recent_entries || []).length,
  });

  if (!resumeContext) {
    send({
      type: "response.create",
      response: { modalities: ["audio", "text"], instructions: "Greet the user briefly in English. Just one sentence." },
    });
    return;
  }
  // Replay recent transcript as conversation history BEFORE the completed-loop
  // dedupe reads `deliveredTaskIds`. This restores the surrounding chitchat
  // context so the model isn't amnesic about topic and tone.
  const recent = resumeContext.recent_entries || [];
  for (const entry of recent) {
    if (entry.task_id) deliveredTaskIds.add(entry.task_id);
    if (entry.role !== "user" && entry.role !== "assistant") continue;
    send({
      type: "conversation.item.create",
      item: {
        type: "message", role: entry.role,
        content: [{ type: entry.role === "user" ? "input_text" : "text", text: entry.text }],
      },
    });
  }
  if (recent.length) {
    logClientEvent("client_resume_transcript_replayed", { count: recent.length });
  }
  // Resume path: replay any answers the user missed as a normal user/assistant
  // pair (NOT function_call_output -- the call_id belonged to the dead session
  // and Realtime would reject it).
  const completed = resumeContext.completed_while_away || [];
  for (const item of completed) {
    if (deliveredTaskIds.has(item.task_id)) continue;
    deliveredTaskIds.add(item.task_id);
    send({
      type: "conversation.item.create",
      item: { type: "message", role: "user", content: [{ type: "input_text", text: `(replayed from before the pause) ${item.question}` }] },
    });
    send({
      type: "conversation.item.create",
      item: { type: "message", role: "assistant", content: [{ type: "text", text: item.answer }] },
    });
    appendBubble("tool", `↻ carried over: ${item.question}`);
    appendBubble("tool", `← ${item.answer}`);
  }
  // When handoff_note is present, the instructions suffix carries the full
  // continuity signal. Skip the heuristic primer + response.create entirely;
  // semantic_vad will trigger the next response on the user's next utterance.
  if (!resumeContext.handoff_note) {
    let primer;
    if (completed.length > 0) {
      primer = "User just got back from a brief pause. The agent finished work while they were away. Greet them in one short sentence and offer to walk through the answer(s).";
    } else {
      primer = "User just got back from a brief pause. Greet them in one short sentence and continue naturally.";
    }
    send({
      type: "conversation.item.create",
      item: { type: "message", role: "system", content: [{ type: "input_text", text: primer }] },
    });
    send({ type: "response.create" });
  }
  // Brown-noise bridge fade-out: poll for first audio frame, fade when seen.
  // Hard cap at 5s so we don't keep the bed playing if no audio ever arrives.
  if (icebreaker) {
    const start = Date.now();
    const poll = setInterval(() => {
      if (firstAudioFrameSeen || Date.now() - start > 5000) {
        clearInterval(poll);
        try { icebreaker.fadeOut(); } catch {}
        logClientEvent("client_resume_first_audio", {
          since_dc_open_ms: Date.now() - start,
          saw_audio: firstAudioFrameSeen,
        });
      }
    }, 100);
  }

  // Reattach long-polls for tasks that are still running on the server.
  const stillWorking = resumeContext.still_working || [];
  for (const t of stillWorking) {
    if (deliveredTaskIds.has(t.task_id)) continue;
    appendBubble("tool", `🧠 still working on: ${t.question} (${t.elapsed_s}s)`);
    pollAgentTask(null, t.task_id, t.question);
  }

  appendBubble("system", `↻ Resumed${completed.length ? ` · ${completed.length} answer(s) ready` : ""}${stillWorking.length ? ` · ${stillWorking.length} still working` : ""}`);
}

// --------------- Connection monitoring (5G <-> wifi, backgrounding) ---------------

function startConnectionWatchdog() {
  if (watchdogTimer) clearInterval(watchdogTimer);
  let lastInbound = null;
  let lastInboundChangeTs = Date.now();
  watchdogTimer = setInterval(async () => {
    if (!pc || pc.connectionState !== "connected") return;
    try {
      const stats = await pc.getStats();
      let inbound = 0;
      stats.forEach((s) => {
        if (s.type === "inbound-rtp" && s.kind === "audio") inbound += s.bytesReceived || 0;
      });
      if (lastInbound === null) { lastInbound = inbound; return; }
      if (inbound !== lastInbound) {
        lastInbound = inbound;
        lastInboundChangeTs = Date.now();
      } else if (Date.now() - lastInboundChangeTs > 12_000) {
        // 12 s with state="connected" but no new inbound bytes => silent freeze.
        console.warn("watchdog: silent freeze, triggering resume");
        lastInboundChangeTs = Date.now();
        triggerResume("silent_freeze");
      }
    } catch {}
  }, 5_000);
}

function onConnectionStateChange(targetPc) {
  if (targetPc !== pc) return; // stale callback from a torn-down peer
  const s = pc.connectionState;
  if (s === "connected") {
    connDownSinceTs = null;
    setStatus("connected", "live");
    return;
  }
  if (s === "failed" || s === "disconnected") {
    if (connDownSinceTs === null) connDownSinceTs = Date.now();
    setStatus("link unstable", "error");
    setTimeout(() => {
      if (!pc || pc !== targetPc) return;
      const stillBad = pc.connectionState === "failed" || pc.connectionState === "disconnected";
      if (stillBad && connDownSinceTs && Date.now() - connDownSinceTs >= 3_000) {
        triggerResume("peer_" + pc.connectionState);
      }
    }, 3_200);
  }
}

// Ask the dying Realtime model for an ≤80-word continuation note for its
// successor. Skipped if a response is already in flight (Realtime allows only
// one). Resolves with the captured text (full or partial) or null on hard
// timeout. Fire-and-forget POST to /api/handoff-note.
async function requestHandoffNote(reason) {
  if (!dc || dc.readyState !== "open" || responseInFlight) {
    logClientEvent("client_handoff_note_skipped", { reason, response_in_flight: responseInFlight });
    return null;
  }
  const t0 = performance.now();
  let resolveDone;
  const donePromise = new Promise((r) => { resolveDone = r; });
  activeHandoffRequest = { responseId: null, buffer: "", resolve: resolveDone, t0 };
  try {
    dc.send(JSON.stringify({
      type: "conversation.item.create",
      item: {
        type: "message", role: "system",
        content: [{ type: "input_text", text:
          "You are about to be paused mid-call. In ≤80 words write a continuation " +
          "note for your successor session: user's name and current situation, " +
          "current topic, tonal observations from this call, anything pending. " +
          "No greeting, no flattery, no filler. Output only the note." }],
      },
    }));
    dc.send(JSON.stringify({ type: "response.create", response: { modalities: ["text"] } }));
  } catch (e) {
    activeHandoffRequest = null;
    logClientEvent("client_handoff_note_failed", { reason, error: String(e) });
    return null;
  }
  // Race the model's response against a 2.5s deadline. On timeout, use whatever
  // partial buffer we accumulated -- a half-formed note still beats nothing.
  const note = await Promise.race([
    donePromise,
    new Promise((r) => setTimeout(() => r(activeHandoffRequest?.buffer || null), 2500)),
  ]);
  const ms = Math.round(performance.now() - t0);
  activeHandoffRequest = null;
  const trimmed = (note || "").trim();
  if (trimmed) {
    fetch("/api/handoff-note", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conv_id: convId, note: trimmed, generated_in_ms: ms }),
    }).catch(() => {});
    logClientEvent("client_handoff_note_generated", { reason, chars: trimmed.length, generated_in_ms: ms });
  } else {
    logClientEvent("client_handoff_note_failed", { reason, deadline_ms: 2500, generated_in_ms: ms });
  }
  return trimmed || null;
}

// Tear down the audio comfort layer. Called only on explicit End -- forced
// reconnects skip this so the brown-noise bed bridges the silent gap.
function endIcebreaker() {
  if (icebreaker) { try { icebreaker.fadeOut(); icebreaker.dispose(); } catch {} icebreaker = null; }
  ambientBuffer = null;
  if (audioCtx) { audioCtx.close().catch(() => {}); audioCtx = null; }
}

// 60-min cap handling: pre-mint a fresh ephemeral at 58:30, then swap into it
// at min(59:00, expires_at − 10s). The user never hears the seam.
const PREMINT_AT_MS = 58 * 60 * 1000 + 30 * 1000;   // 58:30
const HARD_CAP_AT_MS = 59 * 60 * 1000;              // 59:00 absolute

function schedulePremint() {
  if (premintTimer) clearTimeout(premintTimer);
  if (capSwapTimer) clearTimeout(capSwapTimer);
  premintTimer = setTimeout(doPremint, PREMINT_AT_MS);
}

async function doPremint() {
  if (!started || !convId) return;
  try {
    const r = await fetch("/api/premint-session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conv_id: convId }),
    });
    const data = await r.json();
    if (!data?.session?.client_secret?.value) {
      logClientEvent("client_premint_failed", { reason: "no_client_secret" });
      return;
    }
    premintedSession = data;
    premintedExpiresAt = data.session.client_secret.expires_at || null;
    const hardCapAbs = (sessionStartedAt || Date.now()) + HARD_CAP_AT_MS;
    const expiryAbs = premintedExpiresAt ? premintedExpiresAt * 1000 - 10_000 : hardCapAbs;
    const swapAtAbs = Math.min(hardCapAbs, expiryAbs);
    const swapInMs = Math.max(0, swapAtAbs - Date.now());
    capSwapTimer = setTimeout(gracefulCapSwap, swapInMs);
    logClientEvent("client_premint_started", {
      expires_at: premintedExpiresAt,
      swap_in_ms: swapInMs,
    });
  } catch (e) {
    logClientEvent("client_premint_failed", { error: String(e) });
  }
}

async function gracefulCapSwap() {
  if (!started || !convId) return;
  if (premintedExpiresAt && premintedExpiresAt < Date.now() / 1000 + 5) {
    premintedSession = null;
    premintedExpiresAt = null;
    logClientEvent("client_premint_expired", {});
    return triggerResume("session_cap_fallback");
  }
  if (resumeInFlight) return;
  resumeInFlight = true;
  const t0 = performance.now();
  try {
    if (icebreaker) try { icebreaker.fadeIn(); } catch {}
    await requestHandoffNote("session_cap");
    pollAbortGen++;
    activeToolCalls = 0;
    // Claim missed work via a regular /api/resume call (this also marks any
    // completed_while_away tasks delivered, so no double-replay).
    const r = await fetch("/api/resume", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conv_id: convId }),
    });
    const claim = await r.json();
    if (claim.expired) {
      premintedSession = null;
      premintedExpiresAt = null;
      appendBubble("system", "Session expired. Tap Talk to start a new one.");
      cleanupCall();
      return;
    }
    if (dc) { try { dc.close(); } catch {} dc = null; }
    if (pc) { try { pc.close(); } catch {} pc = null; }
    // Use the pre-minted session for transport, but resume context (handoff
    // note, recent_entries, completed_while_away, still_working) from claim.
    const transport = { ...claim, session: premintedSession.session };
    lastSessionConfig = premintedSession.session;
    premintedSession = null;
    premintedExpiresAt = null;
    await attachPeer(transport, claim);
    setStatus("connected", "live");
    logClientEvent("client_session_cap_swap", { swap_ms: Math.round(performance.now() - t0) });
  } catch (e) {
    premintedSession = null;
    premintedExpiresAt = null;
    logClientEvent("client_session_cap_swap_failed", { error: String(e) });
    triggerResume("session_cap_fallback");
  } finally {
    resumeInFlight = false;
  }
}

async function triggerResume(reason, { silent = false } = {}) {
  if (!started || !convId || resumeInFlight) return;
  resumeInFlight = true;
  if (!silent) setStatus("resuming...", "thinking");
  // Brown-noise bridge: keep the bed audible across the silent reconnect gap.
  if (icebreaker) try { icebreaker.fadeIn(); } catch {}
  try {
    // Generate a continuation note while the dying dc is still open. Best
    // effort -- bounded by 2.5s and gated on responseInFlight.
    await requestHandoffNote(reason);
    // Abort any in-flight pollAgentTask loops bound to the dying call_ids;
    // /api/resume's still_working list will re-attach them with callId=null.
    pollAbortGen++;
    activeToolCalls = 0;
    // Tear down the dead peer (don't call cleanupCall -- we want to keep
    // started=true, clientEntries, deliveredTaskIds, and the icebreaker bed
    // intact across the gap).
    if (dc) { try { dc.close(); } catch {} dc = null; }
    if (pc) { try { pc.close(); } catch {} pc = null; }
    const r = await fetch("/api/resume", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conv_id: convId }),
    });
    const data = await r.json();
    if (data.expired) {
      appendBubble("system", "Session expired while away. Tap Talk to start a new one.");
      cleanupCall();
      return;
    }
    lastSessionConfig = data.session || null;
    await attachPeer(data, data);
    setStatus("connected", "live");
  } catch (e) {
    appendBubble("system", `Resume failed: ${e.message}. Tap Talk to retry.`);
    cleanupCall();
  } finally {
    resumeInFlight = false;
  }
}

// --------------- Data channel events ---------------

let assistantTextBuf = "";
let userTranscriptBuf = "";

const fnCallBuffers = new Map(); // call_id -> {name, args}

function onDcMessage(ev) {
  let msg;
  try { msg = JSON.parse(ev.data); } catch { return; }
  switch (msg.type) {
    case "session.created":
    case "session.updated":
      break;

    case "response.created": {
      responseInFlight = true;
      // Bind any pending handoff request to this response.id so the
      // text.delta/done events route to its buffer.
      if (activeHandoffRequest && !activeHandoffRequest.responseId) {
        activeHandoffRequest.responseId = msg.response?.id || null;
      }
      break;
    }

    case "response.text.delta": {
      if (activeHandoffRequest && msg.response_id === activeHandoffRequest.responseId) {
        activeHandoffRequest.buffer += msg.delta || "";
      }
      break;
    }

    case "response.text.done": {
      if (activeHandoffRequest && msg.response_id === activeHandoffRequest.responseId) {
        activeHandoffRequest.resolve(activeHandoffRequest.buffer);
      }
      break;
    }

    case "response.audio_transcript.delta":
      assistantTextBuf += msg.delta || "";
      if (!pendingAssistantBubble) {
        const startedAt = Date.now();
        if (turnTiming.firstTokenTs === null) turnTiming.firstTokenTs = startedAt;
        pendingAssistantBubble = appendBubble("assistant", "", { createdAt: startedAt });
      }
      setBubbleText(pendingAssistantBubble, assistantTextBuf);
      transcriptEl.scrollTop = transcriptEl.scrollHeight;
      break;

    case "response.audio_transcript.done": {
      const completedAt = Date.now();
      const userDoneTs = turnTiming.userDoneTs;
      const firstTokenTs = turnTiming.firstTokenTs;
      const latencyMs = userDoneTs ? completedAt - userDoneTs : undefined;
      const firstTokenLatencyMs = userDoneTs && firstTokenTs ? firstTokenTs - userDoneTs : undefined;
      if (pendingAssistantBubble) {
        updateBubbleMeta(pendingAssistantBubble, { latencyMs, firstTokenLatencyMs });
      }
      if (assistantTextBuf) { recordEntry("assistant", assistantTextBuf, { latencyMs }); }
      assistantTextBuf = "";
      pendingAssistantBubble = null;
      turnTiming.userDoneTs = null;
      turnTiming.firstTokenTs = null;
      break;
    }

    case "conversation.item.input_audio_transcription.delta":
      userTranscriptBuf += msg.delta || "";
      break;

    case "conversation.item.input_audio_transcription.completed": {
      const text = (msg.transcript || userTranscriptBuf || "").trim();
      userTranscriptBuf = "";
      if (text) {
        const userDoneTs = Date.now();
        turnTiming.userDoneTs = userDoneTs;
        turnTiming.firstTokenTs = null;
        appendBubble("user", text, { createdAt: userDoneTs });
        recordEntry("user", text);
      }
      break;
    }

    case "response.output_item.added": {
      const item = msg.item || {};
      if (item.type === "function_call") {
        fnCallBuffers.set(item.call_id, { name: item.name, args: "" });
        hadFunctionCallThisResponse = true;
        logClientEvent("client_function_call_arrived", { name: item.name, call_id: item.call_id });
        if (item.name === "ask_agent") {
          activeToolCalls++;
          appendBubble("tool", "thinking...");
          setStatus("agent is thinking...", "thinking");
          setDot("thinking");
          if (icebreaker) icebreaker.fadeIn();
        }
      }
      break;
    }

    case "response.function_call_arguments.delta": {
      const buf = fnCallBuffers.get(msg.call_id);
      if (buf) buf.args += msg.delta || "";
      break;
    }

    case "response.function_call_arguments.done": {
      const buf = fnCallBuffers.get(msg.call_id);
      const name = buf?.name || msg.name;
      let args = {};
      try { args = JSON.parse(buf?.args || msg.arguments || "{}"); } catch {}
      fnCallBuffers.delete(msg.call_id);
      if (name === "ask_agent") {
        handleAskAgent(msg.call_id, args.question || "");
      } else {
        sendFunctionOutput(msg.call_id, JSON.stringify({ error: `unknown tool ${name}` }));
      }
      break;
    }

    case "response.done":
      responseInFlight = false;
      logClientEvent("client_response_done", {
        had_function_call: hadFunctionCallThisResponse,
        response_id: msg.response?.id || null,
        output_item_count: Array.isArray(msg.response?.output) ? msg.response.output.length : null,
      });
      hadFunctionCallThisResponse = false;
      break;

    case "error": {
      const code = msg.error?.code;
      logClientEvent("client_realtime_error", {
        code,
        err_type: msg.error?.type,
        message: msg.error?.message,
        event_id: msg.event_id,
        error: msg.error || msg,
      });
      if (code === "session_expired") {
        // OpenAI hard-capped the session (15/30/60 min depending on rollout).
        // Rotate silently through the existing resume seam.
        triggerResume("openai_session_expired", { silent: true });
        break;
      }
      console.error("realtime error:", msg);
      setStatus("realtime error", "error");
      break;
    }

    default:
      logClientEvent("client_dc_unhandled_event", { type: msg.type });
      break;
  }
}

function send(obj) {
  if (dc && dc.readyState === "open") dc.send(JSON.stringify(obj));
}
function sendFunctionOutput(call_id, output) {
  send({ type: "conversation.item.create", item: { type: "function_call_output", call_id, output } });
  send({ type: "response.create" });
}

// Spawn an agent task on the server and long-poll until done. Survives
// backgrounding -- if the browser tab dies, the task keeps running on the
// server and the answer is replayed via /api/resume's `completed_while_away`.
async function handleAskAgent(callId, question) {
  let taskId = null;
  let answer = "";
  appendBubble("tool", `→ ${question}`);
  try {
    const spawn = await fetch("/api/ask-agent", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conv_id: convId, question, call_id: callId }),
    });
    const spawnData = await spawn.json();
    taskId = spawnData.task_id || null;
    if (spawnData.status && spawnData.status !== "running") {
      // Inline fast-path -- answer is already here.
      answer = spawnData.answer || "";
    } else if (taskId) {
      answer = await pollAgentTask(callId, taskId, question);
    } else {
      answer = spawnData.answer || "(no answer)";
    }
  } catch (e) {
    answer = `Agent is temporarily unreachable - ${e.message}`;
  } finally {
    activeToolCalls = Math.max(0, activeToolCalls - 1);
    if (activeToolCalls === 0) {
      setStatus("connected", "live");
      setDot("ok");
      if (icebreaker) icebreaker.fadeOut();
      // If the Suno asset finished decoding mid-call, the swap was deferred.
      // Now that the bed is idle, retry — silent because both sit at gain 0.
      maybeSwapToSampleIcebreaker();
    }
  }
  if (taskId) deliveredTaskIds.add(taskId);
  // Render answer; flag failures distinctly so Gary can tell at a glance.
  appendBubble(isErrorAnswer(answer) ? "system" : "tool", `← ${answer || "(no answer)"}`);
  // Always feed something back to the realtime peer so it doesn't hang on the
  // function call. Structured-error sentinel triggers prompt rule #10.
  sendFunctionOutput(callId, answer || "Agent is temporarily unreachable - please ask the user to repeat that.");
}

// Poll loop reused by handleAskAgent and resume's still_working reattach.
// callId may be null on resume -- we still update the transcript/UI even when
// there's no live realtime peer to feed.
async function pollAgentTask(callId, taskId, question) {
  // Capture the abort generation at spawn time. triggerResume bumps this
  // counter so stale polls bound to the dying call_id self-terminate; the
  // resume's still_working list re-attaches them with callId=null.
  const myGen = pollAbortGen;
  while (true) {
    if (!convId) return "";
    if (myGen !== pollAbortGen) return "";
    let data;
    try {
      const r = await fetch(`/api/agent-task/${encodeURIComponent(taskId)}?conv_id=${encodeURIComponent(convId)}`);
      data = await r.json();
    } catch (e) {
      // Network blip during long-poll. Brief retry; resume flow handles real failures.
      await new Promise((res) => setTimeout(res, 1500));
      continue;
    }
    if (data.status === "running") continue;
    if (myGen !== pollAbortGen) return "";
    deliveredTaskIds.add(taskId);
    const answer = data.answer || "";
    if (callId === null) {
      // Resume-attached poll: render in transcript and feed to whichever peer
      // is currently live (if any) as a normal assistant message.
      appendBubble("tool", `→ (carried over) ${question}`);
      appendBubble(isErrorAnswer(answer) ? "system" : "tool", `← ${answer || "(no answer)"}`);
      send({
        type: "conversation.item.create",
        item: { type: "message", role: "assistant", content: [{ type: "text", text: answer }] },
      });
      send({ type: "response.create" });
    }
    return answer;
  }
}

// --------------- Mute / End / Text mode ---------------

muteBtn.addEventListener("click", () => {
  if (!micStream) return;
  micMuted = !micMuted;
  micStream.getAudioTracks().forEach((t) => (t.enabled = !micMuted));
  muteBtn.textContent = micMuted ? "\u{1F399}" : "\u{1F507}";
});

endBtn.addEventListener("click", endCall);

function endCallBeacon(reason) {
  if (!convId) return;
  const cid = convId;
  const entries = clientEntries.slice();
  try {
    fetch("/api/end", {
      method: "POST",
      keepalive: true,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conv_id: cid, entries, reason }),
    }).catch(() => {});
  } catch {}
}

async function endCall() {
  if (!started) return;
  setStatus("ending...");
  const cid = convId;
  const entries = clientEntries.slice();
  cleanupCall();
  try {
    await fetch("/api/end", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conv_id: cid, entries, reason: "user_click" }),
    });
    setStatus("idle");
    appendBubble("system", "Call ended. Transcript saved.");
  } catch (e) {
    appendBubble("system", `Call ended (transcript persist failed: ${e.message})`);
  }
}

// Backgrounding is now a PAUSE, not an end. We only post /api/end on a real
// teardown (pagehide) or the explicit End button. visibilitychange just marks
// the timeline and triggers a resume on return.
document.addEventListener("visibilitychange", () => {
  if (!started) return;
  if (document.visibilityState === "hidden") {
    clientEntries.push({ role: "system", text: "(paused -- app backgrounded)", ts: Date.now() / 1000 });
  } else if (document.visibilityState === "visible") {
    if (!pc || pc.connectionState !== "connected") {
      triggerResume("visibility_visible");
    }
  }
});
window.addEventListener("pagehide", () => {
  if (started) endCallBeacon("pagehide");
});

function cleanupCall() {
  started = false;
  endBtn.hidden = true;
  muteBtn.disabled = true;
  micMuted = false;
  muteBtn.textContent = "\u{1F507}";
  if (watchdogTimer) { clearInterval(watchdogTimer); watchdogTimer = null; }
  if (premintTimer) { clearTimeout(premintTimer); premintTimer = null; }
  if (capSwapTimer) { clearTimeout(capSwapTimer); capSwapTimer = null; }
  endIcebreaker();
  if (dc) { try { dc.close(); } catch {} dc = null; }
  if (pc) { try { pc.close(); } catch {} pc = null; }
  if (micStream) { micStream.getTracks().forEach((t) => t.stop()); micStream = null; }
  talkBtn.disabled = false;
  setDot("ok");
  clientEntries.length = 0;
  deliveredTaskIds.clear();
  turnTiming.userDoneTs = null;
  turnTiming.firstTokenTs = null;
  connDownSinceTs = null;
  resumeInFlight = false;
  responseInFlight = false;
  activeHandoffRequest = null;
  premintedSession = null;
  premintedExpiresAt = null;
  sessionStartedAt = null;
  firstAudioFrameSeen = false;
  convId = null;
}

// Text mode
textToggle.addEventListener("click", () => {
  textMode.hidden = !textMode.hidden;
  if (!textMode.hidden) textInput.focus();
});
textForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const text = textInput.value.trim();
  if (!text) return;
  if (!convId) {
    try {
      const r = await fetch("/api/session", { method: "POST" });
      const d = await r.json();
      convId = d.conv_id;
    } catch (e) {
      appendBubble("system", `Session mint failed: ${e.message}`);
      return;
    }
  }
  textInput.value = "";
  const submitTs = Date.now();
  appendBubble("user", text, { createdAt: submitTs });
  recordEntry("user_text", text);
  setStatus("agent is thinking...", "thinking");
  setDot("thinking");
  try {
    const r = await fetch("/api/text-turn", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conv_id: convId, text }),
    });
    const d = await r.json();
    const doneTs = Date.now();
    const latencyMs = doneTs - submitTs;
    const ans = d.answer || "(no answer)";
    appendBubble(isErrorAnswer(ans) ? "system" : "assistant", ans, { createdAt: doneTs, latencyMs });
    recordEntry("assistant_text", d.answer || "", { latencyMs });
  } catch (e) {
    appendBubble("system", `Error: ${e.message}`);
  } finally {
    setStatus(started ? "connected" : "idle", started ? "live" : "");
    setDot("ok");
  }
});

// --------------- Init ---------------

talkBtn.addEventListener("click", startCall);
checkHealth();
setInterval(checkHealth, 10000);
