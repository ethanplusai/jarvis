/**
 * JARVIS — Main entry point.
 *
 * Wires together the orb visualization, WebSocket communication,
 * speech recognition, and audio playback into a single experience.
 */

import { createOrb, type OrbState } from "./orb";
import { createVoiceInput, createAudioPlayer, createMicMonitor } from "./voice";
import { createSocket } from "./ws";
import { openSettings, checkFirstTimeSetup } from "./settings";
import { matchWake, wakePhrase } from "./wake";
import "./style.css";

// ---------------------------------------------------------------------------
// State machine
// ---------------------------------------------------------------------------

type State = "idle" | "listening" | "thinking" | "speaking" | "compacting";
let currentState: State = "idle";
let isMuted = false;

// Asleep until the wake phrase is heard: nothing shown, nothing spoken.
const WAKE_PHRASE = wakePhrase();
let awake = false;

function wake() {
  if (awake) return;
  awake = true;
  document.body.classList.remove("asleep");
  document.body.classList.add("awake");
}

/** Every transcript, spoken or typed, goes through here. */
function handleUserText(text: string) {
  if (awake) {
    socket.send({ type: "transcript", text, isFinal: true });
    return;
  }
  const { woke, remainder } = matchWake(text, WAKE_PHRASE);
  if (!woke) return;
  wake();
  socket.send({ type: "transcript", text: remainder || "Hello JARVIS", isFinal: true });
}

const statusEl = document.getElementById("status-text")!;
const errorEl = document.getElementById("error-text")!;

function showError(msg: string) {
  errorEl.textContent = msg;
  errorEl.style.opacity = "1";
  setTimeout(() => {
    errorEl.style.opacity = "0";
  }, 5000);
}

function updateStatus(state: State) {
  const labels: Record<State, string> = {
    idle: "",
    listening: "listening...",
    thinking: "thinking...",
    speaking: "",
    compacting: "",          // the notice banner carries the words; the orb carries the state
  };
  statusEl.textContent = labels[state];
}

// ---------------------------------------------------------------------------
// Init components
// ---------------------------------------------------------------------------

const canvas = document.getElementById("orb-canvas") as HTMLCanvasElement;
const orb = createOrb(canvas);

const wsProto = window.location.protocol === "https:" ? "wss:" : "ws:";
const WS_URL = `${wsProto}//${window.location.host}/ws/voice`;
const socket = createSocket(WS_URL);

const audioPlayer = createAudioPlayer();
orb.setAnalyser(audioPlayer.getAnalyser());

let muteMicDuringSpeech = false;

function transition(newState: State) {
  if (newState === currentState) return;
  currentState = newState;
  const btn = document.getElementById("hush");
  if (btn) (btn as HTMLButtonElement).hidden = newState !== "speaking";
  orb.setState(newState as OrbState);
  updateStatus(newState);

  if (isMuted) return;
  if (newState === "speaking" && muteMicDuringSpeech) {
    voiceInput.pause();
  } else {
    voiceInput.resume();
  }
}

// ---------------------------------------------------------------------------
// Voice input
// ---------------------------------------------------------------------------

const voiceInput = createVoiceInput(
  (text: string) => {
    // The server decides whether this is echo, a barge-in, or a new turn.
    micMonitor.sawSpeech();
    handleUserText(text);
  },
  (text: string) => {
    micMonitor.sawSpeech();
    if (awake) socket.send({ type: "interim", text });
  },
  (msg: string) => {
    showError(msg);
  },
  (event: string) => {
    // Mirror the recogniser's lifecycle to the server log. Going deaf is a
    // browser-side failure the server cannot otherwise see at all, and the
    // console it used to be confined to is never open when it happens.
    socket.send({ type: "mic", text: event });
  }
);

// A live meter for the microphone itself. If this moves when you speak, the
// microphone is working — whatever else is or is not happening. It answers
// "is it even hearing me?" without a log, a console or anyone to ask.
const micDot = document.createElement("div");
micDot.id = "mic-level";
micDot.title = "microphone input";
document.body.appendChild(micDot);

const micMonitor = createMicMonitor(
  (level: number) => {
    const pct = Math.min(100, Math.round(level * 900));
    micDot.style.setProperty("--level", `${pct}%`);
    micDot.classList.toggle("is-hot", level > 0.02);
  },
  (event: string) => {
    socket.send({ type: "mic", text: event });
    // Proven deaf: sound going in, nothing coming out. Do not wait for the
    // rotation timer to happen along — measured once at 21 seconds, all of
    // it lost. Rebuild the recogniser now.
    if (event.startsWith("DEAF")) voiceInput.restart("deaf: audio in, no results");
  }
);

// ── stopping him ──────────────────────────────────────────────────────────
// Escape, or the button that appears while he is talking. Not a spoken word:
// his voice comes back through the microphone garbled, and a mis-hear that
// looked like "stop" would cut him off at random. A keystroke cannot be
// misheard.
const hushBtn = document.createElement("button");
hushBtn.id = "hush";
hushBtn.type = "button";
hushBtn.textContent = "Stop";
hushBtn.title = "Stop speaking (Esc)";
hushBtn.hidden = true;
document.body.appendChild(hushBtn);

function hush() {
  if (currentState !== "speaking") return;
  // Locally first: the round trip is real and silence should be instant.
  audioPlayer.stop();
  socket.send({ type: "hush" });
  transition(isMuted ? "idle" : "listening");
}

hushBtn.addEventListener("click", hush);
window.addEventListener("keydown", (e: KeyboardEvent) => {
  if (e.key === "Escape") { e.preventDefault(); hush(); }
});

audioPlayer.onPlayed((utt, idx) => {
  socket.send({ type: "played", utt, idx });
});

// End of speech is the server's call (`status: idle` after every chunk is
// acked); a transient empty queue mid-utterance must not flip the UI.
audioPlayer.onFinished(() => {});

audioPlayer.onNeedsGesture(() => {
  showError("Click anywhere to enable audio");
});

// ---------------------------------------------------------------------------
// WebSocket messages
// ---------------------------------------------------------------------------

socket.onMessage((msg) => {
  const type = msg.type as string;

  if (type === "config") {
    muteMicDuringSpeech = Boolean(msg.muteMicDuringSpeech);
  } else if (type === "audio") {
    const data = msg.data as string;
    if (data && !awake) {
      // Asleep: stay silent, but ack the chunk so the server is not left
      // waiting on a playback that will never happen.
      socket.send({ type: "played", utt: Number(msg.utt), idx: Number(msg.idx) });
    } else if (data) {
      if (currentState !== "speaking") transition("speaking");
      audioPlayer.enqueue(data, Number(msg.utt), Number(msg.idx));
    }
    if (msg.text) console.log("[JARVIS]", msg.text);
  } else if (type === "stop") {
    audioPlayer.stop();
    transition(isMuted ? "idle" : "listening");
  } else if (type === "drop_queued") {
    audioPlayer.dropQueued();
  } else if (type === "status") {
    const state = msg.state as string;
    if (state === "thinking") transition("thinking");
    else if (state === "speaking") transition("speaking");
    else if (state === "compacting") transition("compacting");
    else if (state === "idle") transition(isMuted ? "idle" : "listening");
  } else if (type === "text") {
    // A chunk TTS could not voice: show it instead of losing it
    console.log("[JARVIS]", msg.text);
    statusEl.textContent = String(msg.text);
  } else if (type === "notice") {
    // Shown, never spoken. The server sends one when it is about to be busy
    // for a few seconds (a context rotation), and an empty string to clear it.
    // Without it the pause looks like a crash.
    const text = String(msg.text ?? "");
    statusEl.textContent = text;
    if (text) console.log("[notice]", text);
  }
});

// ── typed input ──────────────────────────────────────────────────────────
// For dictation tools (Wispr Flow) that type rather than speak into the page.
const typeBox = document.getElementById("type-box") as HTMLInputElement | null;
typeBox?.addEventListener("keydown", (e: KeyboardEvent) => {
  if (e.key !== "Enter") return;
  e.preventDefault();
  const text = typeBox.value.trim();
  typeBox.value = "";
  if (text) handleUserText(text);
});

// ---------------------------------------------------------------------------
// Kick off
// ---------------------------------------------------------------------------

const sleepHint = document.getElementById("sleep-hint");
if (sleepHint) sleepHint.textContent = `say "${WAKE_PHRASE}"`;

// Start listening after a brief delay for the orb to render
setTimeout(() => {
  voiceInput.start();
  if (currentState !== "speaking") transition("listening");
}, 1000);

// Resume AudioContext on ANY user interaction (browser autoplay policy)
function ensureAudioContext() {
  const ctx = audioPlayer.getAnalyser().context as AudioContext;
  if (ctx.state === "suspended") {
    ctx.resume().then(() => console.log("[audio] context resumed"));
  }
}
document.addEventListener("click", ensureAudioContext);
document.addEventListener("touchstart", ensureAudioContext);
document.addEventListener("keydown", ensureAudioContext, { once: true });

// Try to resume audio context on load
ensureAudioContext();

// ---------------------------------------------------------------------------
// UI Controls
// ---------------------------------------------------------------------------

const btnMute = document.getElementById("btn-mute")!;
const btnMenu = document.getElementById("btn-menu")!;
const menuDropdown = document.getElementById("menu-dropdown")!;
const btnRestart = document.getElementById("btn-restart")!;
const btnFixSelf = document.getElementById("btn-fix-self")!;

btnMute.addEventListener("click", (e) => {
  e.stopPropagation();
  isMuted = !isMuted;
  btnMute.classList.toggle("muted", isMuted);
  if (isMuted) {
    voiceInput.pause();
    transition("idle");
  } else {
    voiceInput.resume();
    transition("listening");
  }
});

btnMenu.addEventListener("click", (e) => {
  e.stopPropagation();
  menuDropdown.style.display = menuDropdown.style.display === "none" ? "block" : "none";
});

document.addEventListener("click", () => {
  menuDropdown.style.display = "none";
});

btnRestart.addEventListener("click", async (e) => {
  e.stopPropagation();
  menuDropdown.style.display = "none";
  statusEl.textContent = "restarting...";
  try {
    await fetch("/api/restart", { method: "POST" });
    // Wait a few seconds then reload
    setTimeout(() => window.location.reload(), 4000);
  } catch {
    statusEl.textContent = "restart failed";
  }
});

btnFixSelf.addEventListener("click", (e) => {
  e.stopPropagation();
  menuDropdown.style.display = "none";
  // Activate work mode on the WebSocket session (JARVIS becomes Claude Code's voice)
  // Milestone 1 has no tools yet; "Fix yourself" returns as a brain tool later.
  statusEl.textContent = "fix-yourself is not available in this build";
});

// Settings button
const btnSettings = document.getElementById("btn-settings")!;
btnSettings.addEventListener("click", (e) => {
  e.stopPropagation();
  menuDropdown.style.display = "none";
  openSettings();
});

// First-time setup detection — check after a short delay for server readiness
setTimeout(() => {
  checkFirstTimeSetup();
}, 2000);
