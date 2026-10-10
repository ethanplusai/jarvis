#!/usr/bin/env python3
"""JARVIS's tools, exposed to the brain as a stdio MCP server.

Deliberately thin: it holds no state and knows nothing about sessions. Every
call is forwarded to the running JARVIS server over loopback HTTP, which keeps
the state, enforces the origin gate, and caps the result. Hand-rolled JSON-RPC
over stdin/stdout — no new dependency, as the project requires.

The brain's environment is scrubbed of CLAUDE_CODE_* and ANTHROPIC_*, so the
endpoint URL and token path arrive through the `env` block of mcp.json.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL_VERSION = "2024-11-05"

# How long this child waits for the server's answer to one tool call.
#
# EVERY server-side tool handler must finish well inside this. uvicorn does
# NOT cancel a handler when its client gives up: if we time out here, the
# handler carries on and completes anyway, while the brain is told the server
# is unreachable — so JARVIS announces a failure and the action happens
# regardless. That exact lie was shipped once, by steer_session doing its
# read-back (a real TTS playback plus a cancel window, tens of seconds) inside
# the call.
#
# The rule that came out of it: long-running, user-facing work does not belong
# in a tool handler. Validate synchronously, stage the work, return at once,
# and let the server perform it AFTER the turn ends (see server.py's
# _perform_staged_steers). 20s is ample for handlers that obey that rule, and
# raising it would only buy patience for handlers that do not.
TIMEOUT_SEC = 20.0

TOOL_SPECS = [
    {
        "name": "list_sessions",
        "description": (
            "Every Claude Code session running on this machine, grouped by project. "
            "This is the ONLY correct way to answer what is running, which sessions "
            "exist, what needs the user, or what a session is doing. Never use a "
            "screenshot for that."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "filter": {
                    "type": "string",
                    "description": ("Optional state filter: needs_you, working, idle, "
                                    "shell, gone, or fresh."),
                },
            },
        },
    },
    {
        "name": "session_detail",
        "description": (
            "What one session is working on and where it left off: its topic, the "
            "user's last message to it, what it last said, and its recent tools. "
            "Use it before answering 'where did X leave off'."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "The session as the user refers to it."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "steer_session",
        "description": (
            "Send a message into a running Claude Code session, as if the user had "
            "typed it. Rewrite what the user said into a clear instruction addressed "
            "to that session: include the decision they made, and never add anything "
            "they did not say. Returns as soon as the message is staged: JARVIS "
            "reads it back aloud and sends it when your turn ends, unless the user "
            "stops him. Say briefly that it is going out, then end your turn — do "
            "not call this twice for the same message. Cannot answer a permission "
            "prompt or a dialog — use answer_dialog for those."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "The session as the user refers to it."},
                "prompt": {"type": "string",
                           "description": "The message to send, already rewritten."},
            },
            "required": ["name", "prompt"],
        },
    },
    {
        "name": "answer_dialog",
        "description": (
            "Press ONE key in the terminal a session is running in, to clear a "
            "permission prompt or a dialog that has it stuck — the thing "
            "steer_session cannot do. The key must be one of: return (or yes), "
            "escape (or no), or a single digit 1-9 for a numbered option. Nothing "
            "else can be pressed and free text is refused, so ask which one the "
            "user means rather than paraphrasing. Only works when that session is "
            "running in Terminal.app; sessions hosted by another application "
            "cannot be reached and the user is told so. This BRINGS THAT WINDOW "
            "TO THE FRONT, so only use it when the user has just asked for it. "
            "Returns as soon as the keypress is staged: JARVIS says what he is "
            "about to press and presses it when your turn ends, unless the user "
            "stops him. Say briefly that it is going out, then end your turn — "
            "do not call this twice for the same keypress."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "The session as the user refers to it."},
                "key": {"type": "string",
                        "description": ("One of: return, yes, enter, escape, no, "
                                        "cancel, or a single digit 1-9.")},
            },
            "required": ["name", "key"],
        },
    },
    {
        "name": "spawn_run",
        "description": (
            "Start NEW Claude Code work in a project, as a recorded run the user "
            "can watch on the dashboard. For a SMALL, self-contained task — a fix, "
            "a tweak, a question about the code that needs changing. A real "
            "project, something being built from scratch or in phases, is "
            "start_build instead, after you have brainstormed it with the user. "
            "To redirect a session that already exists, use steer_session. "
            "Name the project as the user named it — if that is ambiguous, or the "
            "project is unknown, this comes back with a question to ask rather than "
            "starting anything. Write the prompt as a complete instruction: the run "
            "is unattended and cannot ask a follow-up. ASK THE USER WHICH MODEL "
            "before you start anything, unless he has already said — he asked for "
            "that explicitly. Returns as soon as the run has started; the user is "
            "told when it finishes."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string",
                            "description": "The project as the user refers to it."},
                "prompt": {"type": "string",
                           "description": ("The complete instruction for the run, "
                                           "already written out.")},
                "model": {"type": "string",
                          "description": ("The model for this run — pass it "
                                          "WHENEVER the user names one ('opus', "
                                          "'opus 5', 'sonnet'); say it back as "
                                          "you heard it and it is normalised "
                                          "for you. Omit only if he named "
                                          "none.")},
                "resume": {"type": "boolean",
                           "description": ("True when this continues work a "
                                           "previous run did in that project — "
                                           "'make it better', 'now add a "
                                           "contact form'. It picks up the last "
                                           "finished run there instead of "
                                           "starting from nothing. False, or "
                                           "omitted, for something new.")},
            },
            "required": ["project", "prompt"],
        },
    },
    {
        "name": "start_build",
        "description": (
            "Build a REAL project: a long session that writes its own plan, "
            "reviews and revises it, then executes it phase by phase. Use this — "
            "not spawn_run — whenever the user wants something BUILT that is more "
            "than a single small task. "
            "FIRST brainstorm it with him out loud, one question at a time: what "
            "it is for, what it must do, what he feels strongly about, what is "
            "explicitly out of scope, and which of two or three approaches he "
            "wants. THEN confirm, and only then call this. "
            "ASK WHICH MODEL BEFORE YOU START — he asked for that in as many "
            "words. Opus for a real build, Sonnet for something lighter. If you "
            "call this without a model it comes back with the question rather "
            "than choosing for him. "
            "`spec` is everything the two of you settled, written out properly — "
            "it is saved into the project as a design document and is the only "
            "thing that survives if that session is replaced or compacted, so put "
            "the decisions in it, not a summary of them. The session does the "
            "planning, the review and the work itself; do not spawn a second run "
            "for the same build, and use build_status to say how it is going."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string",
                            "description": "The project as the user refers to it."},
                "spec": {
                    "type": "string",
                    "description": (
                        "The agreed design, in full: what is being built, what it "
                        "is for, how it should work, and every decision the user "
                        "made. Markdown, first line a short title. Written to "
                        "docs/superpowers/specs/ in the project."),
                },
                "constraints": {
                    "type": "string",
                    "description": ("Anything the build must respect — a stack, a "
                                    "look, a thing not to touch."),
                },
                "non_goals": {
                    "type": "string",
                    "description": ("What is explicitly out of scope, so the "
                                    "session does not build it."),
                },
                "model": {
                    "type": "string",
                    "description": ("The model this build runs on. ASK THE USER "
                                    "FIRST; pass what he says ('opus', 'opus 5', "
                                    "'sonnet'). Omitting it returns the question "
                                    "to ask, and starts nothing."),
                },
            },
            "required": ["project", "spec"],
        },
    },
    {
        "name": "build_status",
        "description": (
            "How far a build has actually got, read off its plan file: how many "
            "tasks there are, how many are done, and which one it is on now — "
            "together with whether anything is still running. Use it for 'how's "
            "the build going' rather than guessing from how long it has been. It "
            "will tell you when it is still planning and has not written a plan "
            "yet, and when the plan has work left but nothing is running."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string",
                            "description": "The project as the user refers to it."},
            },
            "required": ["project"],
        },
    },
    {
        "name": "review_document",
        "description": (
            "Read a project's spec or plan back BY SECTION NUMBER — the same "
            "numbers the user can see on the dashboard's SPECS tab, so 'read me "
            "three' and 'change five' mean the same section to both of you. "
            "Called with no section it gives the numbered outline, how many "
            "tasks are done, and whether the document is approved; called with "
            "one it reads that section in full. Use it whenever the user is "
            "talking about a design or a plan you wrote — never count the "
            "headings yourself, and never renumber them out loud."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string",
                            "description": "The project as the user refers to it."},
                "section": {"type": "integer",
                            "description": "The section number the user said. "
                                           "Omit for the outline."},
                "path": {"type": "string",
                         "description": "The document's project-relative path. "
                                        "Omit for the most recently written one, "
                                        "which is nearly always the right one."},
            },
            "required": ["project"],
        },
    },
    {
        "name": "approve_document",
        "description": (
            "Write down that the user approved a spec or a plan. Call it when "
            "he says so out loud — 'that's approved', 'go ahead with that' — "
            "and not before: this is the human approval the whole build "
            "process hangs off, and it is recorded against the exact words "
            "that were on the page, so a later revision shows as needing his "
            "eye again."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string",
                            "description": "The project as the user refers to it."},
                "path": {"type": "string",
                         "description": "The document's project-relative path. "
                                        "Omit for the most recently written one."},
            },
            "required": ["project"],
        },
    },
    {
        "name": "run_command",
        "description": (
            "Run ONE command in a Terminal window sitting in a project's "
            "directory — how you actually start what was built, so the user can "
            "see it. Prefer the command the project itself documents: its "
            "README's start command, or a script in its package.json. JARVIS "
            "reads the command back aloud and gives the user a moment to stop "
            "him before it runs, and tells him if it is not a command the project "
            "documents. One plain command only: anything chained, piped or "
            "redirected is refused, as is anything that is not a way of starting "
            "a project. Returns as soon as it is staged — say briefly that it is "
            "about to run, then end your turn."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string",
                            "description": "The project as the user refers to it."},
                "command": {"type": "string",
                            "description": ("The single command to run, e.g. "
                                            "'npm run dev'.")},
            },
            "required": ["project", "command"],
        },
    },
    {
        "name": "open_in_browser",
        "description": (
            "Show the user something in their browser: a web address, or a file "
            "inside one of their projects — the page a run just built, for "
            "instance. Give the file as the user would name it ('index.html'); "
            "name the project too if it is not the one you last started work in. "
            "A file that is not there is refused rather than opened, and nothing "
            "outside a project JARVIS knows can be opened at all. This puts the "
            "page in front of the USER; to read or see it yourself use "
            "read_page or look_at_page."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string",
                           "description": ("A web address, or a file inside a "
                                           "project.")},
                "project": {"type": "string",
                            "description": ("Optional: which project the file is "
                                            "in, as the user refers to it.")},
                "browser": {"type": "string",
                            "description": ("Optional: 'chrome' or 'firefox', "
                                            "when the user names one for this "
                                            "one page. Otherwise his default "
                                            "is used.")},
            },
            "required": ["target"],
        },
    },
    {
        "name": "read_page",
        "description": (
            "Read a web page yourself: its text comes straight back, in about a "
            "second. THE way to answer what a page says, what is on it, or "
            "what an error on it means. Use it whenever the user asks you about "
            "a site — never guess at a page's contents, and never spawn a run "
            "to go and look. Long pages come back topped and tailed, and you "
            "are told how much there was. Web addresses only."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string",
                        "description": "The http or https address of the page."},
            },
            "required": ["url"],
        },
    },
    {
        "name": "look_at_page",
        "description": (
            "SEE a page: a screenshot comes back as an actual image you can "
            "look at. For how something LOOKS — a layout, a colour, where a "
            "button sits, whether the thing the user just started is rendering "
            "at all ('does this look right'). For the user's OWN screen it is "
            "look_at_screen you want, not this. Slower "
            "and dearer than read_page, so use read_page for what a page SAYS "
            "and this for what it LOOKS LIKE. Web addresses only, including a "
            "local one like http://localhost:3000."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string",
                        "description": "The http or https address of the page."},
            },
            "required": ["url"],
        },
    },
    {
        "name": "what_is_on_screen",
        "description": (
            "What the user has open: which app is in front and what every "
            "window is called. Cheap and instant, and usually enough for "
            "'what am I looking at' or 'what am I working on'. Try this "
            "BEFORE look_at_screen, which costs a great deal more."),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "look_at_screen",
        "description": (
            "SEE the user's screen: a picture of one display comes back "
            "as an actual image. For 'can you see my screen', 'does this look "
            "right', 'what's this error' — anything about how something LOOKS "
            "that a window title cannot answer. Take it only when he has just "
            "asked; it is his private desk, and it costs about a thousand "
            "words of your context. One display per call: pass `display` when "
            "he says which — 'my other screen', 'the second monitor' — and if "
            "what you see is not what he described, say so and try the other."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "display": {
                    "type": "integer",
                    "description": ("Which display, 1-based. Omit for the main "
                                    "one. 2 is the other screen on a two-screen "
                                    "desk."),
                },
            },
        },
    },
    {
        "name": "github_repo",
        "description": (
            "A repository on GitHub: what it is, its licence, its description "
            "and the top of its readme, in half a second. THE way to answer "
            "anything about a repo — never search the web for one. Takes the "
            "name as the user says it ('the arcreactor repo', 'my Arc Loop "
            "repo') or owner/name. When several match you are told which, and "
            "you ask him rather than picking."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "The repository, as the user named it."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "usage_status",
        "description": (
            "How much of the user's Claude subscription is used: the five-hour "
            "and seven-day windows, and when each resets. THE way to answer "
            "'what's my usage', 'what's my session limit', 'how much have I got "
            "left'. Report exactly what comes back — when it says there is no "
            "reading, say so; never offer a number of your own and never say "
            "zero."),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "connections",
        "description": (
            "The services the user has connected you to, from what is actually "
            "running: which are up, what each can do, which would not start, and "
            "what is wrong with any entry. THE way to answer 'what are you "
            "connected to' and 'what can you do with my X' — never answer either "
            "from memory. Call it too whenever a service the user names has no "
            "tool: it says why, and where to fix it."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "service": {"type": "string",
                            "description": "One service, as the user named it."},
            },
        },
    },
    {
        "name": "open_in_terminal",
        "description": (
            "Open a Terminal window sitting in a project's directory, so the user "
            "can carry on there himself."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string",
                            "description": "The project as the user refers to it."},
            },
            "required": ["project"],
        },
    },
    {
        "name": "enable_session_inbox",
        "description": (
            "Set the user's Claude Code sessions to accept messages from you "
            "without asking him to approve each one. Use it ONLY when he has just "
            "said yes to that — offer it in one line first and never write it off "
            "your own back. It changes one setting and leaves the rest of his "
            "configuration exactly as it is."),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_project",
        "description": (
            "Create a NEW, empty project — a directory with a git repository "
            "and a README — when the user wants to build something that does "
            "not exist yet. Use it only when no project of that name is "
            "already there; spawn_run can start work in it the moment this "
            "returns. It never touches or reuses an existing directory: if "
            "the name is taken you are told, and nothing is changed. Give the "
            "name as the user said it."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "What the project should be called."},
                "description": {
                    "type": "string",
                    "description": ("Optional one line on what it is for, for "
                                    "the README."),
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "run_status",
        "description": (
            "How work you started is going. With no argument, everything "
            "active. Otherwise name the project, or refer to it the way the "
            "user did — 'that one', 'the one you just started' — and it is "
            "resolved for you; ask which one rather than guessing if it comes "
            "back with a question. This is the ONLY correct way to answer "
            "whether a run has finished or how it ended."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run": {"type": "string",
                        "description": ("Optional: the project, or how the "
                                        "user referred to the run.")},
            },
        },
    },
    {
        "name": "cancel_run",
        "description": (
            "Stop work that is still going. Name the project or refer to the "
            "run the way the user did. If more than one is running there you "
            "get a question back — ask it, never pick. Report exactly what "
            "came back, including when there was nothing left to stop."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run": {"type": "string",
                        "description": ("The project, or how the user "
                                        "referred to the run.")},
            },
            "required": ["run"],
        },
    },
    {
        "name": "list_projects",
        "description": "The projects that have Claude Code sessions, with their paths.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "repo_overview",
        "description": (
            "What a project IS: the README's opening, its top-level structure, the "
            "languages it is written in, and its git branch. THE way to answer "
            "'what does X do', 'what is X', or 'tell me about X' when X is a "
            "project. It reads the filesystem directly and returns in "
            "milliseconds — never spawn a run to answer a question about code "
            "that already exists, and never guess from the project's name. "
            "YOUR OWN SOURCE is a project here too: pass 'jarvis' to this, to "
            "search_repo or to read_file and you are reading the code you are "
            "running on. That is how you answer how you are built, what you "
            "can do, or why you did something — read it, do not guess."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string",
                            "description": "The project as the user refers to it."},
            },
            "required": ["project"],
        },
    },
    {
        "name": "search_repo",
        "description": (
            "Find WHERE something lives in a project — a function, a setting, a "
            "phrase, an error message. Returns matching lines as "
            "'file:line: text'. Case-insensitive and literal, not a regular "
            "expression, so search for the plain word. Use it for 'where is the "
            "auth logic', 'does X use Y', 'which file handles Z'; then use "
            "read_file on a hit to see the surrounding code. Instant — do NOT "
            "spawn a run to go looking through existing code."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string",
                            "description": "The project as the user refers to it."},
                "query": {"type": "string",
                          "description": "The literal text to look for."},
            },
            "required": ["project", "query"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read a bounded window of one file — the follow-up to a search_repo "
            "hit. Pass 'around' as the line number the hit was on, or as a "
            "phrase to centre on, and you get the lines either side of it. Never "
            "returns a whole large file: the answer says which lines came back "
            "out of how many, and says when there is more. Ask again with a "
            "different 'around' to see another part. Paths are relative to the "
            "project, and anything outside it — or any credential, key or .env "
            "inside it — is refused."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string",
                            "description": "The project as the user refers to it."},
                "path": {"type": "string",
                         "description": ("The file, relative to the project root, "
                                         "e.g. src/auth.ts.")},
                "around": {"type": "string",
                           "description": ("Optional: a line number, or a phrase "
                                           "to centre the window on.")},
            },
            "required": ["project", "path"],
        },
    },
    {
        "name": "open_in_editor",
        "description": (
            "Open a file, or the whole project, in the user's editor — VS Code "
            "where it is installed. Use it when the user asks to SEE or open "
            "code, not to read it to them; read_file is for reading. Omitting "
            "'path' opens the project itself, which is what 'open chitauri' "
            "means. This puts a window on their screen, so only when they have "
            "just asked for it."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string",
                            "description": "The project as the user refers to it."},
                "path": {"type": "string",
                         "description": ("Optional: the file, relative to the "
                                         "project root. Omit to open the project.")},
            },
            "required": ["project"],
        },
    },
    {
        "name": "remember",
        "description": (
            "Store one durable fact, preference, or decision — indexed so it surfaces "
            "in EVERY future conversation, whether or not that one touches the same "
            "project. Use it when the user tells you something you would want to know "
            "next week. Do NOT use it for what is already visible in a session or a "
            "file, and do NOT use it for a running log of work on one project — that "
            "is project_note, which is not indexed and only surfaces when that project "
            "comes up."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "The fact, in one line."},
                "body": {"type": "string", "description": "The detail and why it matters."},
                "hook": {"type": "string", "description": "A few words for the index."},
            },
            "required": ["title"],
        },
    },
    {
        "name": "recall",
        "description": (
            "Search everything you have remembered — facts, project notes, and past "
            "journals. Use it before saying you do not know something about the user "
            "or a project."),
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "project_note",
        "description": (
            "Append what you have learned about one project. Use it after doing real "
            "work on a project, so the next conversation starts informed."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["project", "text"],
        },
    },
    {
        "name": "write_journal",
        "description": (
            "Write a handover note for your next conversation: what you worked on, "
            "what the user decided, what is unfinished. You will be asked to do this "
            "before your context is rotated."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["text"],
        },
    },
]

# Tools this machine cannot perform on Windows, left out of what the brain is
# offered (brain.granted_tools must agree). Empty since answer_dialog was
# ported: there it attaches to the session's own console (dialog_windows.py).
# TOOL_SPECS stays the whole set: it is what every handler is checked against.
UNAVAILABLE_ON_WINDOWS: set[str] = set()

# answer_dialog's description, as true on Windows: the key goes into the
# session's console rather than to Terminal.app, and no window comes forward.
_ANSWER_DIALOG_WHERE_MAC = (
    "Only works when that session is running in Terminal.app; sessions hosted "
    "by another application cannot be reached and the user is told so. This "
    "BRINGS THAT WINDOW TO THE FRONT, so only use it when the user has just "
    "asked for it.")
_ANSWER_DIALOG_WHERE_WINDOWS = (
    "Only works when that session is running in a console window of its own; "
    "sessions hosted by another application (the desktop app, an editor) "
    "cannot be reached and the user is told so. Nothing comes to the front, "
    "but only use it when the user has just asked for it.")


def _for_windows(spec: dict) -> dict:
    if spec["name"] != "answer_dialog":
        return spec
    return {**spec, "description": spec["description"].replace(
        _ANSWER_DIALOG_WHERE_MAC, _ANSWER_DIALOG_WHERE_WINDOWS)}


def offered_tool_specs() -> list[dict]:
    """The tools this machine can actually perform, described as it does them."""
    if sys.platform == "win32":
        return [_for_windows(t) for t in TOOL_SPECS
                if t["name"] not in UNAVAILABLE_ON_WINDOWS]
    return TOOL_SPECS


_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _unverified_loopback_context() -> ssl.SSLContext:
    """An SSLContext that skips certificate verification.

    Safe ONLY here, and nowhere else: this connection never leaves the
    machine — it targets 127.0.0.1 / ::1 / localhost — and what authenticates
    the call is the bearer token in the request header, not the TLS
    certificate. CLAUDE.md's own quick-start has the user generate a
    self-signed cert.pem/key.pem, which no CA trusts; verifying it against
    the system trust store would fail every single call. Never reuse this
    for a non-loopback host.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _ssl_context_for(url: str) -> ssl.SSLContext | None:
    """An unverified context for loopback HTTPS, None otherwise (default
    verification for everything else, including non-loopback HTTPS)."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme == "https" and parsed.hostname in _LOOPBACK_HOSTS:
        return _unverified_loopback_context()
    return None


def _endpoint() -> str:
    return os.getenv("JARVIS_TOOL_URL", "http://127.0.0.1:8340/internal/tool")


def _token() -> str:
    path = os.getenv("JARVIS_TOOL_TOKEN_FILE", "")
    if not path:
        return ""
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return ""


# --- letting the brain SEE something -------------------------------------
#
# The brain is `claude -p` with `--tools` set to an ALLOWLIST that names only
# these MCP tools, so it has no Read tool: handing it the PATH of a PNG would
# hand it a string it can do nothing whatever with. The one route an image has
# into that process is an MCP `image` content block on a tool result, which
# the CLI turns into a real image in the model's context.
#
# Verified end to end before this was built, not assumed: a throwaway stdio
# MCP server returning `{"type": "image", ...}` for a 1280x800 PNG reading
# "PURPLE WALRUS 7421", driven by
#   claude -p --strict-mcp-config --mcp-config … --tools mcp__t__see
#   "call the see tool and tell me the exact words in the image"
# answered "The image shows the exact words: PURPLE WALRUS 7421". So the
# mechanism works, and a path would not have.
#
# The image rides in its own key, NOT in `text`: the server caps every tool
# result's text at 1,500 characters, and base64 of even a small PNG is tens
# of thousands. Only `text` goes through that cap.


def _image_block(raw) -> dict | None:
    """A well-formed MCP image content block from the server's `image` field.

    Anything malformed is dropped rather than passed on: a broken content
    block would take down the whole tool result, and the text half of the
    answer is still worth having.
    """
    if not isinstance(raw, dict):
        return None
    data, mime = raw.get("data"), raw.get("mimeType")
    if not isinstance(data, str) or not data or not isinstance(mime, str):
        return None
    return {"type": "image", "data": data, "mimeType": mime}


def _forward(tool: str, arguments: dict) -> tuple[bool, str, dict | None]:
    """Call the server. Every failure becomes a spoken-able sentence, never a
    traceback: the brain has to say something useful either way.

    Third element is an MCP image content block when the server sent one.
    """
    body = json.dumps({"tool": tool, "arguments": arguments}).encode()
    endpoint = _endpoint()
    req = urllib.request.Request(
        endpoint, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {_token()}"})
    try:
        with urllib.request.urlopen(
                req, timeout=TIMEOUT_SEC, context=_ssl_context_for(endpoint)) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        return False, f"JARVIS refused the call ({e.code}).", None
    except (urllib.error.URLError, OSError, TimeoutError):
        return False, "The JARVIS server is unreachable.", None
    except ValueError:
        return False, "The JARVIS server sent something unreadable.", None
    if not isinstance(payload, dict):
        return False, "The JARVIS server sent something unreadable.", None
    return (bool(payload.get("ok")), str(payload.get("text", "")),
            _image_block(payload.get("image")))


def _result(rid, ok: bool, text: str, image: dict | None = None) -> dict:
    content: list[dict] = [{"type": "text", "text": text}]
    if image:
        content.append(image)
    return {"jsonrpc": "2.0", "id": rid,
            "result": {"content": content, "isError": not ok}}


def _error(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def handle(msg: dict) -> dict | None:
    """One JSON-RPC message in, at most one reply out.

    Per the JSON-RPC 2.0 spec a notification — a message with no "id"
    member at all — gets NO reply, for any method, success or error alike.
    """
    method = msg.get("method")
    is_notification = "id" not in msg
    rid = msg.get("id")

    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "initialize":
        reply = {"jsonrpc": "2.0", "id": rid,
                 "result": {"protocolVersion": PROTOCOL_VERSION,
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "jarvis", "version": "1.0.0"}}}
    elif method == "ping":
        reply = {"jsonrpc": "2.0", "id": rid, "result": {}}
    elif method == "tools/list":
        reply = {"jsonrpc": "2.0", "id": rid,
                 "result": {"tools": offered_tool_specs()}}
    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name", "")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            reply = _result(rid, False, "Arguments must be an object.")
        else:
            ok, text, image = _forward(name, args)
            reply = _result(rid, ok, text, image)
    else:
        reply = _error(rid, -32601, f"Unknown method: {method}")

    return None if is_notification else reply


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue                      # a garbage line must not kill the server
        if not isinstance(msg, dict):
            continue
        try:
            reply = handle(msg)
        except Exception as e:            # never die mid-conversation
            reply = _error(msg.get("id"), -32603, f"Internal error: {e}")
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
