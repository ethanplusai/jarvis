/**
 * JARVIS — Settings Panel
 *
 * Overlay panel for API keys, connection status, preferences, and system info.
 * Slides in from the right with glass-morphism styling.
 */

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface StatusResponse {
  claude_code_installed: boolean;
  calendar_accessible: boolean;
  mail_accessible: boolean;
  notes_accessible: boolean;
  memory_count: number;
  task_count: number;
  server_port: number;
  uptime_seconds: number;
  env_keys_set: {
    anthropic: boolean;
    fish_audio: boolean;
    fish_voice_id: boolean;
    user_name: string;
  };
}

interface PreferencesResponse {
  user_name: string;
  honorific: string;
  calendar_accounts: string;
  speech_lang: string;
  tts_backend: string;
  macos_voice: string;
}

interface VoicesResponse {
  voices: { name: string; lang: string }[];
  matching_language: number;
  resolved: string | null;
}

/**
 * Event fired when the speech language changes, so the live recognition
 * session picks it up without a reload. Used instead of importing the voice
 * input directly, which would make settings and main circular.
 */
export const SPEECH_LANG_EVENT = "jarvis:speech-lang";

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let panelEl: HTMLElement | null = null;
let isOpen = false;
let isFirstTimeSetup = false;
let setupStep = 0; // 0=anthropic, 1=fish, 2=name, 3=done

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------

async function apiGet<T>(url: string): Promise<T> {
  const res = await fetch(url);
  return res.json();
}

async function apiPost<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.json();
}

// ---------------------------------------------------------------------------
// Panel HTML
// ---------------------------------------------------------------------------

function buildPanelHTML(): string {
  return `
    <div class="settings-backdrop" id="settings-backdrop"></div>
    <div class="settings-panel" id="settings-panel-inner">
      <div class="settings-header">
        <h2>Settings</h2>
        <button class="settings-close" id="settings-close">&times;</button>
      </div>

      <div class="settings-welcome" id="settings-welcome" style="display:none">
        <p>Welcome to JARVIS. Let's get you set up.</p>
      </div>

      <div class="settings-body">

        <!-- API Keys -->
        <section class="settings-section" id="section-api-keys">
          <h3>API Keys</h3>

          <div class="settings-field">
            <label>Anthropic API Key</label>
            <div class="settings-input-row">
              <input type="password" id="input-anthropic-key" placeholder="sk-ant-..." />
              <button class="settings-btn" id="btn-test-anthropic">Test</button>
              <span class="status-dot" id="status-anthropic"></span>
            </div>
          </div>

          <div class="settings-field">
            <label>Fish Audio API Key</label>
            <div class="settings-input-row">
              <input type="password" id="input-fish-key" placeholder="Fish Audio key..." />
              <button class="settings-btn" id="btn-test-fish">Test</button>
              <span class="status-dot" id="status-fish"></span>
            </div>
          </div>

          <div class="settings-field">
            <label>Fish Voice ID</label>
            <div class="settings-input-row">
              <input type="text" id="input-fish-voice-id" placeholder="612b878b113047d9a770c069c8b4fdfe" />
              <button class="settings-btn" id="btn-save-voice-id">Save</button>
            </div>
          </div>

          <div class="settings-actions">
            <button class="settings-btn primary" id="btn-save-keys">Save Keys</button>
          </div>
        </section>

        <!-- Connection Status -->
        <section class="settings-section" id="section-status">
          <h3>Connection Status</h3>
          <div class="status-grid">
            <div class="status-row"><span class="status-dot" id="status-claude-cli"></span><span>Claude Code CLI</span></div>
            <div class="status-row"><span class="status-dot" id="status-calendar"></span><span>Apple Calendar</span></div>
            <div class="status-row"><span class="status-dot" id="status-mail"></span><span>Apple Mail</span></div>
            <div class="status-row"><span class="status-dot" id="status-notes"></span><span>Apple Notes</span></div>
            <div class="status-row"><span class="status-dot" id="status-server"></span><span>Server</span><span class="status-detail" id="status-server-detail"></span></div>
          </div>
        </section>

        <!-- User Preferences -->
        <section class="settings-section" id="section-preferences">
          <h3>User Preferences</h3>

          <div class="settings-field">
            <label>Your Name</label>
            <input type="text" id="input-user-name" placeholder="Your name" />
          </div>

          <div class="settings-field">
            <label>Honorific</label>
            <input type="text" id="input-honorific" placeholder="sir" list="honorific-suggestions" />
            <datalist id="honorific-suggestions">
              <option value="sir"></option>
              <option value="ma'am"></option>
              <option value="signore"></option>
              <option value="señor"></option>
              <option value="monsieur"></option>
            </datalist>
          </div>

          <div class="settings-field">
            <label>Spoken Language</label>
            <select id="input-speech-lang">
              <option value="en-US">English (US)</option>
              <option value="en-GB">English (UK)</option>
              <option value="it-IT">Italiano</option>
              <option value="es-ES">Español</option>
              <option value="fr-FR">Français</option>
              <option value="de-DE">Deutsch</option>
              <option value="pt-BR">Português (Brasil)</option>
              <option value="nl-NL">Nederlands</option>
              <option value="ja-JP">日本語</option>
              <option value="zh-CN">中文</option>
            </select>
          </div>

          <div class="settings-field">
            <label>Voice</label>
            <select id="input-tts-backend">
              <option value="auto">Auto — Fish Audio if configured, else macOS</option>
              <option value="fish">Fish Audio (the JARVIS voice)</option>
              <option value="macos">macOS (free, offline)</option>
              <option value="none">Silent — captions only</option>
            </select>
          </div>

          <div class="settings-field" id="field-macos-voice">
            <label>macOS Voice</label>
            <div class="settings-input-row">
              <select id="input-macos-voice"></select>
              <button class="settings-btn" id="btn-preview-voice">Preview</button>
            </div>
          </div>

          <div class="settings-field">
            <label>Calendar Accounts</label>
            <textarea id="input-calendar-accounts" rows="2" placeholder="auto (or comma-separated emails)"></textarea>
          </div>

          <div class="settings-actions">
            <button class="settings-btn primary" id="btn-save-prefs">Save Preferences</button>
          </div>
        </section>

        <!-- System Info -->
        <section class="settings-section" id="section-sysinfo">
          <h3>System Info</h3>
          <div class="sysinfo-grid">
            <div class="sysinfo-row"><span class="sysinfo-label">Memory entries</span><span id="sysinfo-memory">--</span></div>
            <div class="sysinfo-row"><span class="sysinfo-label">Tasks</span><span id="sysinfo-tasks">--</span></div>
            <div class="sysinfo-row"><span class="sysinfo-label">Server port</span><span id="sysinfo-port">--</span></div>
            <div class="sysinfo-row"><span class="sysinfo-label">Uptime</span><span id="sysinfo-uptime">--</span></div>
          </div>
        </section>

        <!-- Setup Navigation (first-time only) -->
        <div class="setup-nav" id="setup-nav" style="display:none">
          <button class="settings-btn primary" id="btn-setup-next">Next</button>
        </div>

      </div>
    </div>
  `;
}

// ---------------------------------------------------------------------------
// Panel lifecycle
// ---------------------------------------------------------------------------

function createPanel(): HTMLElement {
  const container = document.createElement("div");
  container.id = "settings-container";
  container.innerHTML = buildPanelHTML();
  document.body.appendChild(container);
  return container;
}

function setDotStatus(id: string, status: "green" | "red" | "yellow" | "off") {
  const dot = document.getElementById(id);
  if (!dot) return;
  dot.className = "status-dot";
  if (status !== "off") dot.classList.add(`status-${status}`);
}

function formatUptime(seconds: number): string {
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return `${h}h ${m}m`;
}

async function loadStatus() {
  try {
    const status = await apiGet<StatusResponse>("/api/settings/status");

    setDotStatus("status-claude-cli", status.claude_code_installed ? "green" : "red");
    setDotStatus("status-calendar", status.calendar_accessible ? "green" : "red");
    setDotStatus("status-mail", status.mail_accessible ? "green" : "red");
    setDotStatus("status-notes", status.notes_accessible ? "green" : "red");
    setDotStatus("status-server", "green");

    const serverDetail = document.getElementById("status-server-detail");
    if (serverDetail) serverDetail.textContent = `port ${status.server_port} | up ${formatUptime(status.uptime_seconds)}`;

    // API key status dots
    setDotStatus("status-anthropic", status.env_keys_set.anthropic ? "green" : "red");
    setDotStatus("status-fish", status.env_keys_set.fish_audio ? "green" : "red");

    // System info
    const memEl = document.getElementById("sysinfo-memory");
    if (memEl) memEl.textContent = String(status.memory_count);
    const taskEl = document.getElementById("sysinfo-tasks");
    if (taskEl) taskEl.textContent = String(status.task_count);
    const portEl = document.getElementById("sysinfo-port");
    if (portEl) portEl.textContent = String(status.server_port);
    const upEl = document.getElementById("sysinfo-uptime");
    if (upEl) upEl.textContent = formatUptime(status.uptime_seconds);

    return status;
  } catch (e) {
    console.error("[settings] failed to load status:", e);
    setDotStatus("status-server", "red");
    return null;
  }
}

async function loadPreferences() {
  try {
    const prefs = await apiGet<PreferencesResponse>("/api/settings/preferences");
    const nameEl = document.getElementById("input-user-name") as HTMLInputElement;
    const honEl = document.getElementById("input-honorific") as HTMLInputElement;
    const calEl = document.getElementById("input-calendar-accounts") as HTMLTextAreaElement;
    const langEl = document.getElementById("input-speech-lang") as HTMLSelectElement;
    if (nameEl) nameEl.value = prefs.user_name || "";
    if (honEl) honEl.value = prefs.honorific || "sir";
    if (calEl) calEl.value = prefs.calendar_accounts || "auto";
    if (langEl) langEl.value = prefs.speech_lang || "en-US";

    const backendEl = document.getElementById("input-tts-backend") as HTMLSelectElement;
    if (backendEl) backendEl.value = prefs.tts_backend || "auto";
    await loadVoices(prefs.macos_voice || "");
    updateVoiceFieldVisibility();
  } catch (e) {
    console.error("[settings] failed to load preferences:", e);
  }
}

/** Only offer the voice picker when a macOS voice could actually be used. */
function updateVoiceFieldVisibility() {
  const backend = (document.getElementById("input-tts-backend") as HTMLSelectElement)?.value;
  const field = document.getElementById("field-macos-voice");
  if (field) field.style.display = backend === "fish" || backend === "none" ? "none" : "";
}

async function loadVoices(selected: string) {
  const voiceEl = document.getElementById("input-macos-voice") as HTMLSelectElement;
  if (!voiceEl) return;
  try {
    const data = await apiGet<VoicesResponse>("/api/settings/voices");
    // "Automatic" first, labelled with what that currently resolves to, so the
    // default is not a mystery.
    const auto = data.resolved ? `Automatic (${data.resolved})` : "Automatic";
    voiceEl.innerHTML =
      `<option value="">${auto}</option>` +
      data.voices
        .map((v, i) => {
          // Voices for the current language are listed first; mark where the
          // rest begin rather than silently mixing them together.
          const divider = i === data.matching_language && i > 0 ? "— other languages — " : "";
          return `<option value="${v.name}">${divider}${v.name} (${v.lang})</option>`;
        })
        .join("");
    voiceEl.value = selected;
  } catch (e) {
    console.error("[settings] failed to load voices:", e);
  }
}

function wireEvents() {
  // Close
  document.getElementById("settings-close")?.addEventListener("click", closeSettings);
  document.getElementById("settings-backdrop")?.addEventListener("click", closeSettings);

  // Save keys
  document.getElementById("btn-save-keys")?.addEventListener("click", async () => {
    const anthropicKey = (document.getElementById("input-anthropic-key") as HTMLInputElement).value.trim();
    const fishKey = (document.getElementById("input-fish-key") as HTMLInputElement).value.trim();

    if (anthropicKey) {
      await apiPost("/api/settings/keys", { key_name: "ANTHROPIC_API_KEY", key_value: anthropicKey });
    }
    if (fishKey) {
      await apiPost("/api/settings/keys", { key_name: "FISH_API_KEY", key_value: fishKey });
    }
    await loadStatus();
  });

  // Save voice ID
  document.getElementById("btn-save-voice-id")?.addEventListener("click", async () => {
    const voiceId = (document.getElementById("input-fish-voice-id") as HTMLInputElement).value.trim();
    if (voiceId) {
      await apiPost("/api/settings/keys", { key_name: "FISH_VOICE_ID", key_value: voiceId });
    }
  });

  // Test Anthropic
  document.getElementById("btn-test-anthropic")?.addEventListener("click", async () => {
    setDotStatus("status-anthropic", "yellow");
    const key = (document.getElementById("input-anthropic-key") as HTMLInputElement).value.trim();
    try {
      const result = await apiPost<{ valid: boolean; error?: string }>("/api/settings/test-anthropic", { key_value: key || undefined });
      setDotStatus("status-anthropic", result.valid ? "green" : "red");
    } catch {
      setDotStatus("status-anthropic", "red");
    }
  });

  // Test Fish
  document.getElementById("btn-test-fish")?.addEventListener("click", async () => {
    setDotStatus("status-fish", "yellow");
    const key = (document.getElementById("input-fish-key") as HTMLInputElement).value.trim();
    try {
      const result = await apiPost<{ valid: boolean; error?: string }>("/api/settings/test-fish", { key_value: key || undefined });
      setDotStatus("status-fish", result.valid ? "green" : "red");
    } catch {
      setDotStatus("status-fish", "red");
    }
  });

  // Save preferences
  document.getElementById("btn-save-prefs")?.addEventListener("click", async () => {
    const user_name = (document.getElementById("input-user-name") as HTMLInputElement).value.trim();
    const honorific = (document.getElementById("input-honorific") as HTMLInputElement).value;
    const calendar_accounts = (document.getElementById("input-calendar-accounts") as HTMLTextAreaElement).value.trim();
    const speech_lang = (document.getElementById("input-speech-lang") as HTMLSelectElement).value;
    const tts_backend = (document.getElementById("input-tts-backend") as HTMLSelectElement).value;
    const macos_voice = (document.getElementById("input-macos-voice") as HTMLSelectElement).value;
    await apiPost("/api/settings/preferences", {
      user_name, honorific, calendar_accounts, speech_lang, tts_backend, macos_voice,
    });
    document.dispatchEvent(new CustomEvent(SPEECH_LANG_EVENT, { detail: speech_lang }));
    await loadStatus();
  });

  // Voice backend — show or hide the voice picker to match
  document.getElementById("input-tts-backend")?.addEventListener("change", updateVoiceFieldVisibility);

  // Audition a voice before committing to it
  document.getElementById("btn-preview-voice")?.addEventListener("click", async (e) => {
    e.stopPropagation();
    const btn = e.currentTarget as HTMLButtonElement;
    const voice = (document.getElementById("input-macos-voice") as HTMLSelectElement).value;
    const original = btn.textContent;
    btn.textContent = "...";
    btn.disabled = true;
    try {
      const res = await apiGet<{ audio: string | null }>(
        `/api/tts-test?voice=${encodeURIComponent(voice)}`
      );
      if (res.audio) {
        const bytes = Uint8Array.from(atob(res.audio), (c) => c.charCodeAt(0));
        const ctx = new AudioContext();
        const buffer = await ctx.decodeAudioData(bytes.buffer);
        const src = ctx.createBufferSource();
        src.buffer = buffer;
        src.connect(ctx.destination);
        src.start();
      }
    } catch (err) {
      console.error("[settings] voice preview failed:", err);
    } finally {
      btn.textContent = original;
      btn.disabled = false;
    }
  });

  // Setup next button
  document.getElementById("btn-setup-next")?.addEventListener("click", advanceSetup);
}

// ---------------------------------------------------------------------------
// First-time setup wizard
// ---------------------------------------------------------------------------

function enterSetupMode() {
  isFirstTimeSetup = true;
  setupStep = 0;

  const welcome = document.getElementById("settings-welcome");
  if (welcome) welcome.style.display = "block";

  const nav = document.getElementById("setup-nav");
  if (nav) nav.style.display = "flex";

  // Hide sections except API keys
  showSetupStep(0);
}

function showSetupStep(step: number) {
  const sections = ["section-api-keys", "section-status", "section-preferences", "section-sysinfo"];
  sections.forEach((id, i) => {
    const el = document.getElementById(id);
    if (!el) return;
    if (step === 0 && i === 0) el.style.display = "";
    else if (step === 1 && i === 0) el.style.display = "";
    else if (step === 2 && i === 2) el.style.display = "";
    else if (step === 3) el.style.display = "";
    else el.style.display = "none";
  });

  const nextBtn = document.getElementById("btn-setup-next");
  if (nextBtn) {
    if (step === 0) nextBtn.textContent = "Next: Test Keys";
    else if (step === 1) nextBtn.textContent = "Next: Set Your Name";
    else if (step === 2) nextBtn.textContent = "Finish Setup";
    else nextBtn.style.display = "none";
  }
}

async function advanceSetup() {
  setupStep++;
  if (setupStep >= 3) {
    // Done — save everything and close
    isFirstTimeSetup = false;
    const welcome = document.getElementById("settings-welcome");
    if (welcome) welcome.style.display = "none";
    const nav = document.getElementById("setup-nav");
    if (nav) nav.style.display = "none";

    // Show all sections
    ["section-api-keys", "section-status", "section-preferences", "section-sysinfo"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.style.display = "";
    });

    closeSettings();
    return;
  }
  showSetupStep(setupStep);
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

export async function openSettings() {
  if (isOpen) return;
  isOpen = true;

  if (!panelEl) {
    panelEl = createPanel();
    wireEvents();
  }

  panelEl.style.display = "block";

  // Trigger animation
  requestAnimationFrame(() => {
    panelEl!.classList.add("open");
  });

  // Load data
  const status = await loadStatus();
  await loadPreferences();

  // Check for first-time setup
  if (status && !status.env_keys_set.anthropic) {
    enterSetupMode();
  }
}

export function closeSettings() {
  if (!panelEl || !isOpen) return;
  isOpen = false;
  panelEl.classList.remove("open");
  setTimeout(() => {
    if (panelEl) panelEl.style.display = "none";
  }, 300);
}

export function isSettingsOpen(): boolean {
  return isOpen;
}

/**
 * Check if first-time setup is needed and auto-open.
 */
export async function checkFirstTimeSetup(): Promise<boolean> {
  try {
    const status = await apiGet<StatusResponse>("/api/settings/status");
    if (!status.env_keys_set.anthropic) {
      openSettings();
      return true;
    }
  } catch {
    // Server not ready yet, skip
  }
  return false;
}
