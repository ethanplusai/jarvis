# What changed, against the original JARVIS

This is not the old JARVIS with features bolted
on; the thing at the centre was replaced.

This file exists so JARVIS can answer "how are you different now?" out loud.
Keep it accurate: he reads it, and he will say what it says.

---

## The one that changes everything: whose Claude is JARVIS

**Public:** an Anthropic API client. `requirements.txt` installs the
`anthropic` SDK, `.env.example` demands an `ANTHROPIC_API_KEY`, and every
reply is a metered API call — Haiku for conversation, Opus for research. Talk
to him for an evening and it appears on a bill.

**Here:** his brain is a **Claude Code process running on your own
subscription** — the same login you use in the terminal. There is no
Anthropic SDK in `requirements.txt`, no API key on the voice path, and no way
to put one there: every child is launched through `claude_env.child_env()`,
which strips every `ANTHROPIC_*` variable out of the environment first. That
is deliberate rather than tidy — the CLI silently *prefers* an inherited key
over your login, and `claude auth status` goes on reporting `loggedIn: true`
while the billing quietly moves. So the key is removed rather than trusted.
The same goes for whatever started him: launched by an agent from inside a
Claude Code session, his brain still runs on his own settings, not that
session's effort, timeouts and MCP start-up rules.

The honest cost question stopped being "how many dollars" and became "how much
of my five-hour and seven-day windows is gone", which is what the Usage tab
answers.

## JARVIS stopped being a voice and became a foreman

Public JARVIS answers questions and can fire off actions. This one runs work
and watches it:

- **A conversation is the design phase.** He brainstorms one question at a
  time, offers a couple of approaches, and starts nothing until you agree.
- **What you agreed gets written down** as a spec file inside the project
  being built, before any process is spawned. You approve it by voice, by
  section number — "approve the spec", "approve the plan" — and he finds the
  one you mean himself, so it is never the plan approved when you meant the
  spec.
- **A build is a real Claude Code session** handed a brief: write a phased
  plan, review it against the spec, then execute task by task under
  test-driven development, ticking the plan's checkboxes. "How far has it
  got" is answered by reading those boxes, not by guessing.
- **He watches every Claude Code session on the machine** — not only his own.
  Ask which are waiting on you and he checks live. He can post into one, and
  answer a permission prompt for one running in Terminal. He says where a
  waiting prompt is — its terminal, the desktop app, the editor — and a
  session a program started (Paperclip, the Agent SDK) is left to that
  program, which answers its prompts itself.
- **He interrupts when it matters.** A session blocked on a human is said out
  loud immediately; one that merely finished is batched into a sentence at the
  next pause. With no browser tab open it becomes a macOS notification.

## There is a dashboard now

The public repo's frontend is the orb and nothing else — `main.ts`, `orb.ts`.
This one adds a six-tab dashboard: **Runs, Sessions, Memory, Specs, Projects,
Usage**. Every Claude Code process he starts is a *run*: a row in SQLite with
its prompt, project, status, token usage and full event stream, watchable
live.

## Memory you can read

Public memory is rows in SQLite. Here it is a folder of plain Markdown files,
one fact per file, with an index he always sees. Open it in any text editor;
edit it with anything. The dashboard's Memory tab says when a file has no
line in that index — which is to say when he cannot see it — and adds the
line on request; a startup check says the same. Project notes are read back
with `project_history`, so "the next conversation starts informed" is now
true rather than promised. And your own standing orders for him live in
`LOCAL.md` beside his `CLAUDE.md`: read every conversation, never
overwritten, and they no longer cost you every persona upgrade that ships
after you write them.

When his context fills, he hands over to a fresh generation at a pause,
with a note of where things stood — and the approval cards on the Business
desk are named to the new generation straight from the ledger, so a post
you approved is not forgotten because the brain behind it changed. How full
he is is measured as the context really is: until 2026-09-26 a turn that
used a tool was counted several times over, and he was replaced after
nearly every one.

## The conversation is a window, not a log

He listens only while you hold Space, unless you switch the microphone to
always-open in Settings — measured, an open microphone took another
assistant's spoken report in the same room as a command. Two buttons top
right, one for each direction: the microphone, and his
voice. With his voice off nothing is synthesized and he answers in text.
The Conversation panel that shows it stamps every message to the second
(the day too, for anything older than today), lets you delete any message
in two clicks, minimises to its title bar,
drags anywhere on the page by that bar — arrow keys move it for anyone
without a mouse — and remembers both where it was left and whether it was
open. The Business desk is the same kind of window now, and stamps every
approval, the briefing and every record the same way — and finished
approvals can be cleared off it (the ledger keeps them).

## He connects to whatever you use

He ships connected to nothing on purpose. Any MCP server works — drop its
`mcpServers` block into `connections.json` and its tools are granted by name
at launch. The grant is an allowlist computed from your file, never
"everything except". A server started through `uvx`, `npx`, `uv run` or
`pipx run` gets a warning at startup (`mcp_launchers`): those resolve
packages on every launch, and twice here one installed for over twenty
seconds and his brain started without it.

## Security became a design constraint

Public added AppleScript escaping and a permissions toggle — real fixes to a
codebase that started without them. Here it is load-bearing from the floor up:

- Acting tools are gated **in the server**, not in the prompt, so a hostile
  string in somebody else's transcript cannot make him act.
- Anything read from a web page, a search result or a connected service is
  information to report and **never** an instruction to follow — and for the
  rest of any turn that read the web, the unsupervised actions are shut.
- What he puts on record for good — a memory, a document approval, a
  business record — waits until nothing he did not write is in his context.
  You can still change a business record by voice: he finds it by your
  words and is handed back only its id and checked fields, never what the
  record says, so a note pasted from a stranger's email cannot talk him
  into writing one.
- The loopback tool channel is bound to a bearer token created `O_EXCL` at
  mode 0600, compared in constant time.
- On Windows, where a file mode is inert, the data folder — token, database,
  memory, backups — gets a DACL that admits only your account, SYSTEM and
  Administrators, written whole through advapi32; the `private_files`
  preflight check says when anything under it can be read by somebody else.
- Credentials, keys and `.env` files are refused by the file reader, and his
  own tool token by exact name.

## He has a phone now

**Public:** JARVIS exists while the browser tab is open. Walk away and a
session that stops to ask you something waits until you come back.

**Here:** give him a Telegram bot (a minute with @BotFather, no phone
number, no Facebook) or a WhatsApp number (Kapso's) and he reaches you. Every
approval card goes to your phone with Approve and Reject buttons the moment
it is staged; a session waiting on you, a failed build and finished work are
texted when nobody is in the tab, the urgent ones as a voice note in his own
voice. You text him back — a question, `approve 3f2a`, "tell chitauri to
carry on" (read back to you as text and held for your `go`), `start fresh`.

It keeps every rule the desk keeps, on both lines, out of one policy. Only
you are ever written to or read from; a stranger's message is dropped before
anything reads it. A tap
carries the card's id and its full digest and goes through the same function
as the desk's button, so it can decide only the card it was made for, once,
and the ledger says `via:whatsapp`. The key never reaches a Claude Code
child. The line is *polled*, not webhooked, because the server stays bound to
loopback and nothing from the network may reach it. And there are no calls:
Meta's calling API wants a WebRTC media stack and is not offered from US
numbers, and a Telegram bot cannot call at all, so a voice note is what
"calling" means, and `docs/telegram.md` and `docs/whatsapp.md` say so rather
than letting you find out.

A review before it went live found what those rules had missed, and closed
it. The bot token rode in every request URL, and the server logs request
URLs: it now never reaches a log. A message you *forward* is somebody else's
words — it decides nothing, and he reads it as he reads a web page. Pairing a
new phone really moves the line (it used to leave the old phone in charge
while saying "Paired"), Unpair drops a lost one at once, and a pairing code
dies after five wrong guesses. And the line is read while he works, so a
card a phone turn stages can be approved from that phone while he is holding
it.

## A limit is no longer silence

Public: nothing to run out of but money. Here: a Claude subscription has a
usage limit, and when it was reached he could only say when it would reset.
Now, if you switch it on, **ChatGPT stands in** — the Codex CLI on your own
ChatGPT subscription — with his persona, his memory and his tools, and he
goes back to Claude by himself when the limit resets, handing Claude what
was said meanwhile. It is a second brain with the same gates as the first:
Codex is given no tool of its own, your connected services sit behind the
same approval card, and what it reads taints its own thread exactly as it
would his. Off unless you switch it on, because it sends the conversation to
OpenAI; `docs/chatgpt-fallback.md` has the rest.

## And it is tested

Public: 6 test files, 43 tests. Here: 83 files, **2,405 tests**, named for the
behaviour they protect rather than the function they call.

---

## Saying this out loud

If asked how he differs, the short version is the first one — **he runs on
your Claude subscription instead of an API key** — then whichever of these
fits what was asked. Two sentences, not a tour. Never read this file aloud
verbatim, and never read out a path from it.
