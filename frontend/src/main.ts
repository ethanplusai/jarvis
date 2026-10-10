import { openBusiness } from "./business";
/**
 * JARVIS — Main entry point.
 *
 * Wires together the orb visualization, WebSocket communication,
 * speech recognition, and audio playback into a single experience.
 */

import { createOrb, type Orb, type OrbState } from "./orb";
import { claimMicrophone } from "./miclock";
import { createVoiceInput, createAudioPlayer, createMicMonitor } from "./voice";
import { createSocket } from "./ws";
import { createConversation } from "./conversation";
import { loadVoiceOn, saveVoiceOn, voiceFrame } from "./voicepref";
import {
  loadListenMode, saveListenMode, isInteractiveTarget, shortcutFor, hintFor, TALK_TAIL_MS,
  type ListenMode,
} from "./listenmode";
import { openDiagnostics } from "./diagnostics";
import { openSettings, checkFirstTimeSetup } from "./settings";
import "./style.css";

// ---------------------------------------------------------------------------
// State machine
// ---------------------------------------------------------------------------

type State = "idle" | "listening" | "thinking" | "speaking" | "compacting";
let currentState: State = "idle";
let isMuted = false;               // the microphone: he is not listening
// His voice. Independent of the microphone: off, the server synthesizes
// nothing and each reply arrives as text (see voicepref.ts). Even naming
// `localStorage` can throw in a locked-down context, hence the helper.
function storage(): Storage | null {
  try { return localStorage; } catch { return null; }
}
let voiceOn = loadVoiceOn(storage());
// How he listens: only while Space is held (default), or with the mic open.
// See listenmode.ts for why the default is the key.
let listenMode: ListenMode = loadListenMode(storage());
let talking = false;                  // Space is down (hold mode)
let talkTail: number | undefined;

/** Where the page rests between turns: listening only when the mic is
 * live — unmuted, and either open or being held. */
function restState(): State {
  return isMuted || (listenMode === "hold" && !talking) ? "idle" : "listening";
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
    idle: hintFor(listenMode, isMuted),
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
// A disabled GPU must not prevent settings, audio, or the socket from starting.
let orb: Orb;
try {
  orb = createOrb(canvas);
} catch (error) {
  console.warn("[orb] visualization unavailable", error);
  canvas.hidden = true;
  orb = { setState() {}, setAnalyser() {}, destroy() {} };
  showError("The visualization is unavailable. Voice and settings are still available.");
}

const wsProto = window.location.protocol === "https:" ? "wss:" : "ws:";
const WS_URL = `${wsProto}//${window.location.host}/ws/voice`;
const socket = createSocket(WS_URL);
const conversation = createConversation(socket);

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

  // Whatever the microphone hears while he talks is him, not the user: the
  // deaf watchdog must not count it, or it restarts the recogniser exactly
  // as the user starts their next sentence (see deafwatch.ts). Nor does it
  // count the room while the key is up in hold mode.
  const micLive = listenMode === "open" || talking;
  if (newState === "speaking" || !micLive) micMonitor.hold(); else micMonitor.release();

  if (isMuted) return;
  if ((newState === "speaking" && muteMicDuringSpeech) || !micLive) {
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
    conversation.submit(text);
  },
  (text: string) => {
    micMonitor.sawSpeech();
    socket.send({ type: "interim", text });
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
  transition(restState());
}

hushBtn.addEventListener("click", hush);

// ── hold to talk, and the two toggles ─────────────────────────────────────
function startTalking() {
  if (listenMode !== "hold" || isMuted || talking) return;
  window.clearTimeout(talkTail);
  talking = true;
  if (currentState === "idle" || currentState === "listening") transition("listening");
  else { voiceInput.resume(); micMonitor.release(); }   // mid-turn: let the key still open the mic
}

function stopTalking() {
  if (!talking) return;
  window.clearTimeout(talkTail);
  // The recogniser stays on for the tail so the last word lands.
  talkTail = window.setTimeout(() => {
    talking = false;
    voiceInput.pause();
    micMonitor.hold();
    if (currentState === "listening") transition("idle");
    else updateStatus(currentState);
  }, TALK_TAIL_MS);
}

function setListenMode(mode: ListenMode) {
  listenMode = mode;
  talking = false;
  window.clearTimeout(talkTail);
  saveListenMode(storage(), mode);
  const rest = restState();
  if (currentState === "idle" || currentState === "listening") {
    if (rest === currentState) {          // same state: apply the mic change by hand
      if (mode === "open" && !isMuted) { voiceInput.resume(); micMonitor.release(); }
      else { voiceInput.pause(); micMonitor.hold(); }
      updateStatus(currentState);
    } else {
      transition(rest);
    }
  }
}
window.addEventListener("jarvis-listen-mode", (e: Event) => {
  const mode = (e as CustomEvent<ListenMode>).detail;
  if (mode === "hold" || mode === "open") setListenMode(mode);
});

function handleKey(e: KeyboardEvent) {
  if (e.type === "keydown" && e.key === "Escape") { e.preventDefault(); hush(); return; }
  const what = shortcutFor(e, isInteractiveTarget(e.target as Element | null));
  if (!what) return;
  e.preventDefault();
  if (what === "talk-start") startTalking();
  else if (what === "talk-stop") stopTalking();
  else if (what === "mic") toggleMute();
  else if (what === "voice") toggleVoice();
}
window.addEventListener("keydown", handleKey);
window.addEventListener("keyup", handleKey);
// Leaving the page with Space down must not leave the mic open.
window.addEventListener("blur", () => { if (talking) stopTalking(); });

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
    if (!voiceOn) {
      // In flight when the toggle landed: never played, but acked so the
      // scheduler's pacing does not wait on a chunk nobody will hear, and
      // shown as text so the sentence is not lost.
      socket.send({ type: "played", utt: Number(msg.utt), idx: Number(msg.idx) });
      if (msg.text) conversation.showReply(String(msg.text));
      return;
    }
    if (data) {
      if (currentState !== "speaking") transition("speaking");
      audioPlayer.enqueue(data, Number(msg.utt), Number(msg.idx));
    }
    if (msg.text) console.log("[JARVIS]", msg.text);
  } else if (type === "stop") {
    audioPlayer.stop();
    transition(restState());
  } else if (type === "drop_queued") {
    audioPlayer.dropQueued();
  } else if (type === "status") {
    const state = msg.state as string;
    if (state === "thinking") transition("thinking");
    else if (state === "speaking") transition(voiceOn ? "speaking" : "thinking");
    else if (state === "compacting") transition("compacting");
    else if (state === "idle") {
      transition(restState());
      // The turn is over: swap the live text reply for the durable record.
      void conversation.endReply();
    }
  } else if (type === "text") {
    // A sentence with no audio: his voice is off, or TTS could not voice
    // it. Either way it goes to the Conversation panel at once — and to the
    // status line, which is where a failed chunk always went.
    console.log("[JARVIS]", msg.text);
    conversation.showReply(String(msg.text));
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

// ---------------------------------------------------------------------------
// Kick off
// ---------------------------------------------------------------------------

// One tab listens at a time: Chrome runs a single live SpeechRecognition
// per browser, and a second JARVIS tab would abort this one's sessions
// mid-sentence (see miclock.ts). The claim is granted at once when no other
// tab holds it, and the moment the holder closes otherwise.
const micClaim = claimMicrophone((navigator as any).locks, () => {   // eslint-disable-line @typescript-eslint/no-explicit-any
  statusEl.textContent = "Another JARVIS tab is listening. Close it, or this one waits its turn.";
  socket.send({ type: "mic", text: "another tab holds the microphone; waiting" });
});
window.addEventListener("pagehide", () => micClaim.release());
let micHeld = false;
// Reported from the socket's open hook, so it lands in the server log even
// when the claim resolved before the connection did.
socket.onOpen(() => {
  if (micHeld) socket.send({ type: "mic", text: "this tab holds the microphone" });
  // The server forgets on restart; the page does not. Every connection
  // tells it whether his voice is on before any turn can start.
  socket.send(voiceFrame(voiceOn));
});

// Start listening after a brief delay for the orb to render
setTimeout(async () => {
  await micClaim.granted;
  micHeld = true;
  if (socket.isConnected()) socket.send({ type: "mic", text: "this tab holds the microphone" });
  voiceInput.start();
  if (listenMode === "hold") { voiceInput.pause(); micMonitor.hold(); }   // until Space is held
  if (currentState !== "speaking") { transition(restState()); updateStatus(currentState); }
}, 1000);

// Resume AudioContext on ANY user interaction (browser autoplay policy)
function ensureAudioContext() {
  // A user gesture is also the moment an on-device language pack may be
  // installed, if the browser wants one for that.
  voiceInput.prepareLocal();
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

function toggleMute() {
  isMuted = !isMuted;
  btnMute.classList.toggle("muted", isMuted);
  btnMute.title = isMuted ? "Microphone muted — click or press M" : "Mute the microphone (M)";
  if (isMuted) {
    talking = false;
    window.clearTimeout(talkTail);
    voiceInput.pause();
    micMonitor.hold();
    if (currentState !== "speaking") transition("idle");
    updateStatus(currentState);
  } else {
    if (currentState !== "speaking") transition(restState());
    updateStatus(currentState);
  }
}
btnMute.title = "Mute the microphone (M)";
btnMute.addEventListener("click", (e) => { e.stopPropagation(); toggleMute(); });

// His voice — the second toggle. Independent of the microphone's above.
const btnVoice = document.getElementById("btn-voice")!;
function paintVoice() {
  btnVoice.classList.toggle("muted", !voiceOn);
  btnVoice.title = voiceOn
    ? "Voice on — click or press V for text-only replies"
    : "Voice off — replies arrive as text in the Conversation panel (V)";
}
paintVoice();
function toggleVoice() {
  voiceOn = !voiceOn;
  saveVoiceOn(storage(), voiceOn);
  paintVoice();
  socket.send(voiceFrame(voiceOn));
  if (!voiceOn && currentState === "speaking") {
    // Silence is immediate, like hush(); the server sends the rest as text.
    audioPlayer.stop();
    transition(restState());
  }
}
btnVoice.addEventListener("click", (e) => { e.stopPropagation(); toggleVoice(); });

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
    const response = await fetch("/api/restart", { method: "POST" });
    if (!response.ok) throw new Error(`Restart failed (${response.status})`);
    // Wait a few seconds then reload
    setTimeout(() => window.location.reload(), 4000);
  } catch {
    statusEl.textContent = "restart failed";
  }
});

btnFixSelf.addEventListener("click", (e) => {
  e.stopPropagation();
  menuDropdown.style.display = "none";
  openDiagnostics();
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

document.getElementById("btn-business")?.addEventListener("click", () => { menuDropdown.style.display = "none"; openBusiness(); });
