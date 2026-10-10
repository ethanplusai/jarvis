/**
 * JARVIS — Settings Panel
 *
 * Overlay panel for API keys, connection status, preferences, and system info.
 * Slides in from the right with glass-morphism styling.
 */

import { loadListenMode, saveListenMode, type ListenMode } from "./listenmode";
import {
  describeTelegram, pairingEndedText, pairingOutcome, pairingPromptParts,
  type PairingPromptParts, type TelegramStatus,
} from "./telegramstatus";
import { armed } from "./uibits";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface StatusResponse {
  claude_code_installed: boolean;
  server_port: number;
  uptime_seconds: number;
  env_keys_set: {
    fish_audio: boolean;
    fish_voice_id: boolean;
    user_name: string;
    kapso_api_key?: boolean;
    whatsapp_phone_number_id?: boolean;
    whatsapp_owner_number?: boolean;
    telegram_bot_token?: boolean;
    telegram_owner_id?: boolean;
  };
}

interface WhatsAppStatus {
  configured: boolean;
  touched: boolean;
  missing: string[];
  issue: string | null;
  owner: string;
  template: string;
  inbound: boolean;
  approvals: boolean;
  voice_notes: boolean;
  polling: boolean;
  last_sent: number | null;
  last_received: number | null;
  window_open_until: number | null;
  last_error: string | null;
  ignored_strangers: number;
}

interface PreferencesResponse {
  user_name: string;
  honorific: string;
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let panelEl: HTMLElement | null = null;
let isOpen = false;
let isFirstTimeSetup = false;
let setupStep = 0; // 0=fish, 1=name, 2=done

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------

async function apiGet<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Request failed (${res.status}).`);
  return res.json();
}

async function apiPost<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`Request failed (${res.status}).`);
  return res.json();
}

function feedback(message: string) {
  const node = document.getElementById("settings-feedback");
  if (node) node.textContent = message;
}

async function saveKeys(required = false) {
  const input = document.getElementById("input-fish-key") as HTMLInputElement;
  const key = input.value.trim();
  if (!key) {
    if (required) {
      const status = await apiGet<StatusResponse>("/api/settings/status");
      if (!status.env_keys_set.fish_audio) throw new Error("Enter your Fish Audio key to continue.");
    }
    return;
  }
  await apiPost("/api/settings/keys", { key_name: "FISH_API_KEY", key_value: key });
  input.value = "";
}

async function savePreferences() {
  const user_name = (document.getElementById("input-user-name") as HTMLInputElement).value.trim();
  const honorific = (document.getElementById("input-honorific") as HTMLSelectElement).value;
  await apiPost("/api/settings/preferences", { user_name, honorific });
}

/** Every mutation is single-flight and failures stay visible in the panel. */
function onAction(id: string, action: () => Promise<void>) {
  document.getElementById(id)?.addEventListener("click", async (event) => {
    const button = event.currentTarget as HTMLButtonElement;
    if (button.disabled) return;
    button.disabled = true;
    feedback("");
    try { await action(); }
    catch (error) { feedback(error instanceof Error ? error.message : "Could not save. Please try again."); }
    finally { button.disabled = false; }
  });
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
        <p id="settings-feedback" role="status" aria-live="polite"></p>

        <!-- API Keys -->
        <section class="settings-section" id="section-api-keys">
          <h3>API Keys</h3>

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
            <div class="status-row"><span class="status-dot" id="status-server"></span><span>Server</span><span class="status-detail" id="status-server-detail"></span></div>
          </div>
        </section>

        <!-- Telegram -->
        <section class="settings-section" id="section-telegram">
          <h3>Telegram</h3>
          <p class="settings-hint">The easy way to give him your phone: a bot from @BotFather,
            no phone number, no Facebook. Approval cards with Approve and Reject buttons,
            sessions waiting on you, failed or finished work (the urgent ones as a voice note in
            his voice) — and you can text him back. Calls are not available; see
            docs/telegram.md.</p>

          <div class="settings-field">
            <label>Bot token</label>
            <div class="settings-input-row">
              <input type="password" id="input-telegram-token" placeholder="123456789:AA… from @BotFather" autocomplete="off" />
              <span class="status-dot" id="status-telegram-token"></span>
            </div>
            <p class="settings-hint">In Telegram, open @BotFather, send <code>/newbot</code>, and paste the token it gives you.</p>
          </div>

          <div class="settings-field">
            <label>Your Telegram id</label>
            <div class="settings-input-row">
              <input type="text" id="input-telegram-owner" placeholder="filled in by pairing" inputmode="numeric" />
              <span class="status-dot" id="status-telegram-owner"></span>
            </div>
            <p class="settings-hint">You do not need to know it: press Pair and send the code to the bot from your phone.</p>
          </div>

          <div class="settings-actions">
            <button class="settings-btn primary" id="btn-save-telegram">Save Telegram</button>
            <button class="settings-btn" id="btn-pair-telegram">Pair</button>
            <button class="settings-btn" id="btn-test-telegram">Send test message</button>
            <label class="settings-inline"><input type="checkbox" id="input-telegram-test-voice" /> as a voice note too</label>
          </div>
          <p class="settings-pairing" id="telegram-pairing" aria-live="polite"></p>
          <p class="settings-hint" id="telegram-status" aria-live="polite"></p>
        </section>

        <!-- WhatsApp -->
        <section class="settings-section" id="section-whatsapp">
          <h3>WhatsApp</h3>
          <p class="settings-hint">The other line: a number from Kapso (kapso.ai, free plan;
            a dedicated number needs a Facebook login). The same cards, announcements and
            replies as Telegram. Calls are not available; see docs/whatsapp.md.</p>

          <div class="settings-field">
            <label>Kapso API key</label>
            <div class="settings-input-row">
              <input type="password" id="input-kapso-key" placeholder="Kapso API key..." autocomplete="off" />
              <span class="status-dot" id="status-kapso-key"></span>
            </div>
          </div>

          <div class="settings-field">
            <label>Phone number ID</label>
            <div class="settings-input-row">
              <input type="text" id="input-whatsapp-number-id" placeholder="123456789012345" inputmode="numeric" />
              <span class="status-dot" id="status-whatsapp-number-id"></span>
            </div>
            <p class="settings-hint">Meta's id for the number, not the number itself.
              <code>python scripts/whatsapp_setup.py numbers</code> prints it.</p>
          </div>

          <div class="settings-field">
            <label>Your WhatsApp number</label>
            <div class="settings-input-row">
              <input type="tel" id="input-whatsapp-owner" placeholder="+14155550132" />
              <span class="status-dot" id="status-whatsapp-owner"></span>
            </div>
          </div>

          <div class="settings-actions">
            <button class="settings-btn primary" id="btn-save-whatsapp">Save WhatsApp</button>
            <button class="settings-btn" id="btn-test-whatsapp">Send test message</button>
            <label class="settings-inline"><input type="checkbox" id="input-whatsapp-test-voice" /> as a voice note too</label>
          </div>
          <p class="settings-hint" id="whatsapp-status" aria-live="polite"></p>
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
            <select id="input-honorific">
              <option value="sir">Sir</option>
              <option value="ma'am">Ma'am</option>
              <option value="none">None</option>
            </select>
          </div>

          <div class="settings-field">
            <label>Listening</label>
            <select id="input-listen-mode" aria-label="Listening">
              <option value="hold">Hold Space to talk (default)</option>
              <option value="open">Microphone always open</option>
            </select>
            <p class="settings-hint">Hold-to-talk means anything else said in the room — a call, a TV,
              another assistant — is not taken as a command. M mutes the microphone, V mutes his voice,
              Esc stops him. Saved in this browser and applied at once.</p>
          </div>

          <div class="settings-actions">
            <button class="settings-btn primary" id="btn-save-prefs">Save Preferences</button>
          </div>
        </section>

        <!-- System Info -->
        <section class="settings-section" id="section-sysinfo">
          <h3>System Info</h3>
          <div class="sysinfo-grid">
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
    setDotStatus("status-server", "green");

    const serverDetail = document.getElementById("status-server-detail");
    if (serverDetail) serverDetail.textContent = `port ${status.server_port} | up ${formatUptime(status.uptime_seconds)}`;

    // API key status dots
    setDotStatus("status-fish", status.env_keys_set.fish_audio ? "green" : "red");
    // WhatsApp is optional: a missing setting is "off", not a fault, unless
    // one of the three is set and the others are not, which the status line
    // below (from /api/whatsapp/status) explains in words.
    const wa = status.env_keys_set;
    const touched = Boolean(wa.kapso_api_key || wa.whatsapp_phone_number_id || wa.whatsapp_owner_number);
    setDotStatus("status-kapso-key", wa.kapso_api_key ? "green" : touched ? "red" : "off");
    setDotStatus("status-whatsapp-number-id", wa.whatsapp_phone_number_id ? "green" : touched ? "red" : "off");
    setDotStatus("status-whatsapp-owner", wa.whatsapp_owner_number ? "green" : touched ? "red" : "off");
    void loadWhatsAppStatus();
    const tgTouched = Boolean(wa.telegram_bot_token || wa.telegram_owner_id);
    setDotStatus("status-telegram-token", wa.telegram_bot_token ? "green" : tgTouched ? "red" : "off");
    setDotStatus("status-telegram-owner", wa.telegram_owner_id ? "green" : tgTouched ? "yellow" : "off");
    void loadTelegramStatus();

    // System info
    const portEl = document.getElementById("sysinfo-port");
    if (portEl) portEl.textContent = String(status.server_port);
    const upEl = document.getElementById("sysinfo-uptime");
    if (upEl) upEl.textContent = formatUptime(status.uptime_seconds);

    return status;
  } catch (e) {
    console.error("[settings] failed to load status:", e);
    // EVERY dot, not just the server's. The CLI and Fish dots are drawn from
    // fields of the answer that never arrived, so leaving them green states
    // as fact something this call failed to find out. "off" is the absence
    // of a reading, which is what we have.
    setDotStatus("status-server", "red");
    setDotStatus("status-claude-cli", "off");
    setDotStatus("status-fish", "off");
    const serverDetail = document.getElementById("status-server-detail");
    if (serverDetail) serverDetail.textContent = "no answer from the server";
    for (const id of ["sysinfo-port", "sysinfo-uptime"]) {
      const node = document.getElementById(id);
      if (node) node.textContent = "—";
    }
    return null;
  }
}

/** The listening mode is the browser's, not the server's: it is a property
 * of this microphone and this room. Wired before any fetch, because the
 * select is on screen from the moment the dialog opens — a choice made
 * while the status request was still out used to land on no handler. */
function loadListenModeSetting() {
  const listenEl = document.getElementById("input-listen-mode") as HTMLSelectElement | null;
  if (listenEl) {
    listenEl.value = loadListenMode(safeStorage());
    listenEl.onchange = () => {
      const mode: ListenMode = listenEl.value === "open" ? "open" : "hold";
      saveListenMode(safeStorage(), mode);
      window.dispatchEvent(new CustomEvent<ListenMode>("jarvis-listen-mode", { detail: mode }));
      feedback(mode === "open" ? "Microphone open: he listens all the time." : "Hold Space to talk.");
    };
  }
}

async function loadPreferences() {
  try {
    const prefs = await apiGet<PreferencesResponse>("/api/settings/preferences");
    const nameEl = document.getElementById("input-user-name") as HTMLInputElement;
    const honEl = document.getElementById("input-honorific") as HTMLSelectElement;
    if (nameEl) nameEl.value = prefs.user_name || "";
    if (honEl) honEl.value = prefs.honorific || "sir";
  } catch (e) {
    console.error("[settings] failed to load preferences:", e);
  }
}

function safeStorage(): Storage | null {
  try { return localStorage; } catch { return null; }
}

function ago(epoch: number | null): string {
  if (!epoch) return "never";
  const seconds = Math.max(0, Date.now() / 1000 - epoch);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

/** The line under the WhatsApp fields, in words: what is set, what is
 * missing, whether he is reading the line, and how long the 24-hour
 * window has left — the one fact that decides whether a message from him
 * can go out at all. */
async function loadWhatsAppStatus() {
  const node = document.getElementById("whatsapp-status");
  if (!node) return;
  try {
    const s = await apiGet<WhatsAppStatus>("/api/whatsapp/status");
    if (!s.touched) { node.textContent = "Not set up. Optional."; return; }
    if (s.missing.length) { node.textContent = `Half set up: ${s.missing.join(", ")} still missing.`; return; }
    if (s.issue) { node.textContent = `Cannot be used: ${s.issue}`; return; }
    const parts = [`Configured for ${s.owner}.`];
    parts.push(s.polling ? "Reading the line." : s.inbound ? "Not reading the line yet (restart?)." : "Not reading the line (WHATSAPP_INBOUND=0).");
    parts.push(`Last sent ${ago(s.last_sent)}; last received ${ago(s.last_received)}.`);
    if (s.window_open_until && s.window_open_until * 1000 > Date.now()) {
      const hours = Math.max(1, Math.round((s.window_open_until * 1000 - Date.now()) / 3600000));
      parts.push(`He can write freely for about ${hours}h more.`);
    } else {
      parts.push(s.template
        ? `The 24-hour window is shut; he will use the ${s.template} template.`
        : "The 24-hour window is shut and no template is set: message his number from your phone, or run scripts/whatsapp_setup.py template.");
    }
    if (!s.approvals) parts.push("Approvals from the phone are off.");
    if (s.ignored_strangers) parts.push(`${s.ignored_strangers} message${s.ignored_strangers === 1 ? "" : "s"} from other numbers ignored.`);
    if (s.last_error) parts.push(`Last error: ${s.last_error}`);
    node.textContent = parts.join(" ");
  } catch (e) {
    node.textContent = "Could not read the WhatsApp status.";
  }
}

async function saveWhatsApp() {
  const fields: [string, string][] = [
    ["input-kapso-key", "KAPSO_API_KEY"],
    ["input-whatsapp-number-id", "WHATSAPP_PHONE_NUMBER_ID"],
    ["input-whatsapp-owner", "WHATSAPP_OWNER_NUMBER"],
  ];
  let saved = 0;
  for (const [id, key] of fields) {
    const input = document.getElementById(id) as HTMLInputElement | null;
    const value = input?.value.trim() ?? "";
    if (!value) continue;
    const result = await apiPost<{ success: boolean; error?: string }>("/api/settings/keys", { key_name: key, key_value: value });
    if (!result.success) throw new Error(result.error || `Could not save ${key}.`);
    saved++;
    if (input) input.value = "";
  }
  if (!saved) throw new Error("Enter at least one of the three to save.");
}

let pairingWatch: number | null = null;
// Each Pair press is a generation. A newer press SUPERSEDES an older one (its
// code already replaced the old one on the server); closing the panel or
// unpairing ABANDONS the current one, whose code — even if its request is
// still in flight — must be withdrawn, never shown, never watched.
let pairingGeneration = 0;
let pairingAbandoned = -1;

/** The line under the Telegram fields, in words (see telegramstatus.ts). */
async function loadTelegramStatus(): Promise<TelegramStatus | null> {
  const node = document.getElementById("telegram-status");
  if (!node) return null;
  try {
    const s = await apiGet<TelegramStatus>("/api/telegram/status");
    node.textContent = describeTelegram(s, ago);
    return s;
  } catch (e) {
    node.textContent = "Could not read the Telegram status.";
    return null;
  }
}

async function saveTelegram() {
  const fields: [string, string][] = [
    ["input-telegram-token", "TELEGRAM_BOT_TOKEN"],
    ["input-telegram-owner", "TELEGRAM_OWNER_ID"],
  ];
  let saved = 0;
  for (const [id, key] of fields) {
    const input = document.getElementById(id) as HTMLInputElement | null;
    const value = input?.value.trim() ?? "";
    if (!value) continue;
    const result = await apiPost<{ success: boolean; error?: string }>("/api/settings/keys", { key_name: key, key_value: value });
    if (!result.success) throw new Error(result.error || `Could not save ${key}.`);
    saved++;
    if (input) input.value = "";
  }
  if (!saved) throw new Error("Paste the bot token to save.");
}

/** Mint a code, show it, and watch until THAT code has an outcome — paired,
 * withdrawn, lapsed, or locked after wrong guesses. Not `configured`: that
 * is already true while the phone being replaced still owns the line.
 * Returns whether the code was shown — false when the pairing was abandoned
 * while its request was in flight. */
async function pairTelegram(): Promise<boolean> {
  const generation = ++pairingGeneration;
  const superseded = () => generation !== pairingGeneration;
  const dropped = () => pairingAbandoned === generation || !isOpen;
  const abandoned = () => superseded() || dropped();
  const node = document.getElementById("telegram-pairing");
  const res = await fetch("/api/telegram/pair", { method: "POST" });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : `Could not start pairing (${res.status}).`);
  const code: string = body.code;
  const serial: number = body.serial;
  const before = await loadTelegramStatus();
  if (abandoned()) {
    // Nobody is looking at this code any more: show nothing, and withdraw
    // it on the server — it, by its serial, never a newer one.
    void fetch("/api/telegram/pair/cancel", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ serial }),
    });
    return false;
  }
  const shown = pairingPromptParts(code, body.bot_username ?? "", Boolean(before?.configured));
  if (node) {
    showPairingCode(node, shown);
    node.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
  stopPairingWatch();
  const startedAt = Date.now();
  // This generation's own interval: a stale tick from an older one must
  // never stop the newer watch.
  const watch = window.setInterval(async () => {
    const stop = () => {
      window.clearInterval(watch);
      if (pairingWatch === watch) pairingWatch = null;
    };
    if (abandoned()) { stop(); return; }
    const s = await loadTelegramStatus();
    if (abandoned()) { stop(); return; }
    const outcome = pairingOutcome(s, serial) || (Date.now() - startedAt > 10 * 60_000 ? "lapsed" : "");
    if (outcome) {
      stop();
      if (node) node.textContent = pairingEndedText(outcome, s?.pairing.note ?? "");
      await loadStatus();
    } else if (node && s?.pairing.note) {
      showPairingCode(node, shown, s.pairing.note);
    }
  }, 3000);
  pairingWatch = watch;
  return true;
}

/** A live code on the page: the instruction, then the six digits on a line
 * of their own in a size nobody can miss, then how long it lasts (and the
 * server's note, when there is one). Built from text nodes only. */
function showPairingCode(node: HTMLElement, parts: PairingPromptParts, note = "") {
  const code = document.createElement("span");
  code.className = "pairing-code";
  code.textContent = parts.code;
  const tail = parts.tail + (note ? ` — ${note}.` : "");
  node.replaceChildren(document.createTextNode(parts.lead), code, document.createTextNode(tail));
}

/** The pairing watch polls the status every three seconds; it has no
 * business doing so once the panel is shut or the line unpaired. */
function stopPairingWatch() {
  if (pairingWatch !== null) window.clearInterval(pairingWatch);
  pairingWatch = null;
}

/** Closing the panel or unpairing abandons a pairing, even one whose
 * request has not come back yet. */
function abandonPairing() {
  pairingAbandoned = pairingGeneration;
  stopPairingWatch();
  const node = document.getElementById("telegram-pairing");
  if (node) node.textContent = "";
}

/** Unpair: two clicks, and the phone this line belongs to is forgotten —
 * nothing is read from it or sent to it until a phone pairs again. */
function wireUnpairTelegram() {
  const actions = document.querySelector<HTMLElement>("#section-telegram .settings-actions");
  if (!actions) return;
  const button = armed(actions, "Unpair", "Confirm unpair", async () => {
    abandonPairing();       // unpair withdraws a live code on the server too
    const res = await fetch("/api/telegram/unpair", { method: "POST" });
    if (!res.ok) throw new Error(`Could not unpair (${res.status}).`);
    const body = await res.json().catch(() => ({}));
    button.disabled = false;
    button.textContent = "Unpair";
    button.classList.remove("armed");
    feedback(body.note ? `Unpaired, but ${body.note}.`
      : "Unpaired. Nothing is read from or sent to Telegram until a phone pairs again.");
    await loadStatus();
  }, feedback, { className: "settings-btn", title: "Forget the phone this line belongs to", busyText: null });
}

function wireEvents() {
  // Close
  document.getElementById("settings-close")?.addEventListener("click", closeSettings);
  document.getElementById("settings-backdrop")?.addEventListener("click", closeSettings);

  onAction("btn-save-keys", async () => {
    await saveKeys(true);
    await loadStatus();
    feedback("Saved. The next sentence uses this key.");
  });

  onAction("btn-save-voice-id", async () => {
    const voiceId = (document.getElementById("input-fish-voice-id") as HTMLInputElement).value.trim();
    if (!voiceId) throw new Error("Enter a voice ID to save.");
    await apiPost("/api/settings/keys", { key_name: "FISH_VOICE_ID", key_value: voiceId });
    feedback("Voice saved. The next sentence uses this voice.");
  });

  onAction("btn-test-fish", async () => {
    setDotStatus("status-fish", "yellow");
    const key = (document.getElementById("input-fish-key") as HTMLInputElement).value.trim();
    try {
      const result = await apiPost<{ valid: boolean; error?: string }>("/api/settings/test-fish", { key_value: key || undefined });
      setDotStatus("status-fish", result.valid ? "green" : "red");
      feedback(result.valid ? "Voice test passed." : result.error || "Voice test failed.");
    } catch (error) {
      setDotStatus("status-fish", "red");
      throw error;
    }
  });

  onAction("btn-save-prefs", async () => {
    await savePreferences();
    feedback("Preferences saved.");
  });

  onAction("btn-save-whatsapp", async () => {
    await saveWhatsApp();
    await loadStatus();
    feedback("WhatsApp settings saved. He starts reading the line within a minute; no restart needed.");
  });

  onAction("btn-test-whatsapp", async () => {
    const voice = (document.getElementById("input-whatsapp-test-voice") as HTMLInputElement | null)?.checked ?? false;
    const res = await fetch("/api/whatsapp/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ voice }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : `Test failed (${res.status}).`);
    feedback(body.voice_note ? "Sent, with a voice note. Check your phone."
      : body.via === "template" ? "Sent through the template (the 24-hour window was shut). Check your phone."
      : "Sent. Check your phone.");
    await loadWhatsAppStatus();
  });

  onAction("btn-save-telegram", async () => {
    await saveTelegram();
    await loadStatus();
    feedback("Telegram settings saved. Now press Pair, or send a test if already paired.");
  });

  onAction("btn-pair-telegram", async () => {
    if (await pairTelegram()) {
      feedback("Pairing code shown below. Send it to the bot from your phone — whichever phone sends it takes the line.");
    }
  });
  wireUnpairTelegram();

  onAction("btn-test-telegram", async () => {
    const voice = (document.getElementById("input-telegram-test-voice") as HTMLInputElement | null)?.checked ?? false;
    const res = await fetch("/api/telegram/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ voice }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : `Test failed (${res.status}).`);
    feedback(body.voice_note ? "Sent, with a voice note. Check Telegram." : "Sent. Check Telegram.");
    await loadTelegramStatus();
  });

  onAction("btn-setup-next", advanceSetup);
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
  const sections = ["section-api-keys", "section-preferences"];
  sections.forEach((id, i) => {
    const el = document.getElementById(id);
    if (!el) return;
    if (step === 0 && i === 0) el.style.display = "";
    else if (step === 1 && i === 1) el.style.display = "";
    else el.style.display = "none";
  });

  const nextBtn = document.getElementById("btn-setup-next");
  if (nextBtn) {
    if (step === 0) nextBtn.textContent = "Next: Set Your Name";
    else if (step === 1) nextBtn.textContent = "Finish Setup";
    else nextBtn.style.display = "none";
  }
}

async function advanceSetup() {
  // Commit each step before advancing. A rejected save leaves its inputs visible.
  if (setupStep === 0) await saveKeys(true);
  else await savePreferences();
  setupStep++;
  if (setupStep >= 2) {
    // Done — save everything and close
    isFirstTimeSetup = false;
    const welcome = document.getElementById("settings-welcome");
    if (welcome) welcome.style.display = "none";
    const nav = document.getElementById("setup-nav");
    if (nav) nav.style.display = "none";

    // Show all sections
    ["section-api-keys", "section-status", "section-telegram", "section-whatsapp", "section-preferences", "section-sysinfo"].forEach((id) => {
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
  loadListenModeSetting();
  const status = await loadStatus();
  await loadPreferences();

  // Check for first-time setup
  if (status && !status.env_keys_set.fish_audio) {
    enterSetupMode();
  }
}

export function closeSettings() {
  if (!panelEl || !isOpen) return;
  isOpen = false;
  abandonPairing();         // a code shown once, not left lying about
  panelEl.classList.remove("open");
  setTimeout(() => {
    if (panelEl && !isOpen) panelEl.style.display = "none";
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
    if (!status.env_keys_set.fish_audio) {
      openSettings();
      return true;
    }
  } catch {
    // Server not ready yet, skip
  }
  return false;
}
