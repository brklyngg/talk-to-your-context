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
let icebreaker = null;     // {fadeIn, fadeOut}
let activeToolCalls = 0;
const clientEntries = [];  // {role, text, ts, latencyMs?} for end-of-call upload
let pendingAssistantBubble = null;
// Per-turn timing. userDoneTs is set when the user's input is fully transcribed
// (the user-perceived "I'm done speaking" moment). firstTokenTs is set on the
// first assistant transcript delta. Both reset between turns.
const turnTiming = { userDoneTs: null, firstTokenTs: null };

// --------------- UI helpers ---------------

function setStatus(text, cls = "") {
  statusEl.textContent = text;
  statusEl.className = "status " + cls;
}
function setDot(cls) { dotEl.className = "dot " + cls; }

// --- timing label helpers (pure) ---
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

// --------------- Brown-noise icebreaker idle ---------------

function createIcebreakerIdle(ctx) {
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
  const fade = (target, dur = 0.25) => master.gain.linearRampToValueAtTime(target, ctx.currentTime + dur);
  return { fadeIn: () => fade(0.35), fadeOut: () => fade(0.0) };
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

// --------------- Realtime session ---------------

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
  const ephemeral = session.session.client_secret.value;
  const model = session.session.model || "gpt-realtime";

  // WebRTC
  pc = new RTCPeerConnection();
  pc.oniceconnectionstatechange = () => {
    const s = pc.iceConnectionState;
    if (s === "failed" || s === "disconnected") setStatus("connection lost", "error");
  };
  // Inbound audio -> <audio>
  const audioEl = document.createElement("audio");
  audioEl.autoplay = true;
  audioEl.playsInline = true;
  pc.ontrack = (ev) => { audioEl.srcObject = ev.streams[0]; };
  // Outbound mic
  micStream.getTracks().forEach((t) => pc.addTrack(t, micStream));
  // Data channel
  dc = pc.createDataChannel("oai-events");
  dc.addEventListener("open", onDcOpen);
  dc.addEventListener("message", onDcMessage);

  setStatus("connecting...");
  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);
  let answerSdp;
  try {
    const sdpResp = await fetch(`https://api.openai.com/v1/realtime?model=${encodeURIComponent(model)}`, {
      method: "POST",
      headers: { Authorization: `Bearer ${ephemeral}`, "Content-Type": "application/sdp", "OpenAI-Beta": "realtime=v1" },
      body: offer.sdp,
    });
    if (!sdpResp.ok) throw new Error(`sdp ${sdpResp.status}: ${await sdpResp.text()}`);
    answerSdp = await sdpResp.text();
  } catch (e) {
    setStatus("connect failed", "error");
    appendBubble("system", `WebRTC connect failed: ${e.message}`);
    cleanupCall();
    talkBtn.disabled = false;
    return;
  }
  await pc.setRemoteDescription({ type: "answer", sdp: answerSdp });

  // Audio context for icebreaker idle (must be created post-user-gesture; talkBtn click qualifies)
  audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  icebreaker = createIcebreakerIdle(audioCtx);

  started = true;
  endBtn.hidden = false;
  muteBtn.disabled = false;
  setDot("ok");
  setStatus("connected", "live");
}

function onDcOpen() {
  // Have the model greet first.
  send({
    type: "response.create",
    response: { modalities: ["audio", "text"], instructions: "Greet the user briefly. Just one sentence." },
  });
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
        if (item.name === "ask_agent") {
          activeToolCalls++;
          if (icebreaker) icebreaker.fadeIn();
          appendBubble("tool", "thinking...");
          setStatus("agent is thinking...", "thinking");
          setDot("thinking");
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
        // Unknown tool - return an error so the model can recover
        sendFunctionOutput(msg.call_id, JSON.stringify({ error: `unknown tool ${name}` }));
      }
      break;
    }

    case "response.done":
      // turn finished
      break;

    case "error":
      console.error("realtime error:", msg);
      setStatus("realtime error", "error");
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

async function handleAskAgent(callId, question) {
  let answer = "";
  try {
    const r = await fetch("/api/ask-agent", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conv_id: convId, question }),
    });
    const data = await r.json();
    answer = data.answer || "(no answer)";
  } catch (e) {
    answer = `(error reaching agent: ${e.message})`;
  } finally {
    activeToolCalls = Math.max(0, activeToolCalls - 1);
    if (activeToolCalls === 0) {
      if (icebreaker) icebreaker.fadeOut();
      setStatus("connected", "live");
      setDot("ok");
    }
  }
  // Show in transcript and feed back to model
  appendBubble("tool", `-> ${question}`);
  appendBubble("tool", `<- ${answer}`);
  sendFunctionOutput(callId, answer);
}

// --------------- Mute / End / Text mode ---------------

muteBtn.addEventListener("click", () => {
  if (!micStream) return;
  micMuted = !micMuted;
  micStream.getAudioTracks().forEach((t) => (t.enabled = !micMuted));
  muteBtn.textContent = micMuted ? "\u{1F399}" : "\u{1F507}";
});

endBtn.addEventListener("click", endCall);

// Fire-and-forget end-call. Safe to call repeatedly (server is idempotent);
// safe to call from page-unload paths because keepalive:true survives teardown.
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

// Mobile-safe lifecycle cleanup. iOS Safari frequently kills backgrounded
// tabs without firing pagehide - visibilitychange->hidden is the durable
// signal. pagehide is the desktop / proper-close backup. Server /api/end
// is idempotent so duplicate fires are harmless.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden" && started) {
    endCallBeacon("visibilitychange");
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
  if (icebreaker) { icebreaker.fadeOut(); icebreaker = null; }
  if (audioCtx) { audioCtx.close().catch(() => {}); audioCtx = null; }
  if (dc) { try { dc.close(); } catch {} dc = null; }
  if (pc) { try { pc.close(); } catch {} pc = null; }
  if (micStream) { micStream.getTracks().forEach((t) => t.stop()); micStream = null; }
  talkBtn.disabled = false;
  setDot("ok");
  clientEntries.length = 0;
  turnTiming.userDoneTs = null;
  turnTiming.firstTokenTs = null;
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
  // Need an active conv_id - if no call is live, mint a session for text-only
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
    appendBubble("assistant", d.answer || "(no answer)", { createdAt: doneTs, latencyMs });
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
