# JARVIS — Operations Handbook

**Your voice-first AI assistant for macOS. Talks back, takes action, builds software.**

---

## How to Start and Stop

**Start:**
```bash
cd ~/jarvis
(unset ANTHROPIC_API_KEY && source venv/bin/activate && python server.py &)
cd frontend && npm run dev &
```
Then open Firefox and go to: `http://localhost:5200`
Click anywhere to enable audio. You're live.

**Stop:**
```bash
pkill -f "python server.py" && pkill -f "vite"
```

**Interface:**
- The glowing orb is Jarvis. It pulses when he speaks.
- Three-dot menu (top right): Settings, Restart Server, Fix Yourself
- Settings panel: change your name, voice, API key, check system status
- Mute button: toggle listening on/off

---

## How to Talk to Him

Just speak naturally. Jarvis listens continuously and responds by voice.
Short commands get short answers. Open-ended questions get a conversation.

He addresses you as **"sir"** by default. Change it in `.env` → `HONORIFIC=`.

---

## Commands and Speech Patterns

### "Pull up [anything]"
Your universal Firefox command. Whatever follows goes straight to Firefox as a Google search.

```
"Pull up the weather forecast in Ireland"       → Firefox, weather search
"Pull up the mathematical symbol for pi"        → Firefox, Google search
"Pull up BBC News"                              → Firefox, Google search
"Pull up flights from Leicester to Lisbon"      → Firefox, Google search
```

Exception: bare app names with no articles open the app instead.
```
"Pull up Spotify"    → switches to Spotify app
"Pull up Slack"      → switches to Slack app
```

---

### Browser & Web
```
"Search for..."                  → Firefox, Google search
"Go to [website]"                → Firefox, opens that site
"Pull up [anything]"             → Firefox (see above)
"Go back / go forward"           → browser navigation
"Reload / refresh"               → reloads current tab
"Close this tab"                 → closes active tab
"What page is this?"             → reads you the current URL and title
```

---

### Building Software
```
"Build me a [description]"       → Jarvis asks 1-2 clarifying questions, then
                                   spawns a Claude Code session to build it.
                                   A full project lands on your Desktop.
"Jump into [project name]"       → connects to an existing project via Claude Code
"Resume where we left off on X"  → picks up from the last session on that project
"Check for improvements on X"    → reviews the project and suggests next steps
"Pull up what you built"         → opens the last completed build in Firefox
```

---

### Research
```
"Research [topic]"               → Claude Code browses the web, gathers real data,
                                   and produces a formatted HTML report on your Desktop.
                                   More thorough than a browser search — takes 2-3 minutes.
```

---

### Calendar, Mail & Notes
**Calendar and Mail only respond if you already have them open.**
They do not auto-launch. This is by design — no background surprises.

```
"What's on my schedule today?"          → reads from Apple Calendar
"Any meetings this week?"               → calendar summary
"Any unread emails?"                    → reads from Apple Mail (read-only)
"Who emailed me today?"                 → mail scan
"Create a note: [title] / [content]"   → saves to Apple Notes
"Read my note about [topic]"            → reads a note back to you
"Note that [fact]"                      → saves to Jarvis's internal memory, not Notes
```

---

### Tasks & Memory
```
"Remind me to [task] tomorrow"          → creates a task with a due date
"Add a high-priority task: [title]"     → adds to task list
"Remember that I prefer [X] over [Y]"  → Jarvis stores this and uses it in future
```

---

### App & Window Control
```
"Open [app name]"                → launches or switches to that app
"Switch to [app name]"           → brings app to foreground
"Quit [app name]"                → asks confirmation, then quits
"Hide [app name]"                → hides the app (Cmd+H)
"Minimise window"                → minimises front window
"Snap left / snap right"         → moves window to half-screen
```

---

### System & Audio
```
"Volume up / volume down"        → adjusts system volume
"Mute / unmute"                  → mutes audio
"What's the volume?"             → reads current level
"Screenshot"                     → takes a screenshot
"What's on my screen?"           → Jarvis describes what's currently visible
```

---

### Self-Awareness
```
"How are you running?"           → Jarvis checks his own code and reports status
"Fix yourself"                   → opens Claude Code in Jarvis's own project directory
"Restart server"                 → available in the three-dot menu
```

---

## What Jarvis Does Not Do

- **Read-only on Mail.** He can read your emails. He cannot send, delete, or move them.
- **No auto-launch of Calendar or Mail.** Have them open if you want him to use them.
- **No financial or sensitive data.** Keep that away from him.
- **Builds go to your Desktop.** Every project Claude Code creates lands in `~/Desktop`.

---

## Customisation — The `.env` File

Located at `~/jarvis/.env`. Edit this to change core behaviour. Restart Jarvis after saving.

| Variable | What It Does | Current Value |
|---|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic API key — Jarvis's brain | set |
| `EDGE_TTS_VOICE` | His voice. Run `edge-tts --list-voices` to browse options | `en-GB-RyanNeural` |
| `USER_NAME` | Your name — he uses it in conversation | *(empty — set this)* |
| `HONORIFIC` | How he addresses you | `sir` |
| `CALENDAR_ACCOUNTS` | Filter which calendars he reads. `auto` = all | `auto` |
| `LOCAL_LLM_URL` | LM Studio URL for local fast responses | `http://localhost:1234/v1` |
| `LOCAL_LLM_MODEL` | Local model for fast voice responses (Gemma 4 E4B) | `google/gemma-4-e4b` |
| `JARVIS_FAST_MODEL` | Claude model for quick responses | `claude-haiku-4-5-20251001` |
| `JARVIS_SMART_MODEL` | Claude model for research and complex tasks | `claude-opus-4-6` |

**Voice options worth trying:**
- `en-GB-RyanNeural` — British male (current, suits the JARVIS character)
- `en-GB-SoniaNeural` — British female
- `en-US-GuyNeural` — American male
- Run `edge-tts --list-voices` for the full list

---

## Customisation — The System Prompt

Located in `~/jarvis/server.py` around **line 85**.
This is Jarvis's personality, rules, and instructions. It's plain English — edit it directly.

**Things worth tweaking here:**
- His personality and tone (currently: dry British butler, economy of language)
- Response length rules (currently: 1-2 sentences for commands, up to 5 for discussion)
- Default behaviour for specific phrases (the "pull up" rule lives here)
- What he should do when you say specific things

Restart Jarvis after any changes to `server.py`.

---

## How the AI Brain Works

Jarvis uses two AI models in parallel, depending on the task:

**Fast model** (`claude-haiku-4-5-20251001`) — or your local Gemma 4 if LM Studio is running
- Used for: voice conversation, quick commands, task creation, browsing decisions
- Response time: under 1 second with local model, 1-2 seconds via Anthropic

**Smart model** (`claude-opus-4-6`)
- Used for: deep research, complex builds, anything that needs real thinking
- Response time: 2-5 seconds. Worth the wait.

**Local LLM (LM Studio + Gemma 4 E4B)**
If LM Studio is running with Gemma 4 loaded, fast responses are routed there instead of Anthropic. This means zero API cost and zero latency for everyday commands. If LM Studio is off, Jarvis falls back to Claude Haiku automatically.

---

## Files Worth Knowing

```
~/jarvis/
├── server.py              — the brain: all logic, actions, personality, LLM wiring
├── .env                   — your config: API keys, voice, name, models
├── actions.py             — system actions: browser, apps, volume, screenshots
├── memory.py              — conversation memory stored in SQLite
├── planner.py             — multi-step task planning for builds
├── calendar_access.py     — Apple Calendar integration (read-only)
├── mail_access.py         — Apple Mail integration (read-only)
├── notes_access.py        — Apple Notes integration
├── browser.py             — Playwright web automation for research
├── work_mode.py           — persistent Claude Code session management
├── frontend/              — the orb UI (Vite + TypeScript + Three.js)
└── data/jarvis.db         — SQLite: memory, tasks, notes, dispatch history
```

---

## Quick Troubleshooting

| Problem | Fix |
|---|---|
| Jarvis doesn't respond | Check if ports 8340 and 5200 are in use: `lsof -i :8340 -i :5200` |
| No voice / silent | Confirm `edge-tts` is installed: `cd ~/jarvis && source venv/bin/activate && edge-tts --list-voices` |
| API errors / LLM not working | Check `ANTHROPIC_API_KEY` is set in `.env` |
| Calendar/Mail not working | Make sure you have those apps open before asking |
| Local model not working | Make sure LM Studio is running with Gemma 4 E4B loaded |
| Port already in use on restart | Run: `pkill -f "python server.py" && pkill -f "vite"` then restart |

---

*JARVIS — Just A Rather Very Intelligent System.*
