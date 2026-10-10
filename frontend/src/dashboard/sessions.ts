/**
 * The Sessions view: master-detail. The list of every Claude Code
 * conversation on the left, and one conversation's answer beside it.
 *
 * THE QUESTION THIS VIEW EXISTS TO ANSWER is "does this want me, and why?".
 * So the detail panel opens with that answer and nothing else: a callout
 * carrying the state in words, and — when a session is blocked — the reason
 * verbatim, in the largest type on the panel. Everything below it (what it is
 * working on, how long, what it dispatched, where it lives) is context for
 * that one sentence.
 *
 * LAYOUT. `splitView` — the same master-detail primitive Projects and Specs
 * use, so this is not a third arrangement. Both columns are always in the
 * grid: selecting a session swaps the right column's contents and never moves
 * the list, because the list is a live monitor and a list that reflows under
 * the cursor is unusable exactly while you are working down it. On a narrow
 * window the detail becomes a right-hand sheet over a scrim; Escape, the
 * Close control, the scrim and re-clicking the open row all dismiss it.
 *
 * TWO CLOCKS, NEVER ONE. `since` is when the CURRENT STATE began; `started`
 * is when the conversation began. Measured on the live roster they were 102
 * HOURS apart. They are labelled separately and never substituted.
 *
 * `title`, `summary`, `last_prompt`, `last_text`, `needs`, subagent
 * descriptions and project names all come out of other people's transcripts
 * and are attacker-influenced — every one of them reaches the DOM through
 * textContent, never innerHTML. That is the whole reason for the rule.
 */
import {
  listSessions, getUsageSessions,
  type SessionRow, type SessionUsage, type AgentUsage,
} from "./api";
import { connectSessionsLive } from "./sessions-live";
import { handFor, hostWaitLine, startedAs } from "./promptowner";
import {
  el, row, panel, group, stack, kv, button, callout, splitView,
  statusDot, statusPill, stateStyle, pill, emptyState,
  type SplitParts, type PanelParts,
} from "./ui";

let sessions: SessionRow[] = [];
let selectedId: string | null = null;
let started = false;

/**
 * What the detail column is currently showing (a session id, or `null` for
 * the tally). This view repaints on a 30-second tick AND on every socket
 * event, so a repaint must be able to tell "the same thing, fresher" from "a
 * new selection": the first keeps your scroll position and stays still, the
 * second animates in from the top. `undefined` means nothing has been drawn.
 */
let rendered: string | null | undefined;
/** Which projects' `fresh` groups the reader has opened. A repaint rebuilds
 * the list, and without this every one of them would snap shut on the tick. */
const freshOpen = new Set<string>();

let split: SplitParts | null = null;
let banner: HTMLElement | null = null;
let needsPanel: PanelParts | null = null;
let byProjectPanel: PanelParts | null = null;

/**
 * Per-session tokens and the subagent roster, from `/api/usage/sessions`.
 *
 * `undefined` means nobody has asked yet; `null` means we asked and the scan
 * has measured nothing (or the call failed). Neither is an empty map, because
 * "this session dispatched no agents" and "nobody has looked" are different
 * answers and the panel says which one it has.
 */
let usage: Map<string, SessionUsage> | null | undefined;
let usageAt = 0;
let usageInFlight = false;
const USAGE_TTL_MS = 60_000;
/** The window `usage_scan` calls a subagent "active" — a FILE age. */
const AGENT_ACTIVE_SEC = 90;
const MAX_AGENT_ROWS = 8;

const STATE_ORDER: Record<string, number> = {
  needs_you: 0, working: 1, unknown: 2, shell: 3, idle: 4, gone: 5, fresh: 6,
};

// ── Formatting ──────────────────────────────────────────────────────────────

function now(): number {
  return Date.now() / 1000;
}

/** "51m" — the compact form, for a list column. */
function fmtShort(sinceSec: number | null): string {
  if (sinceSec === null) return "—";
  const secs = Math.max(0, Math.round(now() - sinceSec));
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  if (hours < 48) return `${hours}h`;
  return `${Math.floor(hours / 24)}d`;
}

/** "51m", "4h 12m", "3d 6h" — the readable form, for the detail panel. */
function fmtSpan(secs: number): string {
  const s = Math.max(0, Math.round(secs));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 48) return m % 60 ? `${h}h ${m % 60}m` : `${h}h`;
  const d = Math.floor(h / 24);
  return h % 24 ? `${d}d ${h % 24}h` : `${d}d`;
}

function fmtClock(epochSec: number): string {
  const d = new Date(epochSec * 1000);
  const time = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  if (d.toDateString() === new Date().toDateString()) return time;
  return `${d.toLocaleDateString([], { day: "numeric", month: "short" })} ${time}`;
}

/** 812 · 12.4k · 3.1M. Tabular everywhere it is rendered. */
function fmtCount(n: number): string {
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

function oneLine(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}

// ── The vocabulary ──────────────────────────────────────────────────────────
//
// Every session state gets a word and a sentence. The sentence says what the
// state means for the READER — whether they are wanted — because that is the
// only question this view exists to answer. Only `needs_you` gets the loud
// band; if every state shouted, none of them would.

interface Ask { label: string; line: string }

const ASK: Record<string, Ask> = {
  needs_you: { label: "Waiting on you", line: "" },
  working: {
    label: "Working",
    line: "It is mid-turn. Nothing is asked of you.",
  },
  idle: {
    label: "Idle",
    line: "It finished and stopped. Nothing is asked of you.",
  },
  shell: {
    label: "In a shell",
    line: "A command is running in that terminal — not a turn.",
  },
  fresh: {
    label: "Never prompted",
    line: "Nobody has typed into this one. It has written no transcript, so "
        + "there is nothing to read and nothing to answer.",
  },
  gone: {
    label: "Gone",
    line: "Every process on this conversation has exited.",
  },
  unknown: {
    label: "No status",
    line: "The process is alive, but its roster entry carries no status. "
        + "What it is doing is not known.",
  },
};

/** What `since` is measuring, in this state's own words. */
const SINCE_LABEL: Record<string, string> = {
  needs_you: "waiting for",
  working: "working for",
  idle: "idle for",
  shell: "in shell for",
  fresh: "alive for",
  gone: "last seen",
  unknown: "unchanged for",
};

// ── The list (master) ───────────────────────────────────────────────────────

/**
 * One session as a `row`. `showProject` is on in the Needs You panel, where
 * the project is not already implied by a group header.
 *
 * The rows are deliberately thinner than they were: the long reason and the
 * summary used to be crammed into every line, which made a list you had to
 * read instead of one you could scan. The reason stays on a `needs you` row —
 * that is the alarm, and it must be legible without a click — and everything
 * else moved into the panel beside it.
 */
function sessionRow(s: SessionRow, opts: { showProject?: boolean } = {}): HTMLElement {
  const style = stateStyle(s.state);
  const needsYou = s.state === "needs_you";

  const r = row({
    tone: style.tone,
    attn: needsYou,
    dim: s.state === "fresh",
    onOpen: () => select(s.session_id === selectedId ? null : s.session_id),
    label: `${s.voice_name} — ${style.label}`,
  });
  r.root.classList.add("sess-row");
  r.root.dataset.sessionId = s.session_id;
  r.root.classList.toggle("is-open", s.session_id === selectedId);

  r.setLead(statusDot(s.state));
  r.setTitle(s.voice_name);
  if (opts.showProject && s.project) r.setSub(s.project);
  else if (s.summary) r.setSub(oneLine(s.summary));

  r.addTrail(statusPill(s.state));

  // Which one is the MAIN session here, and what is background. The rule
  // lives in `_mark_primary` (session_watch.py) and can decline to answer:
  // two conversations touched within PRIMARY_MARGIN_SEC of each other are
  // equally live, and both say so rather than one taking the label on a
  // coin-flip. Only the two verdicts that carry information are drawn —
  // "background" is what a row without a badge already means.
  if (s.primary) {
    r.addTrail(pill("main", "accent"));
  } else if (s.primary_reason === "equally live as another here") {
    r.addTrail(pill("equally live", "warn", "ghost"));
  }
  if (s.primary_reason) r.root.dataset.primaryReason = s.primary_reason;

  if (s.agents_active > 0) {
    r.addTrail(pill(
      `${agentsActive(s)} agent${s.agents_active === 1 && !s.agents_capped
        ? "" : "s"}`, "accent"));
  }
  if (!s.steerable) r.addTrail(pill("no steer", "warn", "ghost"));
  // "— ago" would be a sentence about a measurement that does not exist.
  r.addMeta(s.since === null ? "no stamp" : `${fmtShort(s.since)} ago`,
            { cls: "sess-when" });

  // The alarm, and only the alarm, keeps a body in the list.
  if (needsYou && s.needs) r.addBody(el("div", "session-reason", oneLine(s.needs)));

  return r.root;
}

function sortSessions(rows: SessionRow[]): SessionRow[] {
  return [...rows].sort((a, b) =>
    (STATE_ORDER[a.state] ?? 9) - (STATE_ORDER[b.state] ?? 9)
    || (a.since ?? 0) - (b.since ?? 0));
}

function groupByProject(rows: SessionRow[]): [string, SessionRow[]][] {
  const map = new Map<string, SessionRow[]>();
  for (const s of rows) {
    const key = s.project || "(unknown)";
    const list = map.get(key);
    if (list) list.push(s); else map.set(key, [s]);
  }
  const groups = Array.from(map.entries());
  groups.sort(([aName, aRows], [bName, bRows]) => {
    const aNeeds = aRows.some((s) => s.state === "needs_you");
    const bNeeds = bRows.some((s) => s.state === "needs_you");
    if (aNeeds !== bNeeds) return aNeeds ? -1 : 1;
    const aActive = aRows.filter((s) => s.state !== "fresh" && s.state !== "gone").length;
    const bActive = bRows.filter((s) => s.state !== "fresh" && s.state !== "gone").length;
    if (aActive !== bActive) return bActive - aActive;
    return aName.localeCompare(bName);
  });
  return groups;
}

function projectGroup(project: string, rows: SessionRow[]): HTMLElement {
  const sorted = sortSessions(rows);
  const active = sorted.filter((s) => s.state !== "fresh");
  const fresh = sorted.filter((s) => s.state === "fresh");

  const g = group(project, String(rows.length));
  for (const s of active) g.body.append(sessionRow(s));
  if (active.length === 0) g.body.append(emptyState("Nothing but fresh sessions.", true));

  if (fresh.length > 0) {
    const details = el("details", "fresh-group");
    const summary = el("summary", undefined, `${fresh.length} fresh (never prompted)`);
    details.append(summary);
    details.open = freshOpen.has(project);
    details.addEventListener("toggle", () => {
      if (details.open) freshOpen.add(project); else freshOpen.delete(project);
    });
    const freshList = stack();
    for (const s of fresh) freshList.append(sessionRow(s));
    details.append(freshList);
    g.root.append(details);
  }

  return g.root;
}

// ── The detail (aside) ──────────────────────────────────────────────────────

/**
 * The band at the top of the panel: does this want you, and why.
 *
 * A `needs_you` session may or may not carry a reason. `waitingFor` gives one
 * verbatim; the other two routes into the state (a roster status of
 * "waiting", or a question spotted in the last message) give none at all. The
 * band never invents one — it says the reason is not recorded and shows the
 * last thing the session said, labelled as exactly that.
 */
function askBand(s: SessionRow): HTMLElement {
  const style = stateStyle(s.state);
  const ask = ASK[s.state] ?? { label: style.label, line: "" };
  const needsYou = s.state === "needs_you";

  const c = callout({
    tone: style.tone,
    label: ask.label,
    lead: statusDot(s.state),
    loud: needsYou,
    meta: s.since === null ? "" : `${SINCE_LABEL[s.state] ?? "for"} ${fmtSpan(now() - s.since)}`,
  });

  const hostWait = s.waiting_on_host ?? null;
  if (!needsYou && hostWait !== null) {
    // A program's session, paused on the program that drives it. `working`
    // is true — and so is the pause, which is what "where is it?" is asking.
    c.body.textContent = hostWaitLine(hostWait);
  } else if (!needsYou) {
    c.body.textContent = ask.line;
  } else if (s.needs) {
    c.body.textContent = s.needs;
  } else if (s.last_text) {
    c.body.textContent = oneLine(s.last_text);
    c.foot.append(el("span", "callout-note",
      "No reason recorded — that is the last thing it said."));
    c.foot.hidden = false;
  } else {
    c.body.textContent = "It has stopped and has not said why.";
  }

  if (needsYou) {
    // Whose it is and where, by origin (promptowner.ts). A program's prompt
    // is the program's whatever it waits on — the socket reaches none of them.
    if (s.needs_a_human_hand || s.origin === "background") {
      const hand = handFor(s.origin);
      c.foot.prepend(pill(hand.pill, hand.tone));
      c.foot.append(el("span", "callout-note", hand.note));
    } else if (s.steerable) {
      c.foot.prepend(pill("steerable", "ok", "ghost"));
      c.foot.append(el("span", "callout-note",
        "JARVIS can reply to this one over its inbox socket."));
    } else {
      c.foot.prepend(pill("no inbox socket", "warn", "ghost"));
      c.foot.append(el("span", "callout-note",
        "Nothing can reach this one but the terminal it is in."));
    }
    c.foot.hidden = false;
  }
  return c.root;
}

/** A labelled block of text inside a panel. */
function block(label: string, node: Node): HTMLElement {
  const wrap = el("div", "sd-block");
  wrap.append(el("span", "sd-label", label), node);
  return wrap;
}

function quote(text: string, title = false): HTMLElement {
  return el("div", title ? "sd-quote is-title" : "sd-quote", text);
}

/** What it is working on, in the words of the transcript itself. */
function doingPanel(s: SessionRow): HTMLElement {
  const p = panel("What It Is Doing");
  const parts: HTMLElement[] = [];

  if (s.title) parts.push(block("Its own title for this", quote(s.title, true)));
  if (s.last_prompt) parts.push(block("You last asked", quote(oneLine(s.last_prompt))));
  if (s.last_text) parts.push(block("It last said", quote(oneLine(s.last_text))));

  if (s.recent_tools.length > 0) {
    const pills = el("div", "sd-pills");
    for (const t of s.recent_tools) pills.append(pill(t, "idle", "ghost"));
    parts.push(block("Last tools it reached for", pills));
  }

  if (parts.length === 0) {
    p.body.append(emptyState(
      s.state === "fresh"
        ? "No transcript exists for this session — nobody has prompted it."
        : "Its transcript holds nothing readable yet."));
  } else {
    for (const part of parts) p.body.append(part);
  }
  return p.root;
}

function agentRow(a: AgentUsage): HTMLElement {
  const r = row({ tone: a.active ? "accent" : "idle", dim: !a.active });
  r.setLead(statusDot(a.active ? "working" : "idle"));
  // The sidecar's own words first; then its type; then, if the CLI wrote no
  // sidecar at all, the id — which is not a description and is not dressed
  // up as one.
  r.setTitle(a.description || a.agent_type || a.agent_id.slice(0, 8));
  r.addMeta(a.tokens.total > 0 ? `${fmtCount(a.tokens.total)} tok` : "—",
            { cls: "sess-agents" });
  if (a.description && a.agent_type) {
    r.addBody(el("div", "sd-agent-note", a.agent_type));
  }
  return r.root;
}

/**
 * What this conversation dispatched.
 *
 * Two independent sources, and they are labelled as two. `session_watch`
 * counts transcript FILES once a second — cheap, always present, and a count
 * that hit its cap is a floor ("300+"), never a total. `usage_scan` reads
 * those files and can name each agent, but it is a slow scan that may never
 * have run: when it has not, this panel says "not measured" rather than
 * drawing an empty list that looks like "none".
 */
/**
 * How many subagents are working, as a number the page may actually claim.
 *
 * A FLOOR WHENEVER THE CAP WAS HIT, exactly like `agents_seen`. `count_agents`
 * computes `capped` from the full listing but counts `active` inside
 * `transcripts[:MAX_AGENT_FILES]` — and that slice is taken in FILE-NAME
 * order, which is uncorrelated with recency, so the 300 it examined may hold
 * none of the busy ones. `agents_seen` has rendered as "300+" all along;
 * this one rendered bare, in both pills.
 */
function agentsActive(s: SessionRow): string {
  return s.agents_capped ? `${s.agents_active}+` : String(s.agents_active);
}

function agentsPanel(s: SessionRow): HTMLElement {
  const seen = s.agents_capped ? `${s.agents_seen}+` : String(s.agents_seen);
  const p = panel("Under It", {
    meta: s.agents_seen > 0 ? seen : "",
    tone: s.agents_active > 0 ? "accent" : undefined,
  });

  if (s.agents_seen === 0) {
    p.body.append(emptyState("No subagent transcripts under this conversation."));
    return p.root;
  }

  const line = el("div", "sd-block");
  const pills = el("div", "sd-pills");
  pills.append(
    pill(`${agentsActive(s)} active`, s.agents_active > 0 ? "accent" : "idle"),
    pill(`${seen} dispatched`, "idle", "ghost"),
  );
  line.append(pills, el("div", "sd-agent-note",
    `"Active" means a transcript written in the last ${AGENT_ACTIVE_SEC} seconds. `
    + "It is a file age, not a live process — there is no process here to check."
    + (s.agents_capped
      ? ` Both counts hit the watcher's cap of ${s.agents_seen} files, so both`
        + " are floors, not totals — the cap is taken in file-name order,"
        + " which has nothing to do with which agents are busy."
      : "")));
  p.body.append(line);

  const measured = usage?.get(s.session_id);
  if (usage === undefined) {
    p.body.append(emptyState("Reading the transcripts…", true));
    return p.root;
  }
  if (!measured) {
    p.body.append(emptyState(
      "Not measured — the transcript scan has not read this session, so "
      + "there is nothing to say about the agents individually.", true));
    return p.root;
  }

  const agents = [...measured.agents].sort((a, b) =>
    Number(b.active) - Number(a.active) || (b.last_at ?? 0) - (a.last_at ?? 0));
  if (agents.length === 0) {
    p.body.append(emptyState("The scan found no agent transcripts to read.", true));
    return p.root;
  }
  const list = stack();
  for (const a of agents.slice(0, MAX_AGENT_ROWS)) list.append(agentRow(a));
  p.body.append(block("Agents, most recent first", list));
  if (agents.length > MAX_AGENT_ROWS) {
    p.body.append(el("div", "sd-agent-note",
      `${agents.length - MAX_AGENT_ROWS} more not shown.`));
  }
  return p.root;
}

/**
 * The two clocks and the identity.
 *
 * `in this state` and `session age` are NOT the same measurement and are
 * never allowed to stand in for one another: the largest gap measured
 * between them on the live roster was 102 hours. Either can be absent — a
 * roster entry may carry neither stamp — and absent reads as "not recorded",
 * never as a zero.
 */
function sessionPanel(s: SessionRow): HTMLElement {
  const p = panel("The Session");
  const fields = kv();

  const sinceKey = `in this state`;
  fields.add(sinceKey, s.since === null ? "not recorded"
    : `${fmtSpan(now() - s.since)} · since ${fmtClock(s.since)}`,
    s.since === null ? "dim" : undefined);
  fields.add("session age", s.started === null ? "not recorded"
    : `${fmtSpan(now() - s.started)} · started ${fmtClock(s.started)}`,
    s.started === null ? "dim" : undefined);

  fields.add("project", s.project || "(unknown)");
  fields.add("folder", s.cwd || "(unknown)");
  fields.add("started as", startedAs(s.origin, s.entrypoint));
  fields.add("steerable", s.steerable
    ? "yes — it bound an inbox socket"
    : "no — no inbox socket to reach it on", s.steerable ? "ok" : "warn");
  // The verdict AND the watcher's own reason for it. A bare boolean is a
  // claim; the reason is what lets a reader check it.
  fields.add("main here", s.primary ? "yes" : "no");
  if (s.primary_reason) fields.add("because", s.primary_reason);

  const measured = usage?.get(s.session_id);
  if (measured) {
    // The LAST turn's carried context, not the sum of every turn. Null when
    // the transcript holds no assistant turn at all: unknown, not zero.
    fields.add("context carried", measured.context_tokens === null
      ? "not measured"
      : `${fmtCount(measured.context_tokens)} tok on its last turn`,
      measured.context_tokens === null ? "dim" : undefined);
    fields.add("turns", String(measured.turns));
  }

  p.body.append(fields.root);
  p.body.append(block("Session id", el("div", "sd-id", s.session_id)));
  if (s.pids.length > 0) {
    p.body.append(el("div", "sd-agent-note",
      `${s.pids.length === 1 ? "Process" : "Processes"} ${s.pids.join(", ")}`
      + (s.primary_pid !== null ? ` · steered through ${s.primary_pid}` : "")));
  }
  return p.root;
}

function detailFor(s: SessionRow): HTMLElement {
  const wrap = el("div", "sd");

  const head = el("div", "sd-head");
  const close = button("Close", () => select(null), { quiet: true });
  close.title = "Close (Esc)";
  head.append(statusDot(s.state), el("h2", "sd-name", s.voice_name), close);

  const where = el("div", "sd-where");
  where.append(el("span", undefined, s.project || "(unknown project)"));
  if (s.primary) where.append(pill("main here", "accent"));
  else if (s.primary_reason === "equally live as another here") {
    where.append(pill("equally live", "warn", "ghost"));
  }
  where.append(pill(s.origin, "idle", "ghost"));
  head.append(where);

  wrap.append(head, askBand(s), doingPanel(s), agentsPanel(s), sessionPanel(s));
  return wrap;
}

/** Standing content for the right column when nothing is selected. It is not
 * dead space: it is the tally the list would otherwise make you count. */
function glance(): HTMLElement {
  const wrap = el("div", "sd");
  const p = panel("At a Glance", { meta: sessions.length > 0 ? String(sessions.length) : "" });

  if (sessions.length === 0) {
    p.body.append(emptyState("No Claude Code conversations on this machine."));
    wrap.append(p.root);
    return wrap;
  }

  const counts = new Map<string, number>();
  for (const s of sessions) counts.set(s.state, (counts.get(s.state) ?? 0) + 1);
  const states = Array.from(counts.keys())
    .sort((a, b) => (STATE_ORDER[a] ?? 9) - (STATE_ORDER[b] ?? 9));

  const list = stack();
  for (const state of states) {
    const style = stateStyle(state);
    const r = row({ tone: style.tone, attn: state === "needs_you" });
    r.setLead(statusDot(state));
    r.setTitle(style.label);
    r.addMeta(String(counts.get(state)), { cls: "sess-count", strong: true });
    list.append(r.root);
  }
  p.body.append(list);
  p.body.append(el("div", "sd-agent-note",
    "Pick one for what it is doing, whether it wants you, and why."));
  wrap.append(p.root);
  return wrap;
}

// ── Selection ───────────────────────────────────────────────────────────────

function select(id: string | null): void {
  selectedId = id;
  if (id !== null) void ensureUsage();
  paint();
}

/**
 * Fetch the per-session token scan, at most once a minute, and only once
 * somebody has actually opened a detail panel. It is a multi-second cold read
 * of every transcript on the machine, so it must never sit on the path that
 * paints the list.
 */
async function ensureUsage(): Promise<void> {
  if (usageInFlight) return;
  if (usage !== undefined && Date.now() - usageAt < USAGE_TTL_MS) return;
  usageInFlight = true;
  try {
    const scan = await getUsageSessions();
    usage = scan.measured
      ? new Map([...scan.sessions, ...scan.own_sessions].map((s) => [s.session_id, s]))
      : null;
  } catch (e) {
    console.error("[sessions] usage scan failed", e);
    usage = null;
  } finally {
    usageInFlight = false;
    usageAt = Date.now();
  }
  paint();
}

// ── Painting ────────────────────────────────────────────────────────────────

function tabBadge(count: number): void {
  const badge = document.getElementById("sessions-badge");
  if (!badge) return;
  badge.textContent = String(count);
  badge.hidden = count === 0;
}

function paint(): void {
  if (!split || !needsPanel || !byProjectPanel) return;

  // The list is rebuilt from scratch, which would drop a keyboard user out of
  // it every 30 seconds. Remember where they were and put them back.
  const focused = document.activeElement instanceof HTMLElement
    ? document.activeElement.dataset.sessionId ?? null : null;

  const needsYou = sessions
    .filter((s) => s.state === "needs_you")
    .sort((a, b) => (a.since ?? 0) - (b.since ?? 0));
  tabBadge(needsYou.length);

  needsPanel.root.hidden = needsYou.length === 0;
  needsPanel.setMeta(String(needsYou.length));
  const needsList = stack();
  for (const s of needsYou) needsList.append(sessionRow(s, { showProject: true }));
  needsPanel.body.replaceChildren(needsList);

  byProjectPanel.setMeta(sessions.length > 0 ? String(sessions.length) : "");
  if (sessions.length === 0) {
    byProjectPanel.body.replaceChildren(emptyState("No sessions found."));
  } else {
    const groups = document.createDocumentFragment();
    for (const [project, rows] of groupByProject(sessions)) {
      groups.append(projectGroup(project, rows));
    }
    byProjectPanel.body.replaceChildren(groups);
  }

  const chosen = selectedId === null ? undefined
    : sessions.find((s) => s.session_id === selectedId);
  if (selectedId !== null && chosen === undefined) {
    // It left the roster while it was open. Nothing honest is left to show.
    selectedId = null;
  }
  if (chosen) {
    const node = detailFor(chosen);
    if (rendered === chosen.session_id) split.update(node);
    else split.show(node);
    rendered = chosen.session_id;
  } else {
    const node = glance();
    if (rendered === null) split.update(node); else split.rest(node);
    rendered = null;
  }

  if (focused !== null) {
    const back = document.querySelector<HTMLElement>(
      `#sessions-view [data-session-id="${CSS.escape(focused)}"]`);
    back?.focus({ preventScroll: true });
  }
}

function showSessionsBanner(text: string | null): void {
  if (!banner) return;
  banner.hidden = text === null;
  banner.textContent = text ?? "";
}

/**
 * How old the roster reading itself is, in seconds — or null before the
 * first one.
 *
 * `taken_at` was in the payload and in the type and was rendered NOWHERE.
 * It is the only thing on the page that can show a FROZEN watcher: one poll
 * that raises leaves the last snapshot standing and `/api/sessions` goes on
 * answering 200 with it, so every row looks current and none of it is.
 */
let takenAt: number | null = null;

const SNAPSHOT_STALE_SEC = 15;

function paintTakenAt(): void {
  if (takenAt === null) return;
  const age = Date.now() / 1000 - takenAt;
  if (age <= SNAPSHOT_STALE_SEC) {
    showSessionsBanner(null);
    return;
  }
  showSessionsBanner(
    `This is a reading from ${fmtShort(takenAt)} ago — the watcher has not `
    + "polled since. What is below may have moved.");
}

async function reconcile(): Promise<void> {
  try {
    const snap = await listSessions();
    sessions = snap.sessions;
    takenAt = snap.taken_at || null;
    paint();
    showSessionsBanner(null);
    paintTakenAt();
  } catch (e) {
    console.error("[sessions] reconcile failed", e);
    showSessionsBanner("Cannot reach the JARVIS server.");
  }
}

function onSessionEvent(_kind: string, session: SessionRow): void {
  const i = sessions.findIndex((s) => s.session_id === session.session_id);
  if (i >= 0) sessions[i] = session; else sessions.push(session);
  paint();
}

/** Build the shell once: the banner, the split, and the two list panels. */
function build(): void {
  const view = document.getElementById("sessions-view");
  if (!view) return;

  banner = el("div", "empty");
  banner.id = "sessions-banner";
  banner.hidden = true;

  split = splitView({
    shape: "aside",
    stick: "detail",
    dismiss: () => select(null),
  });

  // Named so the panel is addressable from outside — and so a selector does
  // not accidentally find the Projects view's detail column instead, which
  // wears the same primitive class.
  split.detail.id = "sessions-detail";

  needsPanel = panel("Needs You", { tone: "bad", alert: true, meta: "0" });
  needsPanel.root.hidden = true;
  byProjectPanel = panel("Sessions by Project");
  split.master.append(needsPanel.root, byProjectPanel.root);

  view.replaceChildren(banner, split.root);
  paint();
}

export function initSessions(): void {
  if (started) return;
  started = true;

  build();
  void reconcile();

  connectSessionsLive({
    onReconcile: () => void reconcile(),
    onSessionEvent,
    onConnectionChange: (connected) => {
      if (!connected) showSessionsBanner("Reconnecting to JARVIS…");
    },
  });

  // Ages drift slowly; a light tick keeps them honest without repainting
  // every second the way active runs need.
  setInterval(() => {
    if (sessions.length > 0) paint();
  }, 30000);

  // The reading's own age has to be checked more often than the rows are
  // repainted: a watcher that stops polling produces no event to notice.
  setInterval(paintTakenAt, 5000);
}
