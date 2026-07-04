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
let currentState: State = "idle";
let isMuted = false;

const statusEl = document.getElementById("status-text")!;
const errorEl = document.getElementById("error-text")!;

const metricLink = document.getElementById("metric-link")!;
const metricUptime = document.getElementById("metric-uptime")!;
const metricLoad = document.getElementById("metric-load")!;
const metricDisk = document.getElementById("metric-disk")!;
const metricCost = document.getElementById("metric-cost")!;
const metricTokens = document.getElementById("metric-tokens")!;
const eventLog = document.getElementById("event-log")!;
const dropZone = document.getElementById("drop-zone")!;
const quickActions = document.getElementById("quick-actions")!;
const artifactList = document.getElementById("artifact-list")!;
const pendingActions = document.getElementById("pending-actions")!;
const widgetGrid = document.getElementById("widget-grid")!;
const jarvisBriefs = document.getElementById("jarvis-briefs")!;
const personalBriefing = document.getElementById("personal-briefing")!;
const btnCustomizeWidgets = document.getElementById("btn-customize-widgets")!;

type WidgetId = "jarvis" | "briefing" | "news" | "weather" | "markets" | "activity" | "guard";
const DEFAULT_WIDGETS: WidgetId[] = ["jarvis", "briefing", "news", "weather", "markets", "activity", "guard"];
const WIDGET_LABELS: Record<WidgetId, string> = {
  jarvis: "JARVIS text summaries",
  briefing: "Personal briefing",
  news: "News radar",
  weather: "Weather",
  markets: "Markets / stock exchange",
  activity: "Activity log",
  guard: "Action guard",
};

function loadWidgetLayout(): WidgetId[] {
  try {
    const saved = JSON.parse(localStorage.getItem("jarvis.controlCenter.widgets") || "null") as WidgetId[] | null;
    const valid = saved?.filter((id) => DEFAULT_WIDGETS.includes(id)) || [];
    return [...valid, ...DEFAULT_WIDGETS.filter((id) => !valid.includes(id))];
  } catch {
    return [...DEFAULT_WIDGETS];
  }
}

function saveWidgetLayout(layout: WidgetId[]) {
  localStorage.setItem("jarvis.controlCenter.widgets", JSON.stringify(layout));
}

function applyWidgetLayout() {
  const layout = loadWidgetLayout();
  layout.forEach((id) => {
    const el = widgetGrid.querySelector<HTMLElement>(`[data-widget="${id}"]`);
    if (el) {
      widgetGrid.appendChild(el);
      el.style.display = "";
    }
  });
  DEFAULT_WIDGETS.forEach((id) => {
    const hidden = localStorage.getItem(`jarvis.controlCenter.hidden.${id}`) === "1";
    const el = widgetGrid.querySelector<HTMLElement>(`[data-widget="${id}"]`);
    if (el) el.style.display = hidden ? "none" : "";
  });
}

function addControlCenterBrief(title: string, body: string, category = "jarvis") {
  const row = document.createElement("div");
  row.className = "brief-row";
  const time = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  row.innerHTML = `<span>${escapeHtml(category)} · ${time}</span><strong>${escapeHtml(title)}</strong><small>${escapeHtml(body)}</small>`;
  jarvisBriefs.prepend(row);
  while (jarvisBriefs.children.length > 4) jarvisBriefs.lastElementChild?.remove();
}

function openWidgetCustomizer() {
  const lines = DEFAULT_WIDGETS.map((id, index) => {
    const hidden = localStorage.getItem(`jarvis.controlCenter.hidden.${id}`) === "1";
    return `${index + 1}. ${hidden ? "[hidden] " : ""}${WIDGET_LABELS[id]}`;
  }).join("\n");
  const choice = window.prompt(`Control Center widgets:
${lines}

Type a widget name to hide/show, or type reset.`, "");
  if (!choice) return;
  const normalized = choice.toLowerCase().trim();
  if (normalized === "reset") {
    DEFAULT_WIDGETS.forEach((id) => localStorage.removeItem(`jarvis.controlCenter.hidden.${id}`));
    saveWidgetLayout([...DEFAULT_WIDGETS]);
  } else {
    const match = DEFAULT_WIDGETS.find((id) => id === normalized || WIDGET_LABELS[id].toLowerCase().includes(normalized));
    if (match) {
      const key = `jarvis.controlCenter.hidden.${match}`;
      localStorage.setItem(key, localStorage.getItem(key) === "1" ? "0" : "1");
    }
  }
  applyWidgetLayout();
}


const statePill = document.getElementById("state-pill")!;
const statePillLabel = document.getElementById("state-pill-label")!;
const toastStack = document.getElementById("toast-stack")!;

type LogTone = "user" | "jarvis" | "system";

function addLog(text: string, tone: LogTone = "system") {
  const row = document.createElement("div");
  row.className = `log-row ${tone}`;
  const time = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  row.innerHTML = `<span>${time}</span><p></p>`;
  row.querySelector("p")!.textContent = text;
  eventLog.prepend(row);
  while (eventLog.children.length > 12) eventLog.lastElementChild?.remove();
}

function formatDuration(seconds: number) {
  if (!Number.isFinite(seconds)) return "--";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

async function refreshSystemMetrics() {
  metricLink.textContent = socket.isConnected() ? "online" : "offline";
  metricLink.classList.toggle("offline", !socket.isConnected());
  try {
    const res = await fetch("/api/system");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    metricUptime.textContent = formatDuration(Number(data.uptime_seconds));
    metricLoad.textContent = typeof data.load_average === "number" ? data.load_average.toFixed(2) : "--";
    metricDisk.textContent = typeof data.disk_free_gb === "number" ? `${data.disk_free_gb.toFixed(1)} GB` : "--";
  } catch {
    metricUptime.textContent = "--";
    metricLoad.textContent = "--";
    metricDisk.textContent = "--";
  }
}

function formatTokens(n: number) {
  if (!Number.isFinite(n) || n <= 0) return "0";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(Math.round(n));
}

async function refreshUsage() {
  try {
    const res = await fetch("/api/usage");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const session = data.session || {};
    const today = data.today || {};
    const tokens = Number(session.input_tokens || 0) + Number(session.output_tokens || 0);
    metricTokens.textContent = formatTokens(tokens);
    metricCost.textContent = typeof today.cost_usd === "number" ? `$${today.cost_usd.toFixed(2)}` : "--";
  } catch {
    metricTokens.textContent = "--";
    metricCost.textContent = "--";
  }
}

// Monotonic id stamped on every outgoing command. The server echoes it back as
// `reqId`, so a reply for a superseded command (the user barged in) is discarded.
let commandSeq = 0;

function sendCommand(text: string, source: "voice" | "quick" | "file" | "text" = "quick") {
  audioPlayer.stop();
  commandSeq += 1;
  socket.send({ type: "transcript", text, isFinal: true, source, id: commandSeq });
  addLog(text, source === "file" ? "system" : "user");
  transition("thinking");
}

type ToastType = "info" | "success" | "error";

function showToast(msg: string, type: ToastType = "info", duration = 4000) {
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.textContent = msg;
  toastStack.appendChild(toast);
  while (toastStack.children.length > 4) toastStack.firstElementChild?.remove();
  setTimeout(() => {
    toast.classList.add("leaving");
    toast.addEventListener("animationend", () => toast.remove(), { once: true });
  }, duration);
}

function showError(msg: string) {
  // Keep the legacy element in sync for any external callers, but surface via toast.
  errorEl.textContent = msg;
  showToast(msg, "error", 5000);
}

function updateStatus(state: State) {
  const labels: Record<State, string> = {
    idle: "",
    listening: "listening...",
    thinking: "thinking...",
    speaking: "",
  };
  statusEl.textContent = labels[state];
  updateStatePill(state);
}

function updateStatePill(state: State) {
  const pillLabels: Record<State, string> = {
    idle: "standby",
    listening: "listening",
    thinking: "thinking",
    speaking: "speaking",
  };
  statePill.className = isMuted ? "state-idle muted" : `state-${state}`;
  statePillLabel.textContent = isMuted ? "muted" : pillLabels[state];
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

let micResumeTimer: number | undefined;

function transition(newState: State) {
  if (newState === currentState) return;
  const prevState = currentState;
  currentState = newState;
  orb.setState(newState as OrbState);
  updateStatus(newState);

  if (micResumeTimer !== undefined) {
    clearTimeout(micResumeTimer);
    micResumeTimer = undefined;
  }

  switch (newState) {
    case "idle":
    case "listening":
      if (!isMuted) {
        // Brief debounce when JARVIS just finished speaking so the audio tail
        // isn't picked back up by the mic as a phantom transcript.
        const delay = prevState === "speaking" ? 300 : 0;
        micResumeTimer = window.setTimeout(() => voiceInput.resume(), delay);
      }
      break;
    case "thinking":
    case "speaking":
      voiceInput.pause();
      break;
  }
}

// ---------------------------------------------------------------------------
// Voice input
// ---------------------------------------------------------------------------

const voiceInput = createVoiceInput(
  (text: string) => {
    // User spoke — send transcript
    sendCommand(text, "voice");
  },
  (msg: string) => {
    showError(msg);
  }
);

window.addEventListener("jarvis:speech-language", (event) => {
  const lang = (event as CustomEvent<string>).detail;
  if (lang) {
    voiceInput.setLanguage(lang);
    showToast(`Speech capture language set to ${lang}`, "success", 2200);
  }
});

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

  // Drop replies for a command the user has already superseded (barge-in).
  const reqId = msg.reqId as number | undefined;
  if (reqId !== undefined && reqId !== commandSeq) {
    console.log("[ws] dropping stale reply", reqId, "current", commandSeq);
    return;
  }

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
    if (msg.text) {
      console.log("[JARVIS]", msg.text);
      addLog(String(msg.text), "jarvis");
    }
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
    if (msg.text) addLog(String(msg.text), "jarvis");
  } else if (type === "task_spawned") {
    console.log("[task]", "spawned:", msg.task_id, msg.prompt);
    addLog(`Task spawned: ${msg.task_id}`, "system");
  } else if (type === "task_complete") {
    console.log("[task]", "complete:", msg.task_id, msg.status, msg.summary);
    addLog(`Task complete: ${msg.summary || msg.status}`, "system");
  } else if (type === "control_center") {
    addControlCenterBrief(String(msg.title || "JARVIS update"), String(msg.body || msg.text || ""), String(msg.category || "jarvis"));
    showToast("Control Center updated", "success", 2500);
  } else if (type === "action_pending") {
    const action = msg.action as { id?: number; title?: string; risk?: string };
    addLog(`Confirmation needed: ${action.title || "external action"}`, "system");
    showToast(`Confirmation needed: ${action.title || "external action"}`, "info", 7000);
    refreshPendingActions();
  }
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
const btnRestart = document.getElementById("btn-restart")!;
const btnFixSelf = document.getElementById("btn-fix-self")!;
const btnMenuClose = document.getElementById("btn-menu-close")!;
const btnControlCenter = document.getElementById("btn-control-center")!;

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
  updateStatePill(currentState);
  showToast(isMuted ? "Microphone muted" : "Microphone live", isMuted ? "info" : "success", 2000);
});

function toggleSideMenu(force?: boolean) {
  const open = force ?? menuDropdown.style.display === "none";
  menuDropdown.style.display = open ? "block" : "none";
  requestAnimationFrame(() => menuDropdown.classList.toggle("open", open));
}

btnMenu.addEventListener("click", (e) => {
  e.stopPropagation();
  toggleSideMenu();
});

menuDropdown.addEventListener("click", (e) => e.stopPropagation());

btnMenuClose.addEventListener("click", (e) => {
  e.stopPropagation();
  toggleSideMenu(false);
});

btnControlCenter.addEventListener("click", (e) => {
  e.stopPropagation();
  toggleSideMenu(false);
  sendCommand(btnControlCenter.dataset.command || "Summarize what matters right now for my control center.", "quick");
});

document.addEventListener("click", () => {
  toggleSideMenu(false);
});

btnRestart.addEventListener("click", async (e) => {
  e.stopPropagation();
  toggleSideMenu(false);
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
  toggleSideMenu(false);
  // Activate work mode on the WebSocket session (JARVIS becomes Claude Code's voice)
  socket.send({ type: "fix_self" });
  statusEl.textContent = "entering work mode...";
});

// Settings button
const btnSettings = document.getElementById("btn-settings")!;
btnSettings.addEventListener("click", (e) => {
  e.stopPropagation();
  toggleSideMenu(false);
  openSettings();
});

// First-time setup detection — check after a short delay for server readiness
setTimeout(() => {
  checkFirstTimeSetup();
}, 2000);


// ---------------------------------------------------------------------------
// MARK XL-inspired Mission Control: metrics, rapid actions, file intake
// ---------------------------------------------------------------------------

function handleCommandButton(event: Event) {
  const button = (event.target as HTMLElement).closest<HTMLButtonElement>("button[data-command]");
  if (!button) return;
  sendCommand(button.dataset.command || button.textContent || "", "quick");
}
quickActions.addEventListener("click", handleCommandButton);
widgetGrid.addEventListener("click", handleCommandButton);
widgetGrid.addEventListener("click", (event) => {
  const pin = (event.target as HTMLElement).closest<HTMLButtonElement>("button[data-pin-widget]");
  if (!pin) return;
  const id = pin.dataset.pinWidget as WidgetId;
  const layout = loadWidgetLayout().filter((widget) => widget !== id);
  saveWidgetLayout([id, ...layout]);
  applyWidgetLayout();
  showToast(`${WIDGET_LABELS[id]} pinned`, "success", 1800);
});
btnCustomizeWidgets.addEventListener("click", openWidgetCustomizer);

async function uploadFile(file: File) {
  const maxBytes = 512 * 1024;
  if (file.size > maxBytes) {
    showError(`${file.name} is too large for quick intake (512 KB limit).`);
    addLog(`Rejected ${file.name}: larger than 512 KB`, "system");
    return;
  }

  const content = await file.text();
  const res = await fetch("/api/intake-file", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: file.name, mime_type: file.type || "text/plain", content }),
  });
  const data = await res.json();
  if (!res.ok) {
    showError(data.error || `Could not ingest ${file.name}.`);
    return;
  }
  const prompt = `I uploaded ${data.name}. Summarize it, identify risks or useful next actions, and remember the important context.`;
  addLog(`File ingested: ${data.name}`, "system");
  showToast(`Ingested ${data.name}`, "success");
  sendCommand(prompt, "file");
}

["dragenter", "dragover"].forEach((name) => {
  dropZone.addEventListener(name, (event) => {
    event.preventDefault();
    dropZone.classList.add("dragging");
  });
});

["dragleave", "drop"].forEach((name) => {
  dropZone.addEventListener(name, (event) => {
    event.preventDefault();
    dropZone.classList.remove("dragging");
  });
});

dropZone.addEventListener("drop", async (event) => {
  const files = Array.from(event.dataTransfer?.files || []);
  for (const file of files.slice(0, 4)) {
    await uploadFile(file);
  }
});

applyWidgetLayout();
addControlCenterBrief("Control Center online", "Customize widgets with Tune, or ask JARVIS to add news, weather, market snapshots, stats, and text summaries here.", "system");
addLog("Mission control online. Voice capture standing by — not ominously, just efficiently.", "system");
refreshSystemMetrics();
refreshUsage();
refreshArtifacts();
refreshPendingActions();
refreshPersonalBriefing();
setInterval(refreshSystemMetrics, 5000);
setInterval(refreshUsage, 8000);
setInterval(refreshArtifacts, 15000);
setInterval(refreshPendingActions, 10000);
setInterval(refreshPersonalBriefing, 60000);


// ---------------------------------------------------------------------------
// Personal briefing, artifacts + guardrail action queue
// ---------------------------------------------------------------------------


type Briefing = {
  tasks?: { open?: number; high_priority?: number; due_today?: Array<{ title?: string }>; overdue?: Array<{ title?: string }>; focus?: Array<{ title?: string; priority?: string; due_date?: string }> };
  calendar?: { today_count?: number };
  email?: { count?: number; available?: boolean };
  memory?: { stats?: { total_memories?: number; open_tasks?: number }; important?: Array<{ content?: string; type?: string }> };
  connectivity?: { mcp_connected?: number; mcp_servers?: Array<{ name?: string }> ; providers_configured?: string[] };
  recommendations?: string[];
};

async function refreshPersonalBriefing() {
  try {
    const res = await fetch("/api/briefing");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = (await res.json()) as Briefing;
    const focus = data.tasks?.focus?.[0];
    const connectors = data.connectivity?.mcp_servers?.map((s) => s.name).filter(Boolean).join(", ") || "No MCP tools connected";
    const recommendations = (data.recommendations || []).slice(0, 3).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
    personalBriefing.innerHTML = `
      <div class="briefing-stats">
        <div><span>Tasks</span><strong>${Number(data.tasks?.open || 0)}</strong></div>
        <div><span>Today</span><strong>${Number(data.calendar?.today_count || 0)}</strong></div>
        <div><span>Unread</span><strong>${Number(data.email?.count || 0)}</strong></div>
        <div><span>MCP</span><strong>${Number(data.connectivity?.mcp_connected || 0)}</strong></div>
      </div>
      <strong>${focus ? escapeHtml(focus.title || "Focus selected") : "No urgent focus selected"}</strong>
      <small>${focus ? `Priority: ${escapeHtml(focus.priority || "medium")}${focus.due_date ? ` · due ${escapeHtml(focus.due_date)}` : ""}` : "Ask JARVIS for a task focus plan when you're ready."}</small>
      <ul>${recommendations}</ul>
      <small>Connectors: ${escapeHtml(connectors)}</small>`;
  } catch {
    personalBriefing.innerHTML = `<strong>Briefing unavailable</strong><small>The local context endpoint did not respond.</small>`;
  }
}

document.getElementById("btn-briefing-refresh")?.addEventListener("click", refreshPersonalBriefing);

function escapeHtml(value: string) {
  return value.replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "\'": "&#039;" }[ch] || ch));
}

function formatBytes(bytes: number) {
  if (!Number.isFinite(bytes)) return "--";
  if (bytes > 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  if (bytes > 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${Math.max(0, Math.round(bytes))} B`;
}

async function refreshArtifacts() {
  try {
    const res = await fetch("/api/artifacts");
    const data = await res.json();
    const artifacts = (data.artifacts || []) as Array<{ name: string; size: number; modified_at: number; download_url: string }>;
    if (artifacts.length === 0) {
      artifactList.innerHTML = `<div class="mini-row"><strong>No artifacts yet</strong><small>Executable skills will appear here.</small></div>`;
      return;
    }
    artifactList.innerHTML = artifacts.slice(0, 5).map((a) => `
      <div class="mini-row">
        <strong>${escapeHtml(a.name)}</strong>
        <small>${formatBytes(Number(a.size))} · ${new Date(Number(a.modified_at) * 1000).toLocaleString()}</small>
        <div class="mini-actions"><button data-open-artifact="${escapeHtml(a.download_url)}">Open</button><button data-copy-artifact="${escapeHtml(a.name)}">Copy name</button></div>
      </div>`).join("");
  } catch {
    artifactList.innerHTML = `<div class="mini-row"><strong>Artifacts unavailable</strong></div>`;
  }
}

async function refreshPendingActions() {
  try {
    const res = await fetch("/api/action-log?status=pending_confirmation&limit=10");
    const data = await res.json();
    const actions = (data.actions || []) as Array<{ id: number; title: string; risk: string; created_at: number }>;
    if (actions.length === 0) {
      pendingActions.innerHTML = `<div class="mini-row"><strong>No pending actions</strong><small>Outbound tool calls pause here before execution.</small></div>`;
      return;
    }
    pendingActions.innerHTML = actions.map((a) => `
      <div class="mini-row">
        <strong>${escapeHtml(a.title)}</strong>
        <small>${escapeHtml(a.risk.toUpperCase())} risk · ${new Date(Number(a.created_at) * 1000).toLocaleTimeString()}</small>
        <div class="mini-actions"><button class="danger" data-confirm-action="${a.id}">Confirm</button><button data-cancel-action="${a.id}">Cancel</button></div>
      </div>`).join("");
  } catch {
    pendingActions.innerHTML = `<div class="mini-row"><strong>Action guard unavailable</strong></div>`;
  }
}

document.getElementById("btn-artifacts-refresh")?.addEventListener("click", refreshArtifacts);
document.getElementById("btn-action-refresh")?.addEventListener("click", refreshPendingActions);

artifactList.addEventListener("click", async (event) => {
  const target = event.target as HTMLElement;
  const open = target.closest<HTMLButtonElement>("button[data-open-artifact]");
  const copy = target.closest<HTMLButtonElement>("button[data-copy-artifact]");
  if (open) window.open(open.dataset.openArtifact, "_blank");
  if (copy) {
    await navigator.clipboard.writeText(copy.dataset.copyArtifact || "");
    showToast("Artifact name copied", "success");
  }
});

pendingActions.addEventListener("click", async (event) => {
  const target = event.target as HTMLElement;
  const confirmBtn = target.closest<HTMLButtonElement>("button[data-confirm-action]");
  const cancelBtn = target.closest<HTMLButtonElement>("button[data-cancel-action]");
  const id = confirmBtn?.dataset.confirmAction || cancelBtn?.dataset.cancelAction;
  if (!id) return;
  const path = confirmBtn ? `/api/action-log/${id}/confirm` : `/api/action-log/${id}/cancel`;
  const res = await fetch(path, { method: "POST" });
  if (!res.ok) {
    showToast(confirmBtn ? "Confirmation failed" : "Cancel failed", "error");
    return;
  }
  showToast(confirmBtn ? "Action confirmed" : "Action cancelled", confirmBtn ? "success" : "info");
  refreshPendingActions();
});

// ---------------------------------------------------------------------------
// Text command bar — type to JARVIS
// ---------------------------------------------------------------------------

const commandBar = document.getElementById("command-bar") as HTMLFormElement;
const commandInput = document.getElementById("command-input") as HTMLInputElement;

commandBar.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = commandInput.value.trim();
  if (!text) return;
  audioPlayer.stop();
  sendCommand(text, "text");
  commandInput.value = "";
});

// ---------------------------------------------------------------------------
// Activity log controls — copy / clear
// ---------------------------------------------------------------------------

const btnLogCopy = document.getElementById("btn-log-copy")!;
const btnLogClear = document.getElementById("btn-log-clear")!;

function clearLog() {
  eventLog.innerHTML = "";
}

btnLogClear.addEventListener("click", (e) => {
  e.stopPropagation();
  clearLog();
});

btnLogCopy.addEventListener("click", async (e) => {
  e.stopPropagation();
  const rows = Array.from(eventLog.querySelectorAll<HTMLElement>(".log-row")).reverse();
  if (rows.length === 0) {
    showToast("Activity log is empty", "info");
    return;
  }
  const text = rows
    .map((row) => {
      const time = row.querySelector("span")?.textContent ?? "";
      const body = row.querySelector("p")?.textContent ?? "";
      return `[${time}] ${body}`;
    })
    .join("\n");
  try {
    await navigator.clipboard.writeText(text);
    showToast("Activity log copied", "success");
  } catch {
    showToast("Clipboard unavailable", "error");
  }
});

// ---------------------------------------------------------------------------
// Keyboard shortcuts + help overlay
// ---------------------------------------------------------------------------

const shortcutsOverlay = document.getElementById("shortcuts-overlay")!;
const btnHelp = document.getElementById("btn-help")!;

function toggleShortcuts(force?: boolean) {
  const show = force ?? shortcutsOverlay.style.display === "none";
  shortcutsOverlay.style.display = show ? "flex" : "none";
}

btnHelp.addEventListener("click", (e) => {
  e.stopPropagation();
  toggleShortcuts();
});

shortcutsOverlay.addEventListener("click", () => toggleShortcuts(false));

document.addEventListener("keydown", (event) => {
  const typing =
    document.activeElement === commandInput ||
    document.activeElement instanceof HTMLInputElement ||
    document.activeElement instanceof HTMLTextAreaElement;

  // Escape always works: stop audio, close overlays/panels, blur input.
  if (event.key === "Escape") {
    if (shortcutsOverlay.style.display !== "none") {
      toggleShortcuts(false);
      return;
    }
    if (menuDropdown.style.display !== "none") {
      toggleSideMenu(false);
      return;
    }
    if (typing) {
      (document.activeElement as HTMLElement).blur();
      return;
    }
    audioPlayer.stop();
    transition("idle");
    return;
  }

  if (typing) return;
  if (event.metaKey || event.ctrlKey || event.altKey) return;

  switch (event.key) {
    case "/":
      event.preventDefault();
      commandInput.focus();
      break;
    case "m":
    case "M":
      (btnMute as HTMLElement).click();
      break;
    case "l":
    case "L":
      clearLog();
      break;
    case ",":
      openSettings();
      break;
    case "?":
      toggleShortcuts();
      break;
  }
});
