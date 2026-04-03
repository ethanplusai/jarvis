/**
 * JARVIS — Main entry point.
 *
 * Wires together the orb visualization, WebSocket communication,
 * speech recognition, and audio playback into a single experience.
 */

import { createOrb, type OrbState } from "./orb";
import { createVoiceInput, createAudioPlayer } from "./voice";
import { createSocket } from "./ws";
import { openSettings, checkFirstTimeSetup } from "./settings";
import "./style.css";

// ---------------------------------------------------------------------------
// State machine
// ---------------------------------------------------------------------------

type State = "idle" | "listening" | "thinking" | "speaking";
type DebugLogEntry = {
  ts: string;
  logger: string;
  level: string;
  message: string;
};

let currentState: State = "idle";
let isMuted = false;
let debugMode = window.localStorage.getItem("jarvis_debug_mode") === "1";

const statusEl = document.getElementById("status-text")!;
const errorEl = document.getElementById("error-text")!;
const debugPanelEl = document.getElementById("debug-panel")!;
const debugLogEl = document.getElementById("debug-log")!;
const debugConnectionEl = document.getElementById("debug-connection")!;

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

function transition(newState: State) {
  if (newState === currentState) return;
  currentState = newState;
  orb.setState(newState as OrbState);
  updateStatus(newState);

  switch (newState) {
    case "idle":
      if (!isMuted) voiceInput.resume();
      break;
    case "listening":
      if (!isMuted) voiceInput.resume();
      break;
    case "thinking":
      voiceInput.pause();
      break;
    case "speaking":
      voiceInput.pause();
      break;
  }
}

function setDebugConnection(connected: boolean) {
  debugConnectionEl.textContent = connected ? "Live stream connected" : "Waiting for connection...";
}

function ensureDebugPlaceholder() {
  if (debugLogEl.childElementCount === 0) {
    const empty = document.createElement("div");
    empty.className = "debug-log-empty";
    empty.textContent = "Live background log will appear here.";
    debugLogEl.appendChild(empty);
  }
}

function clearDebugLog() {
  debugLogEl.innerHTML = "";
  ensureDebugPlaceholder();
}

function appendDebugEntry(entry: DebugLogEntry) {
  const nearBottom =
    debugLogEl.scrollHeight - debugLogEl.scrollTop - debugLogEl.clientHeight < 40;

  const empty = debugLogEl.querySelector(".debug-log-empty");
  if (empty) empty.remove();

  const row = document.createElement("div");
  row.className = "debug-log-entry";

  const timeEl = document.createElement("span");
  timeEl.className = "debug-log-time";
  timeEl.textContent = entry.ts;

  const levelEl = document.createElement("span");
  levelEl.className = `debug-log-level level-${entry.level.toLowerCase()}`;
  levelEl.textContent = entry.level;

  const loggerEl = document.createElement("span");
  loggerEl.className = "debug-log-logger";
  loggerEl.textContent = entry.logger;
  loggerEl.title = entry.logger;

  const messageEl = document.createElement("span");
  messageEl.className = "debug-log-message";
  messageEl.textContent = entry.message;

  row.append(timeEl, levelEl, loggerEl, messageEl);
  debugLogEl.appendChild(row);

  while (debugLogEl.childElementCount > 250) {
    debugLogEl.removeChild(debugLogEl.firstElementChild!);
  }

  if (nearBottom) {
    debugLogEl.scrollTop = debugLogEl.scrollHeight;
  }
}

function applyDebugMode() {
  debugPanelEl.classList.toggle("open", debugMode);
  btnDebugToggle.textContent = debugMode ? "Hide Debug Mode" : "Debug Mode";
  window.localStorage.setItem("jarvis_debug_mode", debugMode ? "1" : "0");

  if (!debugMode) {
    socket.send({ type: "debug_logs", enabled: false });
    return;
  }

  if (socket.isConnected()) {
    socket.send({ type: "debug_logs", enabled: true });
  }
}

// ---------------------------------------------------------------------------
// Voice input
// ---------------------------------------------------------------------------

const voiceInput = createVoiceInput(
  (text: string) => {
    // Cancel any current JARVIS response before sending new input
    audioPlayer.stop();
    // User spoke — send transcript
    socket.send({ type: "transcript", text, isFinal: true });
    transition("thinking");
  },
  (msg: string) => {
    showError(msg);
  }
);

// ---------------------------------------------------------------------------
// Audio playback finished
// ---------------------------------------------------------------------------

audioPlayer.onFinished(() => {
  transition("idle");
});

// ---------------------------------------------------------------------------
// WebSocket messages
// ---------------------------------------------------------------------------

socket.onMessage((msg) => {
  const type = msg.type as string;

  if (type === "audio") {
    const audioData = msg.data as string;
    console.log("[audio] received", audioData ? `${audioData.length} chars` : "EMPTY", "state:", currentState);
    if (audioData) {
      if (currentState !== "speaking") {
        transition("speaking");
      }
      audioPlayer.enqueue(audioData);
    } else {
      // TTS failed — no audio but still need to return to idle
      console.warn("[audio] no data received, returning to idle");
      transition("idle");
    }
    // Log text for debugging
    if (msg.text) console.log("[JARVIS]", msg.text);
  } else if (type === "status") {
    const state = msg.state as string;
    if (state === "thinking" && currentState !== "thinking") {
      transition("thinking");
    } else if (state === "working") {
      // Task spawned — show thinking with a different label
      transition("thinking");
      statusEl.textContent = "working...";
    } else if (state === "idle") {
      transition("idle");
    }
  } else if (type === "text") {
    // Text fallback when TTS fails
    console.log("[JARVIS]", msg.text);
  } else if (type === "task_spawned") {
    console.log("[task]", "spawned:", msg.task_id, msg.prompt);
  } else if (type === "task_complete") {
    console.log("[task]", "complete:", msg.task_id, msg.status, msg.summary);
  } else if (type === "debug_log_snapshot") {
    clearDebugLog();
    const entries = (msg.entries as DebugLogEntry[]) || [];
    for (const entry of entries) appendDebugEntry(entry);
    ensureDebugPlaceholder();
  } else if (type === "debug_log") {
    const entry = msg.entry as DebugLogEntry | undefined;
    if (entry) appendDebugEntry(entry);
  }
});

socket.onOpen(() => {
  setDebugConnection(true);
  if (debugMode) {
    socket.send({ type: "debug_logs", enabled: true });
  }
});

socket.onConnectionChange((connected) => {
  setDebugConnection(connected);
});

// ---------------------------------------------------------------------------
// Kick off
// ---------------------------------------------------------------------------

// Start listening after a brief delay for the orb to render
setTimeout(() => {
  voiceInput.start();
  transition("listening");
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
const btnDebugToggle = document.getElementById("btn-debug-toggle")!;
const btnDebugClear = document.getElementById("btn-debug-clear")!;
const btnDebugClose = document.getElementById("btn-debug-close")!;
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

btnDebugToggle.addEventListener("click", (e) => {
  e.stopPropagation();
  menuDropdown.style.display = "none";
  debugMode = !debugMode;
  applyDebugMode();
});

btnDebugClear.addEventListener("click", () => {
  clearDebugLog();
});

btnDebugClose.addEventListener("click", () => {
  debugMode = false;
  applyDebugMode();
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
  socket.send({ type: "fix_self" });
  statusEl.textContent = "entering work mode...";
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

ensureDebugPlaceholder();
applyDebugMode();
