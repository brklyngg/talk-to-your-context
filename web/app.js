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
let icebreaker = null;     // {fadeIn, fadeOut, dispose} — comfort bed for the
                           // peerless gap during cap-swap and forced-reconnect.
                           // Scope shrunk in May 2026: gpt-realtime-2's native
                           // preambles + async function calling fill in-call
                           // tool waits, so the icebreaker no longer fires on
                           // ask_agent — only when we genuinely have no peer.
let ambientBuffer = null;  // decoded AudioBuffer cached when /static/ambient.mp3 served
const clientEntries = [];  // {role, text, ts, latencyMs?} for end-of-call upload
let pendingAssistantBubble = null;
const turnTiming = { userDoneTs: null, firstTokenTs: null };

// Connection-state debounce (5G <-> wifi handover, transient blips)
let connDownSinceTs = null;
let resumeInFlight = false;
let watchdogTimer = null;

// --- Forced-reconnect continuity state ---
// `responseInFlight` flips on response.created and back on response.done. The
// handoff-note request is gated on it being false (one outstanding response at
// a time per Realtime contract).
let responseInFlight = false;
// Single in-flight handoff request: { responseId, buffer, resolve, t0 }.
// `responseId` is null until response.created arrives, then bound to the real id
// so subsequent response.text.delta events route to the right buffer.
let activeHandoffRequest = null;
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

// Active tool-call status chips on the assistant's bubble. call_id -> {chipDiv, throttled}.
// Phrasing describes the action ("Consulting deep context: …"); never references
// "brain", "model", or "agent" — exposing the split-brain seam is bad UX.
const consultingChips = new Map();

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

// Tiny markdown renderer for displayed tool-answer bubbles. Supports lists,
// code spans, links, bold, italics, and line breaks — nothing else. Voice
// answers stay plain (the model's "no markdown" rule applies to spoken text).
function renderInlineMarkdown(text) {
  let out = String(text || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(^|[^*])\*([^*]+)\*(?!\*)/g, "$1<em>$2</em>");
  out = out.replace(/\[([^\]]+)\]\(([^)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener">$1</a>');
  return out;
}
function renderMarkdown(text) {
  if (!text) return "";
  const lines = String(text).split("\n");
  const out = [];
  let inList = false;
  for (const raw of lines) {
    const line = raw.trimEnd();
    const liMatch = line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)/);
    if (liMatch) {
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push(`<li>${renderInlineMarkdown(liMatch[1])}</li>`);
    } else {
      if (inList) { out.push("</ul>"); inList = false; }
      if (line) out.push(`<div>${renderInlineMarkdown(line)}</div>`);
      else out.push("<br>");
    }
  }
  if (inList) out.push("</ul>");
  return out.join("");
}

function appendBubble(role, text, meta, opts) {
  const div = document.createElement("div");
  div.className = "bubble " + role;
  const metaEl = document.createElement("div");
  metaEl.className = "bubble-meta";
  const label = composeMetaLabel(meta);
  metaEl.textContent = label;
  if (!label) metaEl.style.display = "none";
  const textEl = document.createElement("div");
  textEl.className = "bubble-text";
  if (opts && opts.renderMd) textEl.innerHTML = renderMarkdown(text);
  else textEl.textContent = text;
  div.appendChild(metaEl);
  div.appendChild(textEl);
  div._textEl = textEl;
  div._metaEl = metaEl;
  div._meta = meta ? { ...meta } : {};
  transcriptEl.appendChild(div);
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
  return div;
}

// --------------- Consulting status chip ---------------
// Subtle status indicator while the model is consulting deep context. Phrasing
// describes the action — never the architecture. The streaming args delta lets
// us live-render the topic as it forms, then freeze on .done, then fade when
// the answer arrives.
function showConsultingChip() {
  const div = document.createElement("div");
  div.className = "bubble consulting";
  div.style.opacity = "0.72";
  const textEl = document.createElement("div");
  textEl.className = "bubble-text";
  textEl.style.fontStyle = "italic";
  textEl.style.fontSize = "0.92em";
  textEl.textContent = "Consulting deep context…";
  div.appendChild(textEl);
  div._textEl = textEl;
  transcriptEl.appendChild(div);
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
  return div;
}
function updateConsultingChip(div, topic) {
  if (!div || !div._textEl) return;
  const t = (topic || "").trim();
  div._textEl.textContent = t ? `Consulting deep context: ${t}` : "Consulting deep context…";
}
function fadeOutConsultingChip(div) {
  if (!div) return;
  div.style.transition = "opacity 0.4s";
  div.style.opacity = "0";
  setTimeout(() => { try { div.remove(); } catch {} }, 500);
}
// Pull the question value from a partial JSON args buffer. Args stream in
// before the .done event, and partial JSON can't be parsed, so we extract
// the in-progress "question" string directly.
function extractStreamingQuestion(argsBuffer) {
  if (!argsBuffer) return null;
  const m = argsBuffer.match(/"question"\s*:\s*"((?:[^"\\]|\\.)*)/);
  if (!m) return null;
  return m[1].replace(/\\n/g, " ").replace(/\\"/g, '"').replace(/\\\\/g, "\\");
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
  if (resumeInFlight) return;  // bed may be faded in during cap-swap/resume; defer
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
  // GA endpoint (May 2026): /v1/realtime/calls. Ephemeral key encodes model
  // + session config from the mint, so no ?model= query param needed and
  // no OpenAI-Beta header.
  const sdpResp = await fetch("https://api.openai.com/v1/realtime/calls", {
    method: "POST",
    headers: { Authorization: `Bearer ${ephemeral}`, "Content-Type": "application/sdp" },
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
  // GA Realtime (May 2026): tools/instructions/audio config are baked into
  // the ephemeral at mint time and honored without a follow-up session.update.
  // The handoff-note continuation is spliced into instructions server-side
  // via _mint_realtime_session(instructions_suffix=...).
  logClientEvent("client_dc_opened", {
    resume: !!resumeContext,
    handoff_note_chars: (resumeContext?.handoff_note || "").length,
  });

  if (!resumeContext) {
    send({
      type: "response.create",
      response: { output_modalities: ["audio"], instructions: "Greet the user briefly in English. Just one sentence." },
    });
    return;
  }
  // Resume path: handoff-note instructions suffix carries continuity. semantic_vad
  // triggers the next response on the user's next utterance — no primer needed.
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
  appendBubble("system", "↻ Resumed");
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
    dc.send(JSON.stringify({ type: "response.create", response: { output_modalities: ["text"] } }));
  } catch (e) {
    activeHandoffRequest = null;
    logClientEvent("client_handoff_note_failed", { reason, error: String(e) });
    return null;
  }
  // Race the model's response against a 3.5s deadline (bumped from 2.5s for
  // gpt-realtime-2: GPT-5-class reasoning + 128K context lets the model write
  // a richer note when given the headroom). On timeout, use whatever partial
  // buffer we accumulated — a half-formed note still beats nothing.
  const HANDOFF_DEADLINE_MS = 3500;
  const note = await Promise.race([
    donePromise,
    new Promise((r) => setTimeout(() => r(activeHandoffRequest?.buffer || null), HANDOFF_DEADLINE_MS)),
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
    logClientEvent("client_handoff_note_failed", { reason, deadline_ms: HANDOFF_DEADLINE_MS, generated_in_ms: ms });
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
    // Claim continuity (handoff note baked into instructions server-side) via
    // the regular /api/resume call.
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
    // The pre-minted ephemeral was minted *before* the handoff note existed —
    // its baked-in instructions lack the continuation suffix. Trade-off: we
    // use the pre-minted ephemeral for transport (it's ready) and accept that
    // the model won't have the handoff-note context until next cap-swap. For
    // a richer continuity, fall back to the claim's freshly-minted ephemeral
    // (which DOES have the suffix) and discard the pre-mint. ~200ms slower
    // but preserves the handoff-note → instructions chain.
    const useFreshClaim = !!(claim.session?.client_secret?.value);
    const transport = useFreshClaim
      ? claim
      : { ...claim, session: premintedSession.session };
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
    // effort — bounded by the handoff deadline and gated on responseInFlight.
    await requestHandoffNote(reason);
    // Tear down the dead peer (don't call cleanupCall — we want to keep
    // started=true, clientEntries, and the icebreaker bed intact across the gap).
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
        // Phase 0 routing telemetry: 1 event per user turn. Server derives
        // local_answer_turns = user_turns - ask_agent_count. See
        // events.py:compute_routing_metrics.
        logClientEvent("client_user_turn", { chars: text.length });
      }
      break;
    }

    case "response.output_item.added": {
      const item = msg.item || {};
      if (item.type === "function_call") {
        fnCallBuffers.set(item.call_id, { name: item.name, args: "" });
        logClientEvent("client_function_call_arrived", { name: item.name, call_id: item.call_id });
        if (item.name === "ask_agent") {
          // Subtle status chip describing the action — phrasing intentionally
          // hides the split-brain seam (no "asking your brain" / "calling the
          // agent" framing).
          const chipDiv = showConsultingChip();
          consultingChips.set(item.call_id, { chipDiv, throttled: false });
        }
      }
      break;
    }

    case "response.function_call_arguments.delta": {
      const buf = fnCallBuffers.get(msg.call_id);
      if (buf) buf.args += msg.delta || "";
      // Throttle DOM updates via rAF; partial JSON args yield a partial topic.
      const entry = consultingChips.get(msg.call_id);
      if (entry && !entry.throttled) {
        entry.throttled = true;
        requestAnimationFrame(() => {
          entry.throttled = false;
          const topic = extractStreamingQuestion(buf?.args || "");
          updateConsultingChip(entry.chipDiv, topic);
        });
      }
      break;
    }

    case "response.function_call_arguments.done": {
      const buf = fnCallBuffers.get(msg.call_id);
      const name = buf?.name || msg.name;
      let args = {};
      try { args = JSON.parse(buf?.args || msg.arguments || "{}"); } catch {}
      fnCallBuffers.delete(msg.call_id);
      // Final, complete topic update on the chip.
      const entry = consultingChips.get(msg.call_id);
      if (entry && args.question) updateConsultingChip(entry.chipDiv, args.question);
      if (name === "ask_agent") {
        handleAskAgent(msg.call_id, args.question || "", {
          intent_type: typeof args.intent_type === "string" ? args.intent_type : null,
          freshness_required: typeof args.freshness_required === "boolean" ? args.freshness_required : null,
        });
      } else {
        sendFunctionOutput(msg.call_id, JSON.stringify({ error: `unknown tool ${name}` }));
      }
      break;
    }

    case "response.done":
      responseInFlight = false;
      logClientEvent("client_response_done", {
        response_id: msg.response?.id || null,
        output_item_count: Array.isArray(msg.response?.output) ? msg.response.output.length : null,
      });
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

// Synchronous forward to the backend agent. gpt-realtime-2's async function
// calling keeps the conversation flowing in the audio channel during the
// wait — no client-side polling, no icebreaker, no in-call status interrupt.
async function handleAskAgent(callId, question, routing) {
  let answer = "";
  const intentType = routing && typeof routing.intent_type === "string" ? routing.intent_type : null;
  const freshnessRequired = routing && typeof routing.freshness_required === "boolean" ? routing.freshness_required : null;
  try {
    const r = await fetch("/api/ask-agent", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        conv_id: convId,
        question,
        call_id: callId,
        intent_type: intentType,
        freshness_required: freshnessRequired,
      }),
    });
    const data = await r.json();
    answer = data.answer || "";
  } catch (e) {
    answer = `Agent is temporarily unreachable - ${e.message}`;
  } finally {
    // Fade out the consulting chip; remove from the active map.
    const entry = consultingChips.get(callId);
    if (entry) {
      fadeOutConsultingChip(entry.chipDiv);
      consultingChips.delete(callId);
    }
  }
  // Render answer with markdown for tool-answer bubbles. Voice answers stay
  // plain; this is display-only enrichment.
  if (isErrorAnswer(answer)) {
    appendBubble("system", answer || "(no answer)");
  } else {
    appendBubble("tool", answer || "(no answer)", null, { renderMd: true });
  }
  // Always feed something back to the realtime peer so it doesn't hang on the
  // function call. Structured-error sentinel triggers prompt rule #10.
  sendFunctionOutput(callId, answer || "Agent is temporarily unreachable - please ask the user to repeat that.");
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
  // Tear down any open consulting chips (no peer to receive answers anyway).
  for (const entry of consultingChips.values()) {
    try { fadeOutConsultingChip(entry.chipDiv); } catch {}
  }
  consultingChips.clear();
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
