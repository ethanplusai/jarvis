/**
 * JARVIS — Main entry point.
 *
 * Wires together the orb visualization, WebSocket communication,
 * speech recognition, and audio playback into a single experience.
 */

import { createOrb, type OrbState } from "./orb";
import { createAudioPlayer } from "./voice";
import { createAudioCapture } from "./audio_capture";
import { createSocket } from "./ws";
import { captureCameraFrame } from "./camera";
import { openSettings, checkFirstTimeSetup } from "./settings";
import "./style.css";

// ---------------------------------------------------------------------------
// State machine
// ---------------------------------------------------------------------------

type State = "idle" | "listening" | "thinking" | "speaking";
let currentState: State = "idle";
let isMuted = false;
let bootActive = true; // during the startup boot video — suppress greeting + mic
let currentLang = "en"; // active language (drives boot audio + recognition)
let awaitingBriefing = false; // mic stays off until the post-boot briefing finishes

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

// ---------------------------------------------------------------------------
// Voice input — mic capture + VAD; server transcribes (Whisper) & auto-detects
// the language, so we just stream raw audio of each utterance.
// ---------------------------------------------------------------------------

const voiceInput = createAudioCapture(
  (pcm: ArrayBuffer) => {
    // Cancel any current JARVIS response before sending new input
    audioPlayer.stop();
    socket.sendBinary(pcm);
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
  // After the post-boot briefing finishes speaking, NOW start the mic — keeping
  // it off during the briefing so JARVIS never transcribes its own voice.
  if (awaitingBriefing) {
    awaitingBriefing = false;
    voiceInput.start();
    transition("listening");
    return;
  }
  transition("idle");
});

// ---------------------------------------------------------------------------
// WebSocket messages
// ---------------------------------------------------------------------------

socket.onMessage((msg) => {
  const type = msg.type as string;

  if (type === "audio") {
    if (bootActive) return; // ignore the backend greeting while the boot video plays
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
  } else if (type === "capture_camera") {
    // Server wants a single webcam frame. Capture one, release the camera,
    // and send it back tagged with the same request_id.
    const requestId = msg.request_id as string;
    console.log("[camera] capture requested", requestId);
    captureCameraFrame()
      .then((data) => {
        socket.send({ type: "camera_frame", request_id: requestId, data });
        if (!data) showError("Camera unavailable or blocked.");
      })
      .catch((e) => {
        console.error("[camera] error", e);
        socket.send({ type: "camera_frame", request_id: requestId, data: null });
      });
  } else if (type === "task_spawned") {
    console.log("[task]", "spawned:", msg.task_id, msg.prompt);
  } else if (type === "task_complete") {
    console.log("[task]", "complete:", msg.task_id, msg.status, msg.summary);
  }
});

// ---------------------------------------------------------------------------
// Kick off
// ---------------------------------------------------------------------------

// ── Boot sequence: machine sound + HUD loading video, then fade to the orb ──
const bootOverlay = document.getElementById("boot-overlay")!;
const bootVideo = document.getElementById("boot-video") as HTMLVideoElement;
const bootHint = document.getElementById("boot-hint")!;
const bootLoading = document.getElementById("boot-loading")!;
const bootAudio = document.getElementById("boot-audio") as HTMLAudioElement;
let bootStarted = false;
let bootGraphicFading = false;

bootVideo.addEventListener("timeupdate", () => {
  if (!bootActive) return;
  // When the red HUD fades to black (~18.6s), reveal the red loading graphic.
  if (bootVideo.currentTime >= 18.6) bootLoading.classList.add("show");
  // Near the end (~bar 95%), fade the graphic out so the orb emerges as the
  // music fades — the video keeps playing underneath so the audio fade finishes.
  if (!bootGraphicFading && bootVideo.currentTime >= 26.8) {
    bootGraphicFading = true;
    bootOverlay.classList.add("done");
  }
});

function endBoot() {
  if (!bootActive) return;
  bootActive = false;
  try { bootVideo.pause(); bootAudio.pause(); } catch {}
  bootOverlay.classList.add("done");
  setTimeout(() => { bootOverlay.style.display = "none"; }, 1500);
  // Deliver the briefing with the mic OFF so JARVIS can't hear (and transcribe)
  // its own voice. The mic starts only when the briefing finishes (onFinished).
  awaitingBriefing = true;
  transition("thinking");
  setTimeout(() => socket.send({ type: "briefing" }), 600);
  // Safety net: if the briefing never produces audio, start the mic anyway.
  setTimeout(() => {
    if (awaitingBriefing) {
      awaitingBriefing = false;
      voiceInput.start();
      transition("listening");
    }
  }, 60000);
}

function startBoot() {
  if (bootStarted) return;
  bootStarted = true;
  bootHint.style.display = "none";
  // Silent HUD video + the active language's audio track (machine sound +
  // music + welcome line in EN/FR/TR), started together by this user gesture.
  bootVideo.muted = true;
  bootVideo.currentTime = 0;
  bootAudio.src = `/boot_audio_${currentLang}.mp3`;
  bootAudio.currentTime = 0;
  bootVideo.play().catch(() => {});
  bootAudio.play().catch(() => endBoot());
  // Prefetch the briefing NOW (during the ~28s boot) so it's ready instantly
  // when the boot ends — no second wait.
  socket.send({ type: "set_lang", lang: currentLang });
  socket.send({ type: "briefing_prefetch" });
}

bootVideo.addEventListener("ended", endBoot);
bootAudio.addEventListener("ended", endBoot);
// Safety net: end the boot even if the video stalls.
bootVideo.addEventListener("loadedmetadata", () => {
  setTimeout(endBoot, (bootVideo.duration + 4) * 1000);
});
// Boot needs a user gesture (audio autoplay). Start it on the first click.
document.addEventListener("click", startBoot);

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

// Language toggle — forces Whisper recognition + JARVIS replies to a language.
const langButtons = Array.from(document.querySelectorAll<HTMLButtonElement>(".lang-btn"));
function setLanguage(lang: string) {
  currentLang = lang;
  for (const b of langButtons) b.classList.toggle("active", b.dataset.lang === lang);
  socket.send({ type: "set_lang", lang });
  console.log("[lang] set to", lang);
}
for (const b of langButtons) {
  b.addEventListener("click", (e) => {
    e.stopPropagation();
    setLanguage(b.dataset.lang || "en");
  });
}
// Tell the server the default (English) once connected.
setTimeout(() => setLanguage("en"), 1500);

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
