/**
 * JARVIS — Settings Panel
 *
 * Overlay panel for API keys, connection status, preferences, and system info.
 * Slides in from the right with glass-morphism styling.
 */

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface ApiProvider {
  id: string;
  name: string;
  env_key: string;
  category: string;
  description: string;
  placeholder: string;
  docs_url: string;
  optional: boolean;
  configured: boolean;
}

interface SkillPack {
  id: string;
  name: string;
  category: string;
  description: string;
  bundled: boolean;
  install_hint: string;
}

interface Skill {
  slug: string;
  name: string;
  category: string;
  description: string;
  when_to_use: string;
  instructions: string;
  executable: boolean;
  enabled: boolean;
}

interface SkillCategory {
  category: string;
  total: number;
  enabled: number;
}

interface McpServer {
  id: string;
  name: string;
  category: string;
  description: string;
  capabilities: string[];
  connected: boolean;
  status: string;
  auth_required: boolean;
  auth_present: boolean;
  auth_env: string;
  needs_config: boolean;
  config_hint: string;
  docs_url: string;
}

interface LlmProviderStatus {
  id: string;
  label: string;
  configured: boolean;
  needs_key: boolean;
  models: string[];
  default_model: string;
  active_model: string;
  is_ollama: boolean;
}

interface LlmStatus {
  providers: LlmProviderStatus[];
  active: string;
  active_model: string;
  ollama_base_url: string;
}

interface TtsProviderStatus {
  id: string;
  label: string;
  configured: boolean;
  voice_id: string;
}

interface TtsStatus {
  providers: TtsProviderStatus[];
  active: string;
}

interface StatusResponse {
  claude_code_installed: boolean;
  calendar_accessible: boolean;
  mail_accessible: boolean;
  notes_accessible: boolean;
  memory_count: number;
  task_count: number;
  server_port: number;
  uptime_seconds: number;
  platform: string;
  env_keys_set: {
    anthropic: boolean;
    fish_audio: boolean;
    fish_voice_id: boolean;
    user_name: string;
    interface_language: string;
    response_language: string;
  };
  providers: ApiProvider[];
  llm?: LlmStatus;
  tts?: TtsStatus;
  skills: SkillPack[];
  skill_counts?: { total: number; enabled: number };
  mcp_connected?: number;
  onboarding?: { status: string; turns: number };
}

interface VoiceSample {
  name: string;
  mime_type: string;
  duration_seconds: number;
  size_bytes: number;
  created_at: number;
  download_url: string;
}

interface VoiceSamplesResponse {
  samples: VoiceSample[];
}

interface PreferencesResponse {
  user_name: string;
  honorific: string;
  calendar_accounts: string;
  interface_language: string;
  response_language: string;
  personality_preset: string;
  personality_brief: string;
  humor_level: string;
  formality_level: string;
  proactive_mode: string;
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let panelEl: HTMLElement | null = null;
let isOpen = false;
let isFirstTimeSetup = false;
let setupStep = 0; // 0=keys, 1=engine, 2=skills, 3=tools, 4=profile, 5=personality
let currentLanguage = localStorage.getItem("jarvis.uiLanguage") || "en";

const ALL_SECTIONS = ["section-api-keys", "section-engine", "section-skills", "section-mcp", "section-status", "section-preferences", "section-personality", "section-sysinfo", "section-memory", "section-workflow"];

const TAB_SECTIONS: Record<string, string[]> = {
  onboarding: ["section-api-keys"],
  skills: ["section-skills"],
  mcp: ["section-mcp"],
  engine: ["section-engine"],
  profile: ["section-preferences"],
  personality: ["section-personality"],
  system: ["section-status", "section-sysinfo"],
  memory: ["section-memory"],
  workflow: ["section-workflow"],
};

function showTab(tabName: string) {
  // Update active class on nav links
  document.querySelectorAll(".nav-link").forEach((link) => {
    const target = (link as HTMLElement).dataset.targetTab;
    link.classList.toggle("active", target === tabName);
  });

  // Show only relevant sections
  const visibleSections = TAB_SECTIONS[tabName] || [];
  ALL_SECTIONS.forEach((id) => {
    const el = document.getElementById(id);
    if (el) {
      el.style.display = visibleSections.includes(id) ? "" : "none";
    }
  });

  // Hide or show the onboarding guide depending on if we are in onboarding tab
  const guide = document.getElementById("onboarding-guide");
  if (guide) {
    guide.style.display = tabName === "onboarding" ? "" : "none";
  }

  // Update header title
  const titleEl = document.getElementById("settings-title");
  if (titleEl) {
    const titles: Record<string, string> = {
      onboarding: currentLanguage === "pl" ? "Start, model i API" : "Start, Model & APIs",
      skills: currentLanguage === "pl" ? "Zarządzanie Umiejętnościami" : "Skills Management",
      mcp: currentLanguage === "pl" ? "Narzędzia MCP" : "Connected Tools (MCP)",
      engine: currentLanguage === "pl" ? "Silnik LLM & Głos" : "LLM Engine & Voice",
      profile: currentLanguage === "pl" ? "Profil & Preferencje" : "User Profile & Settings",
      personality: currentLanguage === "pl" ? "Laboratorium Osobowości" : "Personality Lab",
      memory: currentLanguage === "pl" ? "Pamięć & Baza Wiedzy" : "Memory & Knowledge Base",
      workflow: currentLanguage === "pl" ? "Workflow & Agent" : "Workflow & Agent",
      system: currentLanguage === "pl" ? "Status Systemu & Metryki" : "System Status & Metrics",
    };
    titleEl.textContent = titles[tabName] || "Settings";
  }

  // Lazy-load tab data
  if (tabName === "memory") {
    loadMemories();
    loadMemoryTasks();
  } else if (tabName === "workflow") {
    loadAgentStatus();
    loadConversationStats();
  }
}

const I18N: Record<string, Record<string, string>> = {
  en: {
    settings: "Settings", keys: "Required APIs", engine: "Model & Voice", skills: "Skills", tools: "Connected Tools", status: "Connection Status", prefs: "User Preferences", personality: "Personality Lab", system: "System Info", save: "Save Preferences", generate: "Generate JARVIS Personality", language: "Menu Language", voiceLanguage: "Response Language", name: "Your Name", honorific: "Honorific", calendar: "Calendar Accounts", preset: "Personality Preset", humor: "Humor", formality: "Formality", initiative: "Initiative", brief: "Custom Personality Brief", nextSkills: "Next: Skills", nextTools: "Next: Tools", nextProfile: "Next: Profile", nextPersonality: "Next: Personality", nextEngine: "Next: Model", finish: "Launch JARVIS",
  },
  pl: {
    settings: "Ustawienia", keys: "Wymagane API", engine: "Model i Głos", skills: "Umiejętności", tools: "Narzędzia", status: "Status Połączeń", prefs: "Preferencje", personality: "Laboratorium Osobowości", system: "Informacje", save: "Zapisz Preferencje", generate: "Wygeneruj Osobowość JARVIS-a", language: "Język Menu", voiceLanguage: "Język Odpowiedzi", name: "Twoje Imię", honorific: "Zwrot", calendar: "Konta Kalendarza", preset: "Styl Osobowości", humor: "Humor", formality: "Formalność", initiative: "Inicjatywa", brief: "Własny Opis Osobowości", nextSkills: "Dalej: Umiejętności", nextTools: "Dalej: Narzędzia", nextProfile: "Dalej: Profil", nextPersonality: "Dalej: Osobowość", nextEngine: "Dalej: Model", finish: "Uruchom JARVIS-a",
  },
};

function t(key: string): string {
  return I18N[currentLanguage]?.[key] || I18N.en[key] || key;
}

// ---------------------------------------------------------------------------
// Localization
// ---------------------------------------------------------------------------

function applyLanguage(lang?: string) {
  currentLanguage = lang || currentLanguage || "en";
  localStorage.setItem("jarvis.uiLanguage", currentLanguage);
  document.documentElement.lang = currentLanguage;
  document.querySelectorAll<HTMLElement>("[data-i18n]").forEach((el) => {
    const key = el.dataset.i18n || "";
    el.textContent = t(key);
  });
  const ui = document.getElementById("input-ui-language") as HTMLSelectElement | null;
  if (ui) ui.value = currentLanguage;
}

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
      
      <!-- Left side: Navigation Sidebar -->
      <aside class="sidebar-nav">
        <div>
          <div class="sidebar-logo">
            <div class="logo-orb"></div>
            <span>JARVIS</span>
          </div>
          
          <nav class="nav-links">
            <button class="nav-link active" data-target-tab="onboarding">
              <span class="nav-icon">🚀</span>
              <span>Start + API</span>
            </button>
            <button class="nav-link" data-target-tab="skills">
              <span class="nav-icon">⚡</span>
              <span>Umiejętności</span>
            </button>
            <button class="nav-link" data-target-tab="mcp">
              <span class="nav-icon">🔌</span>
              <span>Narzędzia MCP</span>
            </button>
            <button class="nav-link" data-target-tab="engine">
              <span class="nav-icon">🧠</span>
              <span>Mózg (LLM)</span>
            </button>
            <button class="nav-link" data-target-tab="profile">
              <span class="nav-icon">👤</span>
              <span>Profil i Język</span>
            </button>
            <button class="nav-link" data-target-tab="personality">
              <span class="nav-icon">🎭</span>
              <span>Osobowość</span>
            </button>
            <button class="nav-link" data-target-tab="memory">
              <span class="nav-icon">🧠</span>
              <span>Pamięć</span>
            </button>
            <button class="nav-link" data-target-tab="workflow">
              <span class="nav-icon">🔄</span>
              <span>Workflow</span>
            </button>
            <button class="nav-link" data-target-tab="system">
              <span class="nav-icon">📊</span>
              <span>Status & Info</span>
            </button>
          </nav>
        </div>
        
        <div class="sidebar-footer">
          <small>JARVIS v0.1.0</small>
        </div>
      </aside>

      <!-- Right side: Content Area -->
      <div class="sidebar-content">
        <div class="settings-header">
          <h2 id="settings-title">Settings</h2>
          <button class="settings-close" id="settings-close">&times;</button>
        </div>

        <div class="settings-welcome" id="settings-welcome" style="display:none">
          <div class="onboarding-hero">
            <span class="onboarding-kicker">AI Evolution Labs</span>
            <strong>Choose the brain, add only the keys you need, then tune the cockpit.</strong>
            <p>Minimum setup is the reasoning model plus optional voice. Everything else is grouped as optional connectors you can enable later.</p>
          </div>
          <div class="onboarding-steps" id="onboarding-steps" style="display:none">
            <span class="active">1 Keys</span><span>2 Engine</span><span>3 Skills</span><span>4 Tools</span><span>5 Profile</span><span>6 Personality</span>
          </div>
        </div>

        <div class="settings-body">

          <!-- Krótka instrukcja onboardingowa dla użytkownika -->
          <div class="onboarding-guide-card" id="onboarding-guide">
            <h4>Witaj w JARVIS Control Center! 🤖</h4>
            <p>Start jest prosty: wybierz model w zakładce <strong>Model i Głos</strong>, dodaj minimalne klucze API poniżej, a potem włącz widgety, umiejętności i narzędzia. JARVIS może później wysyłać do Control Center własne podsumowania, newsy, pogodę, statystyki i alerty.</p>
            <div class="onboarding-guide-steps">
              <div class="onboarding-guide-step">
                <span class="onboarding-guide-step-num">1</span>
                <div class="onboarding-guide-step-text">
                  <strong>Must-have — mózg asystenta:</strong> dodaj Anthropic API key albo wybierz skonfigurowany alternatywny model w zakładce Model i Głos. Claude jest nadal rekomendowany do akcji narzędziowych.
                </div>
              </div>
              <div class="onboarding-guide-step">
                <span class="onboarding-guide-step-num">2</span>
                <div class="onboarding-guide-step-text">
                  <strong>Optional — głos i integracje:</strong> Fish Audio/ElevenLabs, research, giełda, newsy i inne providery możesz dodać od razu albo później. Bez nich tekstowy JARVIS nadal działa.
                </div>
              </div>
              <div class="onboarding-guide-step">
                <span class="onboarding-guide-step-num">3</span>
                <div class="onboarding-guide-step-text">
                  Kliknij <strong>Save Keys</strong>, potem <strong>Test</strong>. Zielone kropki oznaczają gotowość; reszta connectorów jest opcjonalna i widoczna w sekcji Optional poniżej.
                </div>
              </div>
            </div>
          </div>

          <!-- API Keys -->
          <section class="settings-section" id="section-api-keys">
            <h3 data-i18n="keys">Required APIs</h3>
            <p class="section-note"><strong>Must:</strong> at least one reasoning provider. <strong>Recommended:</strong> Fish Audio for voice. Optional connectors below unlock news, market research, alternate models, and premium voices.</p>

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

            <div class="api-group-title">Optional connectors</div>
            <div class="provider-grid" id="provider-grid"></div>

            <div class="settings-actions">
              <button class="settings-btn primary" id="btn-save-keys">Save Keys</button>
            </div>
          </section>

          <!-- Engine: active brain + voice -->
          <section class="settings-section" id="section-engine">
            <h3 data-i18n="engine">Model & Voice</h3>
            <p class="section-note">Choose the model JARVIS uses for conversation. Claude is recommended for tool actions; OpenAI, Google, DeepSeek, or Ollama can be selected when configured.</p>

            <div class="settings-field">
              <label>Active Brain</label>
              <div class="settings-input-row">
                <select id="select-llm-provider"></select>
                <span class="status-dot" id="status-llm"></span>
              </div>
            </div>

            <div class="settings-field">
              <label>Model</label>
              <div class="settings-input-row">
                <select id="select-llm-model"></select>
                <button class="settings-btn" id="btn-test-llm">Test</button>
              </div>
            </div>

            <div class="settings-field" id="field-ollama-url" style="display:none">
              <label>Ollama Base URL</label>
              <div class="settings-input-row">
                <input type="text" id="input-ollama-url" placeholder="http://localhost:11434" />
                <button class="settings-btn" id="btn-save-ollama-url">Save</button>
              </div>
            </div>

            <div class="settings-field">
              <label>Active Voice</label>
              <div class="settings-input-row">
                <select id="select-tts-provider"></select>
                <button class="settings-btn" id="btn-test-tts">Test</button>
                <span class="status-dot" id="status-tts"></span>
              </div>
            </div>

            <div class="settings-field" id="field-eleven-voice" style="display:none">
              <label>ElevenLabs Voice ID</label>
              <div class="settings-input-row">
                <input type="text" id="input-eleven-voice" placeholder="JBFqnCBsd6RMkjVDRZzb" />
                <button class="settings-btn" id="btn-save-eleven-voice">Save</button>
              </div>
            </div>

            <div class="voice-lab-card">
              <div>
                <span class="voice-lab-kicker">🎙️ Voice Capture Lab</span>
                <h4>Record a reference voice sample</h4>
                <p>Capture a clean 10–30 second sample, play it back, save it locally, and use the sample with your chosen voice provider when creating a custom JARVIS voice.</p>
              </div>
              <div class="settings-field compact-field">
                <label>Speech capture language</label>
                <select id="select-speech-language">
                  <option value="en-US">English (US)</option>
                  <option value="en-GB">English (UK)</option>
                  <option value="pl-PL">Polski</option>
                  <option value="es-ES">Español</option>
                  <option value="de-DE">Deutsch</option>
                  <option value="fr-FR">Français</option>
                  <option value="it-IT">Italiano</option>
                  <option value="pt-PT">Português</option>
                  <option value="uk-UA">Українська</option>
                </select>
              </div>
              <div class="voice-recorder-row">
                <button class="settings-btn primary" id="btn-voice-record">Start Recording</button>
                <button class="settings-btn" id="btn-voice-stop" disabled>Stop</button>
                <button class="settings-btn" id="btn-voice-save" disabled>Save Sample</button>
                <span id="voice-record-status">Ready for calibration, sir.</span>
              </div>
              <audio id="voice-sample-player" controls style="display:none"></audio>
              <div id="voice-sample-list" class="mini-list voice-samples"></div>
            </div>
          </section>

          <!-- Skills -->
          <section class="settings-section" id="section-skills">
            <h3><span data-i18n="skills">Skills</span> <span class="skills-count" id="skills-count"></span></h3>
            <p class="section-note">Enable the capabilities JARVIS should be ready to use. Active skills are loaded into context so he performs the task well. Some skills can run and produce a file.</p>
            <input type="search" id="skills-search" class="skills-search" placeholder="Search 100 skills — invoice, email, hiring..." />
            <div class="skills-chips" id="skills-chips"></div>
            <div class="skills-list" id="skills-list"></div>
          </section>

          <!-- Connected Tools (MCP) -->
          <section class="settings-section" id="section-mcp">
            <h3><span data-i18n="tools">Connected Tools</span> <span class="skills-count" id="mcp-count"></span></h3>
            <p class="section-note">Connect external tools over MCP so JARVIS can reach your email, docs, CRM, and database. Add the matching API key in Settings where a tool needs auth.</p>
            <div class="mcp-list" id="mcp-list"></div>
          </section>

          <!-- Connection Status -->
          <section class="settings-section" id="section-status">
            <h3 data-i18n="status">Connection Status</h3>
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
            <h3 data-i18n="prefs">User Preferences</h3>

            <div class="settings-field">
              <label data-i18n="name">Your Name</label>
              <input type="text" id="input-user-name" placeholder="Your name" />
            </div>

            <div class="settings-field">
              <label data-i18n="honorific">Honorific</label>
              <select id="input-honorific">
                <option value="sir">Sir</option>
                <option value="ma'am">Ma'am</option>
                <option value="none">None</option>
              </select>
            </div>

            <div class="settings-field">
              <label data-i18n="calendar">Calendar Accounts</label>
              <textarea id="input-calendar-accounts" rows="2" placeholder="auto (or comma-separated emails)"></textarea>
            </div>

            <div class="settings-field">
              <label data-i18n="language">Menu Language</label>
              <select id="input-ui-language">
                <option value="en">English</option>
                <option value="pl">Polski</option>
              </select>
            </div>

            <div class="settings-field">
              <label data-i18n="voiceLanguage">Response Language</label>
              <select id="input-response-language">
                <option value="en">English</option>
                <option value="pl">Polski</option>
                <option value="es">Español</option>
                <option value="de">Deutsch</option>
                <option value="fr">Français</option>
                <option value="it">Italiano</option>
                <option value="pt">Português</option>
                <option value="uk">Ukranian</option>
              </select>
            </div>

            <div class="settings-actions">
              <button class="settings-btn primary" id="btn-save-prefs" data-i18n="save">Save Preferences</button>
            </div>
          </section>

          <!-- Personality -->
          <section class="settings-section" id="section-personality">
            <h3 data-i18n="personality">Personality Lab</h3>
            <p class="section-note">Default is Stark-style JARVIS: loyal, elegant, concise, British, dryly funny. Tune him into an executive operator, coach, engineer, or custom assistant.</p>

            <div class="personality-preview">
              <span>Default protocol</span>
              <strong>“Good evening, sir. Systems are standing by; naturally, I have taken the liberty of preparing the interesting bits first.”</strong>
            </div>

            <div class="settings-field">
              <label data-i18n="preset">Personality Preset</label>
              <select id="input-personality-preset">
                <option value="stark">Stark JARVIS (default)</option>
                <option value="executive">Executive Operator</option>
                <option value="coach">Supportive Coach</option>
                <option value="engineer">Senior Engineer</option>
                <option value="creative">Creative Director</option>
              </select>
            </div>

            <div class="settings-field">
              <label data-i18n="humor">Humor</label>
              <select id="input-humor-level">
                <option value="balanced">Balanced dry wit</option>
                <option value="subtle">Subtle</option>
                <option value="playful">Playful</option>
              </select>
            </div>

            <div class="settings-field">
              <label data-i18n="formality">Formality</label>
              <select id="input-formality-level">
                <option value="butler">British butler</option>
                <option value="professional">Professional</option>
                <option value="casual">Casual</option>
              </select>
            </div>

            <div class="settings-field">
              <label data-i18n="initiative">Initiative</label>
              <select id="input-proactive-mode">
                <option value="smart">Smart suggestions</option>
                <option value="quiet">Ask first</option>
                <option value="high">Proactive mission control</option>
              </select>
            </div>

            <div class="settings-field">
              <label data-i18n="brief">Custom Personality Brief</label>
              <textarea id="input-personality-brief" rows="5" placeholder="e.g. Be concise in Polish, call me Szefie, make sharp but friendly comments, and prioritize business automation."></textarea>
            </div>

            <div class="settings-actions split-actions">
              <button class="settings-btn" id="btn-generate-personality" data-i18n="generate">Generate JARVIS Personality</button>
              <button class="settings-btn primary" id="btn-save-personality" data-i18n="save">Save Preferences</button>
            </div>
          </section>

          <!-- Memory & Knowledge Base -->
          <section class="settings-section" id="section-memory">
            <h3>🧠 Pamięć JARVIS-a</h3>
            
            <div class="memory-stats-grid" id="memory-stats-grid">
              <div class="stat-card stat-total"><div class="stat-number" id="mem-stat-total">0</div><div class="stat-label">Wspomnienia</div></div>
              <div class="stat-card stat-facts"><div class="stat-number" id="mem-stat-facts">0</div><div class="stat-label">Fakty</div></div>
              <div class="stat-card stat-prefs"><div class="stat-number" id="mem-stat-prefs">0</div><div class="stat-label">Preferencje</div></div>
              <div class="stat-card stat-24h"><div class="stat-number" id="mem-stat-recent">0</div><div class="stat-label">Ostatnie 24h</div></div>
            </div>
            
            <div class="memory-controls">
              <div class="memory-search-row">
                <input type="text" id="memory-search-input" placeholder="Szukaj we wspomnieniach..." />
                <button class="settings-btn" id="btn-memory-search">🔍</button>
                <button class="settings-btn" id="btn-memory-refresh">↻</button>
              </div>
              <div class="memory-filter-row">
                <button class="filter-chip active" data-mem-filter="all">Wszystkie</button>
                <button class="filter-chip" data-mem-filter="fact">Fakty</button>
                <button class="filter-chip" data-mem-filter="preference">Preferencje</button>
                <button class="filter-chip" data-mem-filter="project">Projekty</button>
                <button class="filter-chip" data-mem-filter="person">Osoby</button>
                <button class="filter-chip" data-mem-filter="decision">Decyzje</button>
              </div>
            </div>

            <div class="memory-list" id="memory-list">
              <div class="mini-row"><strong>Ładowanie wspomnień...</strong></div>
            </div>

            <div class="memory-add-form">
              <h4>Dodaj wspomnienie</h4>
              <div class="settings-field">
                <input type="text" id="memory-add-content" placeholder="Treść wspomnienia..." />
              </div>
              <div class="memory-add-row">
                <select id="memory-add-type">
                  <option value="fact">Fakt</option>
                  <option value="preference">Preferencja</option>
                  <option value="project">Projekt</option>
                  <option value="person">Osoba</option>
                  <option value="decision">Decyzja</option>
                </select>
                <select id="memory-add-importance">
                  <option value="3">Niska ważność</option>
                  <option value="5" selected>Średnia</option>
                  <option value="7">Wysoka</option>
                  <option value="9">Krytyczna</option>
                </select>
                <button class="settings-btn primary" id="btn-memory-add">+ Dodaj</button>
              </div>
            </div>

            <div class="memory-tasks-section">
              <h4>📋 Otwarte Zadania</h4>
              <div id="memory-task-list" class="memory-task-list">
                <div class="mini-row"><strong>Ładowanie...</strong></div>
              </div>
            </div>
          </section>

          <!-- Workflow & Agent -->
          <section class="settings-section" id="section-workflow">
            <h3>🔄 Workflow & Agent</h3>
            
            <div class="agent-status-bar">
              <div class="agent-state-pill" id="agent-state-pill">
                <span class="agent-state-dot"></span>
                <span id="agent-state-label">Gotowy</span>
              </div>
              <div class="agent-meta">
                <small>Akcje: <span id="agent-action-count">0</span></small>
                <small>Uptime: <span id="agent-uptime">0m</span></small>
              </div>
            </div>

            <div class="goal-input-section">
              <h4>🎯 Nowy Cel</h4>
              <p class="section-note">Opisz cel, a JARVIS autonomicznie go rozłoży na kroki i wykona.</p>
              <div class="goal-input-row">
                <input type="text" id="goal-input" placeholder="np. Przygotuj raport sprzedaży za ten miesiąc..." />
                <select id="goal-priority">
                  <option value="high">🔴 Wysoki</option>
                  <option value="medium" selected>🟡 Średni</option>
                  <option value="low">🟢 Niski</option>
                </select>
                <button class="settings-btn primary" id="btn-submit-goal">Wykonaj</button>
              </div>
            </div>

            <div class="agent-history-section">
              <h4>📜 Historia Akcji</h4>
              <div id="agent-history-list" class="agent-history-list">
                <div class="mini-row"><strong>Brak akcji</strong><small>Akcje agenta pojawią się tutaj.</small></div>
              </div>
            </div>

            <div class="conversation-summary-section">
              <h4>💬 Sesja</h4>
              <div class="conversation-stats" id="conversation-stats">
                <div class="stat-card stat-total"><div class="stat-number" id="conv-tokens">0</div><div class="stat-label">Tokeny sesji</div></div>
                <div class="stat-card stat-facts"><div class="stat-number" id="conv-cost">$0.00</div><div class="stat-label">Koszt dziś</div></div>
                <div class="stat-card stat-prefs"><div class="stat-number" id="conv-uptime">0m</div><div class="stat-label">Uptime</div></div>
              </div>
            </div>
          </section>

          <!-- System Info -->
          <section class="settings-section" id="section-sysinfo">
            <h3 data-i18n="system">System Info</h3>
            <div class="sysinfo-grid">
              <div class="sysinfo-row"><span class="sysinfo-label">Memory entries</span><span id="sysinfo-memory">--</span></div>
              <div class="sysinfo-row"><span class="sysinfo-label">Tasks</span><span id="sysinfo-tasks">--</span></div>
              <div class="sysinfo-row"><span class="sysinfo-label">Server port</span><span id="sysinfo-port">--</span></div>
              <div class="sysinfo-row"><span class="sysinfo-label">Uptime</span><span id="sysinfo-uptime">--</span></div>
              <div class="sysinfo-row"><span class="sysinfo-label">Platform</span><span id="sysinfo-platform">--</span></div>
            </div>
          </section>

          <!-- Setup Navigation (first-time only) -->
          <div class="setup-nav" id="setup-nav" style="display:none">
            <button class="settings-btn primary" id="btn-setup-next">Next</button>
          </div>

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
  applyLanguage(currentLanguage);
  return container;
}

function setDotStatus(id: string, status: "green" | "red" | "yellow" | "off") {
  const dot = document.getElementById(id);
  if (!dot) return;
  dot.className = "status-dot";
  if (status !== "off") dot.classList.add(`status-${status}`);
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "'": "&#39;",
    '"': "&quot;",
  }[char] || char));
}

function formatUptime(seconds: number): string {
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return `${h}h ${m}m`;
}

function renderProviders(providers: ApiProvider[]) {
  const grid = document.getElementById("provider-grid");
  if (!grid) return;
  grid.innerHTML = providers
    .filter((provider) => !["anthropic", "fish_audio"].includes(provider.id))
    .map((provider) => `
      <div class="provider-card ${provider.configured ? "configured" : ""}">
        <div class="provider-card-header">
          <span class="status-dot ${provider.configured ? "status-green" : ""}" id="status-provider-${escapeHtml(provider.id)}"></span>
          <div>
            <strong>${escapeHtml(provider.name)}</strong>
            <small>${escapeHtml(provider.category)}</small>
          </div>
          ${provider.docs_url ? `<a href="${escapeHtml(provider.docs_url)}" target="_blank" rel="noreferrer">Docs</a>` : ""}
        </div>
        <p>${escapeHtml(provider.description)}</p>
        <input type="password" data-provider-key="${escapeHtml(provider.env_key)}" placeholder="${escapeHtml(provider.placeholder || provider.env_key)}" />
      </div>
    `)
    .join("");
}

// --- Engine (active brain + voice) -----------------------------------------

let llmStatusCache: LlmStatus | null = null;

function populateModelSelect(providerId: string, selected?: string) {
  const sel = document.getElementById("select-llm-model") as HTMLSelectElement | null;
  if (!sel || !llmStatusCache) return;
  const provider = llmStatusCache.providers.find((p) => p.id === providerId);
  const models = provider?.models?.length ? provider.models : (provider ? [provider.active_model] : []);
  const want = selected || provider?.active_model || provider?.default_model || "";
  sel.innerHTML = models
    .map((m) => `<option value="${escapeHtml(m)}" ${m === want ? "selected" : ""}>${escapeHtml(m)}</option>`)
    .join("");
}

async function refreshOllamaModels(selected?: string) {
  const sel = document.getElementById("select-llm-model") as HTMLSelectElement | null;
  if (!sel) return;
  sel.innerHTML = `<option>loading…</option>`;
  try {
    const data = await apiGet<{ models: string[]; error?: string }>("/api/settings/ollama-models");
    const models = data.models?.length ? data.models : ["llama3.2"];
    sel.innerHTML = models
      .map((m) => `<option value="${escapeHtml(m)}" ${m === selected ? "selected" : ""}>${escapeHtml(m)}</option>`)
      .join("");
    if (data.error) console.warn("[engine] ollama:", data.error);
  } catch (e) {
    sel.innerHTML = `<option value="llama3.2">llama3.2</option>`;
    console.warn("[engine] ollama models failed:", e);
  }
}

function renderEngine(status: StatusResponse) {
  const llm = status.llm;
  const tts = status.tts;
  if (llm) {
    llmStatusCache = llm;
    const sel = document.getElementById("select-llm-provider") as HTMLSelectElement | null;
    if (sel) {
      sel.innerHTML = llm.providers
        .map((p) => {
          const tag = p.is_ollama ? " (local)" : p.configured ? "" : " — no key";
          return `<option value="${escapeHtml(p.id)}" ${p.id === llm.active ? "selected" : ""} ${!p.configured && !p.is_ollama ? "disabled" : ""}>${escapeHtml(p.label)}${tag}</option>`;
        })
        .join("");
    }
    const active = llm.providers.find((p) => p.id === llm.active);
    setDotStatus("status-llm", active && active.configured ? "green" : "red");
    const urlField = document.getElementById("field-ollama-url");
    if (urlField) urlField.style.display = llm.active === "ollama" ? "" : "none";
    const urlInput = document.getElementById("input-ollama-url") as HTMLInputElement | null;
    if (urlInput && !urlInput.value) urlInput.value = llm.ollama_base_url || "";
    if (llm.active === "ollama") refreshOllamaModels(llm.active_model);
    else populateModelSelect(llm.active, llm.active_model);
  }
  if (tts) {
    const sel = document.getElementById("select-tts-provider") as HTMLSelectElement | null;
    if (sel) {
      sel.innerHTML = tts.providers
        .map((p) => `<option value="${escapeHtml(p.id)}" ${p.id === tts.active ? "selected" : ""}>${escapeHtml(p.label)}${p.configured ? "" : " — no key"}</option>`)
        .join("");
    }
    const active = tts.providers.find((p) => p.id === tts.active);
    setDotStatus("status-tts", active && active.configured ? "green" : "red");
    const voiceField = document.getElementById("field-eleven-voice");
    if (voiceField) voiceField.style.display = tts.active === "elevenlabs" ? "" : "none";
    const voiceInput = document.getElementById("input-eleven-voice") as HTMLInputElement | null;
    const eleven = tts.providers.find((p) => p.id === "elevenlabs");
    if (voiceInput && !voiceInput.value && eleven) voiceInput.value = eleven.voice_id || "";
  }
}

async function savePersonalization(user_name: string, honorific: string, calendar_accounts: string) {
  const interface_language = (document.getElementById("input-ui-language") as HTMLSelectElement).value;
  const response_language = (document.getElementById("input-response-language") as HTMLSelectElement).value;
  const personality_preset = (document.getElementById("input-personality-preset") as HTMLSelectElement).value;
  const personality_brief = (document.getElementById("input-personality-brief") as HTMLTextAreaElement).value.trim();
  const humor_level = (document.getElementById("input-humor-level") as HTMLSelectElement).value;
  const formality_level = (document.getElementById("input-formality-level") as HTMLSelectElement).value;
  const proactive_mode = (document.getElementById("input-proactive-mode") as HTMLSelectElement).value;
  await apiPost("/api/settings/preferences", { user_name, honorific, calendar_accounts, interface_language, response_language, personality_preset, personality_brief, humor_level, formality_level, proactive_mode });
  applyLanguage(interface_language);
}

function generatePersonalityBrief(preset: string, language: string, honorific: string): string {
  const base = "Act like Stark-style JARVIS by default: elegant British butler energy, calm under pressure, loyal, concise, technically competent, and dryly funny. Address me naturally with the selected honorific, but do not overuse it.";
  const variants: Record<string, string> = {
    stark: "Keep the cinematic mission-control tone. Anticipate what I need, report status with numbers first, and use subtle dry wit when something is obvious.",
    executive: "Prioritize decisions, deadlines, money, risk, and next actions. Challenge weak plans politely and summarize options like a chief of staff.",
    coach: "Be motivating and structured. Turn vague goals into small actions, celebrate progress briefly, and keep momentum without becoming cheesy.",
    engineer: "Be precise, test-oriented, skeptical of assumptions, and comfortable explaining technical tradeoffs. Prefer reproducible steps and evidence.",
    creative: "Bring bold ideas, visual language, naming options, and brand polish. Keep the wit sharp and the output practical.",
  };
  const languageLine = language === "pl" ? "Respond in Polish by default, unless I ask for another language." : `Respond in ${language.toUpperCase()} by default, unless I ask for another language.`;
  return `${base} ${variants[preset] || variants.stark} ${languageLine} My honorific is ${honorific}.`;
}

// --- Skills browser --------------------------------------------------------

let allSkills: Skill[] = [];
let skillCategories: SkillCategory[] = [];
let activeCategory = "All";
let skillSearchTerm = "";

async function loadSkills() {
  try {
    const data = await apiGet<{ skills: Skill[]; categories: SkillCategory[]; counts: { total: number; enabled: number } }>("/api/skills");
    allSkills = data.skills || [];
    skillCategories = data.categories || [];
    const countEl = document.getElementById("skills-count");
    if (countEl) countEl.textContent = `${data.counts.enabled}/${data.counts.total} active`;
    renderChips();
    renderSkillsList();
  } catch (e) {
    console.error("[settings] failed to load skills:", e);
  }
}

function renderChips() {
  const chips = document.getElementById("skills-chips");
  if (!chips) return;
  const cats = ["All", ...skillCategories.map((c) => c.category)];
  chips.innerHTML = cats
    .map((c) => {
      const meta = skillCategories.find((x) => x.category === c);
      const count = c === "All" ? "" : ` <em>${meta?.enabled ?? 0}/${meta?.total ?? 0}</em>`;
      return `<button class="skill-chip ${c === activeCategory ? "active" : ""}" data-cat="${escapeHtml(c)}">${escapeHtml(c)}${count}</button>`;
    })
    .join("");
}

function renderSkillsList() {
  const list = document.getElementById("skills-list");
  if (!list) return;
  const term = skillSearchTerm.toLowerCase();
  const filtered = allSkills.filter((s) => {
    const matchesCat = activeCategory === "All" || s.category === activeCategory;
    const matchesTerm =
      !term ||
      `${s.name} ${s.description} ${s.instructions} ${s.category}`.toLowerCase().includes(term);
    return matchesCat && matchesTerm;
  });

  if (filtered.length === 0) {
    list.innerHTML = `<p class="section-note">No skills match "${escapeHtml(skillSearchTerm)}".</p>`;
    return;
  }

  list.innerHTML = filtered
    .map(
      (s) => `
    <div class="skill-row ${s.enabled ? "enabled" : ""}">
      <div class="skill-row-main">
        <strong>${escapeHtml(s.name)}${s.executable ? ' <span class="skill-exec">runs</span>' : ""}</strong>
        <small>${escapeHtml(s.category)}</small>
        <p>${escapeHtml(s.description)}</p>
      </div>
      <button class="skill-toggle ${s.enabled ? "on" : ""}" data-slug="${escapeHtml(s.slug)}" role="switch" aria-checked="${s.enabled}" aria-label="Toggle ${escapeHtml(s.name)}"><span></span></button>
    </div>`
    )
    .join("");
}

async function toggleSkill(slug: string) {
  const skill = allSkills.find((s) => s.slug === slug);
  if (!skill) return;
  const next = !skill.enabled;
  skill.enabled = next; // optimistic
  renderSkillsList();
  try {
    await apiPost(`/api/skills/${slug}/toggle`, { enabled: next });
    await loadSkills();
  } catch {
    skill.enabled = !next;
    renderSkillsList();
  }
}

// --- MCP tools -------------------------------------------------------------

async function loadMcp() {
  try {
    const data = await apiGet<{ servers: McpServer[] }>("/api/mcp");
    const servers = data.servers || [];
    const countEl = document.getElementById("mcp-count");
    if (countEl) countEl.textContent = `${servers.filter((s) => s.connected).length} connected`;
    renderMcp(servers);
  } catch (e) {
    console.error("[settings] failed to load MCP servers:", e);
  }
}

function renderMcp(servers: McpServer[]) {
  const list = document.getElementById("mcp-list");
  if (!list) return;
  list.innerHTML = servers
    .map(
      (s) => `
    <div class="mcp-card ${s.connected ? "connected" : ""}">
      <div class="mcp-card-head">
        <span class="status-dot ${s.connected ? "status-green" : ""}"></span>
        <div>
          <strong>${escapeHtml(s.name)}</strong>
          <small>${escapeHtml(s.category)}${s.capabilities.length ? " · " + escapeHtml(s.capabilities.join(", ")) : ""}</small>
        </div>
        <button class="settings-btn mcp-btn" data-mcp="${escapeHtml(s.id)}" data-connected="${s.connected}">${s.connected ? "Disconnect" : "Connect"}</button>
      </div>
      <p>${escapeHtml(s.description)}</p>
      ${s.needs_config ? `
      <div class="mcp-auth">
        <input type="text" class="mcp-config-input" data-mcp-config="${escapeHtml(s.id)}"
               placeholder="MCP server URL or stdio command" aria-label="${escapeHtml(s.name)} server URL or command" />
      </div>
      ${s.config_hint ? `<small class="mcp-hint">${escapeHtml(s.config_hint)}</small>` : ""}` : ""}
      ${s.auth_required && !s.auth_present ? `
      <div class="mcp-auth">
        <input type="password" class="mcp-token-input" data-token-for="${escapeHtml(s.id)}"
               placeholder="${escapeHtml(s.auth_env)}" aria-label="${escapeHtml(s.name)} API token" />
        <button class="settings-btn mcp-token-save" data-save-token="${escapeHtml(s.id)}" data-env="${escapeHtml(s.auth_env)}">Save key</button>
      </div>` : ""}
      ${s.auth_required && s.auth_present ? `<small class="mcp-hint mcp-ok">API key on file (${escapeHtml(s.auth_env)}).</small>` : ""}
    </div>`
    )
    .join("");
}

async function toggleMcp(id: string, connected: boolean) {
  try {
    const config: Record<string, string> = {};
    if (!connected) {
      const input = document.querySelector<HTMLInputElement>(`input[data-mcp-config="${id}"]`);
      const value = input?.value.trim() || "";
      if (value) {
        if (/^https?:\/\//i.test(value)) config.url = value;
        else config.command = value;
      }
    }
    await apiPost(`/api/mcp/${id}/${connected ? "disconnect" : "connect"}`, { config });
    await loadMcp();
  } catch (e) {
    console.error("[settings] MCP toggle failed:", e);
  }
}

async function saveMcpToken(id: string, envKey: string) {
  const input = document.querySelector<HTMLInputElement>(`input[data-token-for="${id}"]`);
  const value = input?.value.trim() || "";
  if (!value || !envKey) return;
  try {
    await apiPost("/api/settings/keys", { key_name: envKey, key_value: value });
    await loadMcp();
  } catch (e) {
    console.error("[settings] MCP token save failed:", e);
  }
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

    renderProviders(status.providers || []);
    renderEngine(status);
    loadSkills();
    loadMcp();

    // System info
    const memEl = document.getElementById("sysinfo-memory");
    if (memEl) memEl.textContent = String(status.memory_count);
    const taskEl = document.getElementById("sysinfo-tasks");
    if (taskEl) taskEl.textContent = String(status.task_count);
    const portEl = document.getElementById("sysinfo-port");
    if (portEl) portEl.textContent = String(status.server_port);
    const upEl = document.getElementById("sysinfo-uptime");
    if (upEl) upEl.textContent = formatUptime(status.uptime_seconds);
    const platformEl = document.getElementById("sysinfo-platform");
    if (platformEl) platformEl.textContent = status.platform || "unknown";

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
    const nameEl = document.getElementById("input-user-name") as HTMLInputElement | null;
    const honEl = document.getElementById("input-honorific") as HTMLSelectElement | null;
    const calEl = document.getElementById("input-calendar-accounts") as HTMLTextAreaElement | null;
    const uiEl = document.getElementById("input-ui-language") as HTMLSelectElement | null;
    const responseEl = document.getElementById("input-response-language") as HTMLSelectElement | null;
    const presetEl = document.getElementById("input-personality-preset") as HTMLSelectElement | null;
    const briefEl = document.getElementById("input-personality-brief") as HTMLTextAreaElement | null;
    const humorEl = document.getElementById("input-humor-level") as HTMLSelectElement | null;
    const formalityEl = document.getElementById("input-formality-level") as HTMLSelectElement | null;
    const proactiveEl = document.getElementById("input-proactive-mode") as HTMLSelectElement | null;
    if (nameEl) nameEl.value = prefs.user_name || "";
    if (honEl) honEl.value = prefs.honorific || "sir";
    if (calEl) calEl.value = prefs.calendar_accounts || "auto";
    if (uiEl) uiEl.value = prefs.interface_language || "en";
    if (responseEl) responseEl.value = prefs.response_language || "en";
    if (presetEl) presetEl.value = prefs.personality_preset || "stark";
    if (briefEl) briefEl.value = prefs.personality_brief || "";
    if (humorEl) humorEl.value = prefs.humor_level || "balanced";
    if (formalityEl) formalityEl.value = prefs.formality_level || "butler";
    if (proactiveEl) proactiveEl.value = prefs.proactive_mode || "smart";
    applyLanguage(prefs.interface_language || "en");
  } catch (e) {
    console.error("[settings] failed to load preferences:", e);
  }
}

function wireEvents() {
  // Close
  document.getElementById("settings-close")?.addEventListener("click", closeSettings);
  document.getElementById("settings-backdrop")?.addEventListener("click", closeSettings);

  // Tab navigation click handlers
  document.querySelectorAll(".nav-link").forEach((link) => {
    link.addEventListener("click", (e) => {
      const target = (e.currentTarget as HTMLElement).dataset.targetTab;
      if (target) {
        showTab(target);
      }
    });
  });

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

    const providerInputs = Array.from(document.querySelectorAll<HTMLInputElement>("[data-provider-key]"));
    await Promise.all(providerInputs.map((input) => {
      const value = input.value.trim();
      if (!value) return Promise.resolve();
      return apiPost("/api/settings/keys", { key_name: input.dataset.providerKey, key_value: value });
    }));

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

  // --- Engine: active brain ---
  document.getElementById("select-llm-provider")?.addEventListener("change", async (e) => {
    const provider = (e.target as HTMLSelectElement).value;
    const urlField = document.getElementById("field-ollama-url");
    if (urlField) urlField.style.display = provider === "ollama" ? "" : "none";
    if (provider === "ollama") await refreshOllamaModels();
    else populateModelSelect(provider);
    const model = (document.getElementById("select-llm-model") as HTMLSelectElement)?.value;
    await apiPost("/api/settings/active", { llm_provider: provider, llm_model: model });
    await loadStatus();
  });

  document.getElementById("select-llm-model")?.addEventListener("change", async (e) => {
    const provider = (document.getElementById("select-llm-provider") as HTMLSelectElement)?.value;
    const model = (e.target as HTMLSelectElement).value;
    if (provider) await apiPost("/api/settings/active", { llm_provider: provider, llm_model: model });
  });

  document.getElementById("btn-test-llm")?.addEventListener("click", async () => {
    const provider = (document.getElementById("select-llm-provider") as HTMLSelectElement)?.value;
    if (!provider) return;
    setDotStatus("status-llm", "yellow");
    try {
      const result = await apiPost<{ valid: boolean; error?: string }>("/api/settings/test-provider", { provider });
      setDotStatus("status-llm", result.valid ? "green" : "red");
    } catch {
      setDotStatus("status-llm", "red");
    }
  });

  document.getElementById("btn-save-ollama-url")?.addEventListener("click", async () => {
    const url = (document.getElementById("input-ollama-url") as HTMLInputElement).value.trim();
    if (url) {
      await apiPost("/api/settings/keys", { key_name: "OLLAMA_BASE_URL", key_value: url });
      await refreshOllamaModels();
    }
  });

  // --- Engine: active voice ---
  document.getElementById("select-tts-provider")?.addEventListener("change", async (e) => {
    const provider = (e.target as HTMLSelectElement).value;
    const voiceField = document.getElementById("field-eleven-voice");
    if (voiceField) voiceField.style.display = provider === "elevenlabs" ? "" : "none";
    await apiPost("/api/settings/active", { tts_provider: provider });
    await loadStatus();
  });

  document.getElementById("btn-test-tts")?.addEventListener("click", async () => {
    const provider = (document.getElementById("select-tts-provider") as HTMLSelectElement)?.value;
    if (!provider) return;
    setDotStatus("status-tts", "yellow");
    try {
      const result = await apiPost<{ valid: boolean; error?: string }>("/api/settings/test-provider", { provider });
      setDotStatus("status-tts", result.valid ? "green" : "red");
    } catch {
      setDotStatus("status-tts", "red");
    }
  });

  document.getElementById("btn-save-eleven-voice")?.addEventListener("click", async () => {
    const voice = (document.getElementById("input-eleven-voice") as HTMLInputElement).value.trim();
    if (voice) await apiPost("/api/settings/keys", { key_name: "ELEVENLABS_VOICE_ID", key_value: voice });
  });

  wireVoiceCaptureLab();

  // Save preferences
  document.getElementById("btn-save-prefs")?.addEventListener("click", async () => {
    const user_name = (document.getElementById("input-user-name") as HTMLInputElement).value.trim();
    const honorific = (document.getElementById("input-honorific") as HTMLSelectElement).value;
    const calendar_accounts = (document.getElementById("input-calendar-accounts") as HTMLTextAreaElement).value.trim();
    await savePersonalization(user_name, honorific, calendar_accounts);
    await loadStatus();
  });

  document.getElementById("input-ui-language")?.addEventListener("change", (e) => {
    applyLanguage((e.target as HTMLSelectElement).value);
  });

  document.getElementById("btn-save-personality")?.addEventListener("click", async () => {
    const user_name = (document.getElementById("input-user-name") as HTMLInputElement).value.trim();
    const honorific = (document.getElementById("input-honorific") as HTMLSelectElement).value;
    const calendar_accounts = (document.getElementById("input-calendar-accounts") as HTMLTextAreaElement).value.trim();
    await savePersonalization(user_name, honorific, calendar_accounts);
  });

  document.getElementById("btn-generate-personality")?.addEventListener("click", () => {
    const preset = (document.getElementById("input-personality-preset") as HTMLSelectElement).value;
    const language = (document.getElementById("input-response-language") as HTMLSelectElement).value;
    const honorific = (document.getElementById("input-honorific") as HTMLSelectElement).value || "sir";
    const generated = generatePersonalityBrief(preset, language, honorific);
    (document.getElementById("input-personality-brief") as HTMLTextAreaElement).value = generated;
  });

  // Setup next button
  document.getElementById("btn-setup-next")?.addEventListener("click", advanceSetup);

  // Skills search
  document.getElementById("skills-search")?.addEventListener("input", (e) => {
    skillSearchTerm = (e.target as HTMLInputElement).value;
    renderSkillsList();
  });

  // Category chips (delegated)
  document.getElementById("skills-chips")?.addEventListener("click", (e) => {
    const btn = (e.target as HTMLElement).closest<HTMLButtonElement>("button[data-cat]");
    if (!btn) return;
    activeCategory = btn.dataset.cat || "All";
    renderChips();
    renderSkillsList();
  });

  // Skill toggles (delegated)
  document.getElementById("skills-list")?.addEventListener("click", (e) => {
    const btn = (e.target as HTMLElement).closest<HTMLButtonElement>("button[data-slug]");
    if (!btn) return;
    toggleSkill(btn.dataset.slug || "");
  });

  // MCP connect/disconnect + token save (delegated)
  document.getElementById("mcp-list")?.addEventListener("click", (e) => {
    const tokenBtn = (e.target as HTMLElement).closest<HTMLButtonElement>("button[data-save-token]");
    if (tokenBtn) {
      saveMcpToken(tokenBtn.dataset.saveToken || "", tokenBtn.dataset.env || "");
      return;
    }
    const btn = (e.target as HTMLElement).closest<HTMLButtonElement>("button[data-mcp]");
    if (!btn) return;
    toggleMcp(btn.dataset.mcp || "", btn.dataset.connected === "true");
  });

  // --- Memory Tab ---
  document.getElementById("btn-memory-search")?.addEventListener("click", loadMemories);
  document.getElementById("btn-memory-refresh")?.addEventListener("click", () => loadMemories());
  document.getElementById("memory-search-input")?.addEventListener("keydown", (e) => {
    if ((e as KeyboardEvent).key === "Enter") loadMemories();
  });
  document.getElementById("btn-memory-add")?.addEventListener("click", addMemory);
  document.getElementById("btn-submit-goal")?.addEventListener("click", submitGoal);

  // Memory filter chips
  document.querySelectorAll(".filter-chip[data-mem-filter]").forEach((chip) => {
    chip.addEventListener("click", () => {
      document.querySelectorAll(".filter-chip[data-mem-filter]").forEach((c) => c.classList.remove("active"));
      chip.classList.add("active");
      loadMemories();
    });
  });
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

  const navBar = document.querySelector(".sidebar-nav") as HTMLElement | null;
  if (navBar) navBar.style.display = "none";

  const guide = document.getElementById("onboarding-guide");
  if (guide) guide.style.display = "none";

  const titleEl = document.getElementById("settings-title");
  if (titleEl) titleEl.textContent = currentLanguage === "pl" ? "Konfiguracja asystenta" : "Assistant Configuration";

  // Hide sections except API keys
  showSetupStep(0);
}

// Which section each setup step reveals (0=keys, 1=engine, 2=skills, 3=tools, 4=profile, 5=personality).
const SETUP_STEP_SECTION: Record<number, string> = {
  0: "section-api-keys",
  1: "section-engine",
  2: "section-skills",
  3: "section-mcp",
  4: "section-preferences",
  5: "section-personality",
};

function showSetupStep(step: number) {
  document.querySelectorAll("#onboarding-steps span").forEach((item, index) => {
    item.classList.toggle("active", index === Math.min(step, 5));
  });
  const visible = SETUP_STEP_SECTION[step];
  ALL_SECTIONS.forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.style.display = id === visible ? "" : "none";
  });

  const nextBtn = document.getElementById("btn-setup-next");
  if (nextBtn) {
    const labels: Record<number, string> = {
      0: t("nextEngine"),
      1: t("nextSkills"),
      2: t("nextTools"),
      3: t("nextProfile"),
      4: t("nextPersonality"),
      5: t("finish"),
    };
    nextBtn.textContent = labels[step] || t("finish");
  }
}

async function advanceSetup() {
  setupStep++;
  if (setupStep >= 6) {
    // Done — reveal everything and close
    isFirstTimeSetup = false;
    const welcome = document.getElementById("settings-welcome");
    if (welcome) welcome.style.display = "none";
    const nav = document.getElementById("setup-nav");
    if (nav) nav.style.display = "none";

    const navBar = document.querySelector(".sidebar-nav") as HTMLElement | null;
    if (navBar) navBar.style.display = "flex";

    ALL_SECTIONS.forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.style.display = "";
    });

    showTab("onboarding");
    closeSettings();
    return;
  }
  showSetupStep(setupStep);
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

let voiceRecorder: MediaRecorder | null = null;
let voiceChunks: BlobPart[] = [];
let latestVoiceBlob: Blob | null = null;
let voiceRecordStartedAt = 0;

function setVoiceStatus(message: string) {
  const status = document.getElementById("voice-record-status");
  if (status) status.textContent = message;
}

async function loadVoiceSamples() {
  const list = document.getElementById("voice-sample-list");
  if (!list) return;
  try {
    const data = await apiGet<VoiceSamplesResponse>("/api/voice-samples");
    if (!data.samples.length) {
      list.innerHTML = `<div class="mini-row"><strong>No saved samples yet</strong><small>Record a clean phrase so future voice cloning has proper source material.</small></div>`;
      return;
    }
    list.innerHTML = data.samples.slice(0, 6).map((sample) => `
      <div class="mini-row">
        <strong>${sample.name}</strong>
        <small>${Math.round(sample.duration_seconds)}s · ${(sample.size_bytes / 1024).toFixed(1)} KB · ${new Date(sample.created_at * 1000).toLocaleString()}</small>
        <div class="mini-actions"><a href="${sample.download_url}" target="_blank" rel="noreferrer">Open</a></div>
      </div>`).join("");
  } catch {
    list.innerHTML = `<div class="mini-row"><strong>Voice samples unavailable</strong><small>The server is not ready yet.</small></div>`;
  }
}

function blobToBase64(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onloadend = () => resolve(String(reader.result || "").split(",")[1] || "");
    reader.onerror = reject;
    reader.readAsDataURL(blob);
  });
}

function wireVoiceCaptureLab() {
  const languageSelect = document.getElementById("select-speech-language") as HTMLSelectElement | null;
  if (languageSelect) {
    languageSelect.value = localStorage.getItem("jarvis.speechLanguage") || "en-US";
    languageSelect.addEventListener("change", async () => {
      localStorage.setItem("jarvis.speechLanguage", languageSelect.value);
      window.dispatchEvent(new CustomEvent("jarvis:speech-language", { detail: languageSelect.value }));
      await apiPost("/api/settings/keys", { key_name: "JARVIS_SPEECH_LANGUAGE", key_value: languageSelect.value });
      setVoiceStatus(`Speech capture language set to ${languageSelect.value}. Excellent diction advised.`);
    });
  }

  const recordBtn = document.getElementById("btn-voice-record") as HTMLButtonElement | null;
  const stopBtn = document.getElementById("btn-voice-stop") as HTMLButtonElement | null;
  const saveBtn = document.getElementById("btn-voice-save") as HTMLButtonElement | null;
  const player = document.getElementById("voice-sample-player") as HTMLAudioElement | null;

  recordBtn?.addEventListener("click", async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
      const mimeType = MediaRecorder.isTypeSupported("audio/webm;codecs=opus") ? "audio/webm;codecs=opus" : "audio/webm";
      voiceChunks = [];
      latestVoiceBlob = null;
      voiceRecordStartedAt = Date.now();
      voiceRecorder = new MediaRecorder(stream, { mimeType });
      voiceRecorder.ondataavailable = (event) => { if (event.data.size > 0) voiceChunks.push(event.data); };
      voiceRecorder.onstop = () => {
        stream.getTracks().forEach((track) => track.stop());
        latestVoiceBlob = new Blob(voiceChunks, { type: mimeType });
        const duration = Math.max(1, Math.round((Date.now() - voiceRecordStartedAt) / 1000));
        if (player) {
          player.src = URL.createObjectURL(latestVoiceBlob);
          player.style.display = "block";
        }
        if (saveBtn) saveBtn.disabled = false;
        setVoiceStatus(`Captured ${duration}s. Playback ready; save when it sounds suitably brilliant.`);
      };
      voiceRecorder.start();
      recordBtn.disabled = true;
      if (stopBtn) stopBtn.disabled = false;
      if (saveBtn) saveBtn.disabled = true;
      setVoiceStatus("Recording… speak naturally for 10–30 seconds. Dramatic pauses are optional.");
    } catch {
      setVoiceStatus("Microphone access failed. Permission appears to be playing hard to get.");
    }
  });

  stopBtn?.addEventListener("click", () => {
    if (voiceRecorder && voiceRecorder.state !== "inactive") voiceRecorder.stop();
    if (recordBtn) recordBtn.disabled = false;
    if (stopBtn) stopBtn.disabled = true;
  });

  saveBtn?.addEventListener("click", async () => {
    if (!latestVoiceBlob) return;
    const duration = Math.max(1, Math.round((Date.now() - voiceRecordStartedAt) / 1000));
    const dataBase64 = await blobToBase64(latestVoiceBlob);
    const result = await apiPost<{ success?: boolean; error?: string }>("/api/voice-samples", {
      filename: `jarvis-voice-sample-${new Date().toISOString().replace(/[:.]/g, "-")}.webm`,
      mime_type: latestVoiceBlob.type || "audio/webm",
      duration_seconds: duration,
      data_base64: dataBase64,
    });
    if (result.success) {
      setVoiceStatus("Voice sample saved. The archive has accepted your dulcet tones.");
      saveBtn.disabled = true;
      await loadVoiceSamples();
    } else {
      setVoiceStatus(result.error || "Could not save voice sample.");
    }
  });

  loadVoiceSamples();
}

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
  } else {
    // Normal mode: show default tab (onboarding/keys) and make sure sidebar-nav is visible
    const navBar = document.querySelector(".sidebar-nav") as HTMLElement | null;
    if (navBar) navBar.style.display = "flex";
    
    // Hide onboarding welcome screen if not onboarding
    const welcome = document.getElementById("settings-welcome");
    if (welcome) welcome.style.display = "none";
    
    const nav = document.getElementById("setup-nav");
    if (nav) nav.style.display = "none";

    showTab("onboarding");
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

async function loadMemories() {
  const searchInput = document.getElementById("memory-search-input") as HTMLInputElement;
  const query = searchInput?.value?.trim() || "";
  const activeFilter = document.querySelector(".filter-chip[data-mem-filter].active") as HTMLElement;
  const typeFilter = activeFilter?.dataset.memFilter || "all";
  const listEl = document.getElementById("memory-list");
  if (!listEl) return;

  try {
    let data: any;
    if (query) {
      data = await apiGet<any>(`/api/memories/search?q=${encodeURIComponent(query)}`);
      const memories = data.memories || [];
      renderMemoryList(listEl, memories);
    } else {
      const typeParam = typeFilter !== "all" ? `&type=${typeFilter}` : "";
      data = await apiGet<any>(`/api/memories?limit=50${typeParam}`);
      renderMemoryList(listEl, data.memories || []);
      updateMemoryStats(data.stats);
    }
  } catch {
    listEl.innerHTML = `<div class="mini-row"><strong>Błąd ładowania</strong></div>`;
  }
}

function renderMemoryList(container: HTMLElement, memories: any[]) {
  if (memories.length === 0) {
    container.innerHTML = `<div class="mini-row"><strong>Brak wspomnień</strong><small>JARVIS nauczy się z rozmów lub dodaj ręcznie.</small></div>`;
    return;
  }
  container.innerHTML = memories.map((m: any) => {
    const age = timeAgo(m.created_at);
    const typeBadge = `<span class="mem-type-badge mem-type-${m.type}">${m.type}</span>`;
    const impDots = "●".repeat(Math.min(5, Math.ceil((m.importance || 5) / 2))) + "○".repeat(5 - Math.min(5, Math.ceil((m.importance || 5) / 2)));
    return `<div class="memory-card">
      <div class="memory-card-header">
        ${typeBadge}
        <span class="memory-importance" title="Ważność: ${m.importance}/10">${impDots}</span>
        <button class="memory-delete-btn" data-delete-mem="${m.id}" title="Usuń">✕</button>
      </div>
      <p class="memory-content">${escapeHtml(m.content)}</p>
      <small class="memory-meta">${age}${m.source ? ` · ${escapeHtml(m.source)}` : ""} · użyte ${m.access_count || 0}x</small>
    </div>`;
  }).join("");

  // Wire delete buttons
  container.querySelectorAll("button[data-delete-mem]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = (btn as HTMLElement).dataset.deleteMem;
      if (!id) return;
      try {
        await fetch(`/api/memories/${id}`, { method: "DELETE" });
        loadMemories();
      } catch (err) {
        console.error("Failed to delete memory:", err);
      }
    });
  });
}

function updateMemoryStats(stats: any) {
  if (!stats) return;
  const set = (id: string, val: string | number) => {
    const el = document.getElementById(id);
    if (el) el.textContent = String(val);
  };
  set("mem-stat-total", stats.total_memories || 0);
  set("mem-stat-facts", stats.by_type?.fact || 0);
  set("mem-stat-prefs", stats.by_type?.preference || 0);
  set("mem-stat-recent", stats.last_24h || 0);
}

function timeAgo(ts: number): string {
  const seconds = Math.floor(Date.now() / 1000 - ts);
  if (seconds < 60) return "teraz";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m temu`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h temu`;
  return `${Math.floor(seconds / 86400)}d temu`;
}

async function addMemory() {
  const contentEl = document.getElementById("memory-add-content") as HTMLInputElement;
  const typeEl = document.getElementById("memory-add-type") as HTMLSelectElement;
  const impEl = document.getElementById("memory-add-importance") as HTMLSelectElement;
  const content = contentEl?.value?.trim();
  if (!content) return;
  await apiPost("/api/memories", {
    content,
    type: typeEl?.value || "fact",
    importance: parseInt(impEl?.value || "5"),
    source: "manual",
  });
  contentEl.value = "";
  loadMemories();
}

async function submitGoal() {
  const goalInput = document.getElementById("goal-input") as HTMLInputElement;
  const priorityEl = document.getElementById("goal-priority") as HTMLSelectElement;
  const goal = goalInput?.value?.trim();
  if (!goal) return;
  await apiPost("/api/agent/goal", { goal, priority: priorityEl?.value || "medium" });
  goalInput.value = "";
  loadAgentStatus();
}

async function loadAgentStatus() {
  try {
    const data = await apiGet<any>("/api/agent/status");
    const stateLabel = document.getElementById("agent-state-label");
    const actionCount = document.getElementById("agent-action-count");
    const uptimeEl = document.getElementById("agent-uptime");
    const pill = document.getElementById("agent-state-pill");
    
    const stateLabels: Record<string, string> = {
      idle: "Gotowy", planning: "Planowanie", executing: "Wykonywanie", verifying: "Weryfikacja",
    };
    if (stateLabel) stateLabel.textContent = stateLabels[data.status] || data.status;
    if (pill) pill.className = `agent-state-pill agent-state-${data.status}`;
    if (actionCount) actionCount.textContent = String(data.action_count || 0);
    if (uptimeEl) {
      const mins = Math.floor((data.uptime || 0) / 60);
      uptimeEl.textContent = mins > 60 ? `${Math.floor(mins / 60)}h ${mins % 60}m` : `${mins}m`;
    }
    
    // Render action history
    const histList = document.getElementById("agent-history-list");
    const actions = data.recent_actions || [];
    if (histList) {
      if (actions.length === 0) {
        histList.innerHTML = `<div class="mini-row"><strong>Brak akcji</strong><small>Akcje agenta pojawią się tutaj.</small></div>`;
      } else {
        histList.innerHTML = actions.slice(0, 15).map((a: any) => {
          const statusIcon = a.status === "completed" ? "✅" : a.status === "failed" ? "❌" : a.status === "pending_confirmation" ? "⏳" : "🔵";
          const t = new Date((a.created_at || 0) * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
          return `<div class="agent-history-item">
            <span class="agent-history-icon">${statusIcon}</span>
            <div class="agent-history-detail">
              <strong>${escapeHtml(a.title || a.action_type || "Action")}</strong>
              <small>${t} · ${a.risk || "low"} risk</small>
            </div>
          </div>`;
        }).join("");
      }
    }
  } catch {
    // ignore
  }
}

async function loadConversationStats() {
  try {
    const data = await apiGet<any>("/api/conversation/summary");
    const set = (id: string, val: string) => {
      const el = document.getElementById(id);
      if (el) el.textContent = val;
    };
    const tokens = (data.session_tokens?.input_tokens || 0) + (data.session_tokens?.output_tokens || 0);
    set("conv-tokens", tokens >= 1000 ? `${(tokens / 1000).toFixed(1)}k` : String(tokens));
    set("conv-cost", `$${(data.today_cost || 0).toFixed(2)}`);
    const mins = Math.floor((data.uptime_seconds || 0) / 60);
    set("conv-uptime", mins > 60 ? `${Math.floor(mins / 60)}h ${mins % 60}m` : `${mins}m`);
  } catch {}
}

async function loadMemoryTasks() {
  try {
    const data = await apiGet<any>("/api/tasks-memory");
    const listEl = document.getElementById("memory-task-list");
    if (!listEl) return;
    const tasks = data.tasks || [];
    if (tasks.length === 0) {
      listEl.innerHTML = `<div class="mini-row"><strong>Brak otwartych zadań</strong><small>Zadania pojawią się tutaj po dodaniu.</small></div>`;
      return;
    }
    listEl.innerHTML = tasks.slice(0, 10).map((t: any) => {
      const prioIcon = t.priority === "high" ? "🔴" : t.priority === "medium" ? "🟡" : "🟢";
      return `<div class="task-mini-card">
        <span class="task-prio">${prioIcon}</span>
        <div class="task-detail">
          <strong>${escapeHtml(t.title)}</strong>
          ${t.due_date ? `<small>Do: ${t.due_date}</small>` : ""}
        </div>
        <button class="settings-btn task-complete-btn" data-complete-task="${t.id}">✓</button>
      </div>`;
    }).join("");
    listEl.querySelectorAll("button[data-complete-task]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const id = (btn as HTMLElement).dataset.completeTask;
        if (id) {
          await apiPost(`/api/tasks-memory/${id}/complete`, {});
          loadMemoryTasks();
        }
      });
    });
  } catch {
    // ignore
  }
}

