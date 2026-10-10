import { openBusiness } from "../business";
// frontend/src/dashboard/main.ts
//
// The Runs view and the shell around it (masthead readouts, tab switching,
// link state). Everything visual is built from the primitives in ui.ts.
//
// `prompt`, `project_name` and the now-line all originate in model output or
// on someone's disk: every one of them goes through textContent.
import {
  listRuns, listActiveRuns, runTokens, getStats, getUsageLimits,
  type RunRow, type UsageSnapshot,
} from "./api";
import { connectLive } from "./live";
import { openDetail, appendEvent, notifyRunChanged, refreshTranscript } from "./detail";
import { summaryFor, noteEvent } from "./nowline";
import { initSessions } from "./sessions";
import { initMemory, refreshMemory } from "./memory";
import { initProjects, refreshProjects } from "./projects";
import { initSpecs, refreshSpecs } from "./specs";
import { initUsage, refreshUsageView } from "./usage";
import {
  el, row, readout, bar, pill, setTone, statusDot, statusPill, stateStyle,
  emptyState, flash,
  type BarParts, type ReadoutParts, type Tone,
} from "./ui";

const ACTIVE = new Set(["queued", "running"]);
const FAILED = new Set(["failed", "timed_out"]);
/** How many failures the attention panel surfaces before it stops helping. */
const ATTENTION_LIMIT = 5;

let runs: RunRow[] = [];
let historyCursor: RunRow | undefined;
let historyDone = false;
let historyLoading = false;
let historyProject = "";
let historyGeneration = 0;
const HISTORY_STATUS = "succeeded,failed,timed_out,cancelled";
/** Runs whose status changed since the last paint — they get the flash. */
const changed = new Set<string>();

function fmtDuration(run: RunRow): string {
  if (!run.started_at) return "—";
  const end = run.ended_at ?? Date.now() / 1000;
  const secs = Math.max(0, Math.round(end - run.started_at));
  return `${Math.floor(secs / 60)}m ${String(secs % 60).padStart(2, "0")}s`;
}

/** 812 · 12.4k · 3.1M. Tabular, so a ticking number doesn't shove the column. */
function fmtCount(n: number): string {
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

/** What a run actually consumed. Not dollars: JARVIS runs on the user's
 * subscription and bills nobody, so the CLI's cost figure is a price list
 * for an API call that never happened. Tokens are the honest quantity. */
function fmtRunTokens(run: RunRow): string {
  const total = runTokens(run);
  return total > 0 ? `${fmtCount(total)} tok` : "—";
}

function fmtClock(epochSec: number): string {
  return new Date(epochSec * 1000)
    .toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/** "4:20pm" — the same clock a person would say out loud. */
function fmtTime(d: Date): string {
  return d
    .toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    .replace(/\s?([AaPp])\.?[Mm]\.?$/, (_m, half: string) => `${half.toLowerCase()}m`);
}

/** "4:20pm" today, "Thu 9:00am" later this week, "12 Sep 9:00am" beyond it. */
function fmtWhen(epochSec: number, now = new Date()): string {
  const d = new Date(epochSec * 1000);
  if (d.toDateString() === now.toDateString()) return fmtTime(d);
  const days = (d.getTime() - now.getTime()) / 86_400_000;
  if (days > 0 && days < 6) {
    return `${d.toLocaleDateString([], { weekday: "short" })} ${fmtTime(d)}`;
  }
  return `${d.toLocaleDateString([], { day: "numeric", month: "short" })} ${fmtTime(d)}`;
}

/** How long ago a reading was taken, in words. */
function fmtAge(sec: number): string {
  if (sec < 45) return "just now";
  if (sec < 3600) return `${Math.round(sec / 60)}m ago`;
  if (sec < 36 * 3600) return `${Math.round(sec / 3600)}h ago`;
  return `${Math.round(sec / 86_400)}d ago`;
}

/** The CLI's own courtesy warning fires at 75% of a window; past 90% the next
 * long run is the one that gets cut off. Those are the two lines. */
function usageTone(pct: number): Tone {
  if (pct >= 90) return "bad";
  if (pct >= 75) return "warn";
  return "accent";
}

// ── Masthead readout cluster ────────────────────────────────────────────────
//
// Two zones, in the order they matter:
//
//   LIMITS   how much of the five-hour and seven-day windows is gone, and
//            when each one resets. The hero. It is the only place this can
//            be seen, and it decides whether JARVIS can answer at all.
//   TODAY    what is running right now, then the day's volume.
//
// There is no Spend. JARVIS runs on the user's Claude subscription — the
// brain scrubs every ANTHROPIC_* variable so the CLI can never bill a key —
// so the CLI's cost figure is the price of an API call nobody made. It used
// to be the largest number on the page.
//
// Everything is built once and updated in place, so a number that changes
// flashes and a number that doesn't stays perfectly still.

interface Gauge { bar: BarParts; when: HTMLElement }

interface Masthead {
  /** Provenance of the reading: "measured 3m ago" / "no reading yet". */
  usageNote: HTMLElement;
  /** Only shown when something is wrong with the reading itself. */
  usagePill: HTMLElement;
  usageGauges: HTMLElement;
  gauges: Map<string, Gauge>;
  nowDot: HTMLElement;
  nowText: HTMLElement;
  nowRoot: HTMLElement;
  attn: HTMLElement;
  runs: ReadoutParts;
  ok: ReadoutParts;
  bad: ReadoutParts;
  tokens: ReadoutParts;
}

let masthead: Masthead | null = null;

function mastheadEnsured(): Masthead | null {
  if (masthead) return masthead;
  const host = document.getElementById("stats");
  if (!host) return null;

  // Zone 1 — the limits.
  const usagePill = pill("no reading", "warn");
  usagePill.hidden = true;
  const usageNote = el("span", "stat-zone-note");
  const usageHead = el("div", "stat-zone-head");
  usageHead.append(el("span", "stat-zone-title", "Subscription"), usagePill, usageNote);
  const usageGauges = el("div", "usage-gauges");
  const limits = el("section", "stat-zone");
  limits.append(usageHead, usageGauges);

  // Zone 2 — now, then today.
  const nowDot = el("span", "dot dot--void tone-idle");
  nowDot.setAttribute("aria-hidden", "true");
  const nowText = el("span", undefined, "no runs yet");
  const nowRoot = el("div", "stat-now tone-idle");
  nowRoot.append(nowDot, nowText);
  const attn = pill("0 need you", "bad");
  attn.hidden = true;

  const cells = {
    runs: readout("Runs", "0", { size: "sm" }),
    ok: readout("Succeeded", "0", { size: "sm" }),
    bad: readout("Failed", "0", { size: "sm", tone: "dim" }),
    tokens: readout("Tokens", "0", { size: "sm" }),
  };
  const todayHead = el("div", "stat-zone-head");
  todayHead.append(el("span", "stat-zone-title", "Today"), nowRoot, attn);
  const figures = el("div", "stat-figures");
  figures.append(cells.runs.root, cells.ok.root, cells.bad.root, cells.tokens.root);
  const today = el("section", "stat-zone");
  today.append(todayHead, figures);

  host.replaceChildren(limits, today);
  masthead = {
    usageNote, usagePill, usageGauges, gauges: new Map(),
    nowDot, nowText, nowRoot, attn, ...cells,
  };
  return masthead;
}

/**
 * Paint the usage zone.
 *
 * `null` means the endpoint could not be reached — which is NOT the same as
 * "nothing used", so it says so and draws no gauges rather than empty ones.
 *
 * BOTH PATHS OWN THE WHOLE HOST. The failure path used `replaceChildren` and
 * the success path only ever appended, so one dropped 60-second poll left
 * "Cannot read the usage limits." sitting above two live gauges — for ever,
 * because the gauges it had cleared were rebuilt beside the message rather
 * than in place of it. A window that disappears from the payload has the
 * same shape of bug: its old gauge stayed on screen with a number nothing
 * was still reporting.
 */
function renderUsage(snap: UsageSnapshot | null): void {
  const m = mastheadEnsured();
  if (!m) return;

  if (snap === null) {
    m.gauges.clear();
    m.usagePill.hidden = false;
    m.usagePill.textContent = "unavailable";
    setTone(m.usagePill, "idle");
    m.usageNote.textContent = "";
    m.usageGauges.replaceChildren(
      el("div", "usage-unavailable", "Cannot read the usage limits."),
    );
    return;
  }

  // Anything in the host that is not one of this payload's gauges is stale:
  // the failure message, or a window the server has stopped reporting.
  const wanted = new Set(snap.windows.map((w) => w.key));
  for (const [key, gauge] of [...m.gauges]) {
    if (!wanted.has(key)) {
      gauge.bar.root.parentElement?.remove();
      m.gauges.delete(key);
    }
  }
  for (const child of [...m.usageGauges.children]) {
    if (!child.classList.contains("usage-gauge")) child.remove();
  }

  // The provenance line. Absence of a reading is stated in words: JARVIS
  // only learns its usage when the brain takes a turn, so a fresh start
  // genuinely knows nothing — and must not imply a full tank.
  if (!snap.measured) {
    m.usagePill.hidden = false;
    m.usagePill.textContent = "no reading";
    setTone(m.usagePill, "warn");
    m.usageNote.textContent = "nothing measured since JARVIS started";
  } else if (snap.stale) {
    m.usagePill.hidden = false;
    m.usagePill.textContent = "stale";
    setTone(m.usagePill, "warn");
    m.usageNote.textContent = `last measured ${fmtAge(snap.age_sec ?? 0)}`;
  } else {
    m.usagePill.hidden = true;
    m.usageNote.textContent = `measured ${fmtAge(snap.age_sec ?? 0)}`;
  }

  for (const w of snap.windows) {
    let gauge = m.gauges.get(w.key);
    if (!gauge) {
      const b = bar(w.label);
      const when = el("div", "usage-when");
      const wrap = el("div", "usage-gauge");
      wrap.append(b.root, when);
      m.usageGauges.append(wrap);
      gauge = { bar: b, when };
      m.gauges.set(w.key, gauge);
    }

    // A window past its reset describes a window that no longer exists, so
    // it is unknown again — not 0%, and not the old high-water mark.
    const usable = w.utilization !== null && !w.expired;
    if (usable) {
      const pct = w.utilization as number;
      setTone(gauge.bar.root, usageTone(pct));
      gauge.bar.set(pct, `${Math.round(pct)}%`);
    } else {
      setTone(gauge.bar.root, "dim");
      gauge.bar.unknown(w.expired ? "reset since" : "not measured");
    }
    gauge.bar.setStale(usable && w.stale);

    const parts: string[] = [];
    if (w.resets_at !== null) {
      parts.push(w.expired ? `reset ${fmtWhen(w.resets_at)}` : `resets ${fmtWhen(w.resets_at)}`);
    } else if (!usable) {
      parts.push("no reading yet");
    } else {
      parts.push("reset time unknown");
    }
    if (usable && w.stale && w.age_sec !== null) parts.push(fmtAge(w.age_sec));
    gauge.when.textContent = parts.join(" · ");
  }
}

/** What is happening this second: active runs, and anything wanting a human. */
function renderNow(active: number, failures: number): void {
  const m = mastheadEnsured();
  if (!m) return;

  const tone: Tone = active > 0 ? "accent" : "idle";
  m.nowDot.className = `dot ${active > 0 ? "dot--live" : "dot--void"} tone-${tone}`;
  setTone(m.nowRoot, tone);
  m.nowText.textContent =
    active > 0 ? `${active} running` : runs.length > 0 ? "all quiet" : "no runs yet";

  m.attn.hidden = failures === 0;
  m.attn.textContent = `${failures} need${failures === 1 ? "s" : ""} you`;
}

// ── Link state ──────────────────────────────────────────────────────────────

type Link = "ok" | "retrying" | "down";

function setLink(state: Link): void {
  const host = document.getElementById("link-state");
  const text = document.getElementById("link-state-text");
  if (!host || !text) return;
  const spec = {
    ok: { tone: "tone-ok", dot: "dot--done", label: "Link OK" },
    retrying: { tone: "tone-warn", dot: "dot--live", label: "Reconnecting" },
    down: { tone: "tone-bad", dot: "dot--fault", label: "Offline" },
  }[state];
  host.className = `link-state ${spec.tone}`;
  const dot = host.querySelector(".dot");
  if (dot) dot.className = `dot ${spec.dot} ${spec.tone}`;
  text.textContent = spec.label;
}

// ── Rows ────────────────────────────────────────────────────────────────────

function runRow(run: RunRow, opts: { attn?: boolean } = {}): HTMLElement {
  const style = stateStyle(run.status);
  const r = row({
    tone: style.tone,
    attn: opts.attn,
    onOpen: () => void openDetail(run.id),
    label: `${run.project_name} — ${style.label}`,
  });

  r.setLead(statusDot(run.status));
  r.setTitle(run.project_name);
  // The prompt is what actually tells two runs of the same project apart.
  r.setSub(run.prompt.replace(/\s+/g, " "));

  const status = statusPill(run.status);
  status.classList.add("run-status");
  r.addTrail(status);
  r.addMeta(fmtDuration(run), { cls: "run-dur" });
  r.addMeta(fmtRunTokens(run), { cls: "run-tokens" });
  r.addMeta(fmtClock(run.created_at), { cls: "run-when" });

  const now = summaryFor(run.id);
  if (ACTIVE.has(run.status) && now) r.setNote(`└ ${now}`);

  if (changed.has(run.id)) {
    changed.delete(run.id);
    flash(r.root);
  }
  return r.root;
}

function renderInto(
  listId: string, metaId: string, rows: RunRow[],
  emptyText: string, opts: { attn?: boolean } = {},
): void {
  const container = document.getElementById(listId);
  if (!container) return;
  container.replaceChildren();

  const meta = document.getElementById(metaId);
  if (meta) meta.textContent = rows.length > 0 ? String(rows.length) : "";

  if (rows.length === 0) {
    container.append(emptyState(emptyText));
    return;
  }
  for (const run of rows) container.append(runRow(run, opts));
}

function paint(): void {
  // Failures are pulled to the top of the view rather than left buried in
  // history — they still appear in History too, in their own place in time.
  const allFailures = runs.filter((r) => FAILED.has(r.status));
  const failures = allFailures.slice(0, ATTENTION_LIMIT);
  renderNow(runs.filter((r) => ACTIVE.has(r.status)).length, allFailures.length);
  const attention = document.getElementById("attention-section");
  if (attention) attention.hidden = failures.length === 0;
  if (failures.length > 0) {
    renderInto("attention-list", "attention-meta", failures, "", { attn: true });
  }

  renderInto(
    "active-list", "active-meta",
    runs.filter((r) => ACTIVE.has(r.status)),
    "Nothing running.",
  );
  renderInto(
    "history-list", "history-meta",
    runs.filter((r) => !ACTIVE.has(r.status)
      && (!historyProject || r.project_name.toLowerCase().includes(historyProject.toLowerCase()))),
    "No runs yet. Ask JARVIS to build something.",
  );
}

async function reconcile(): Promise<void> {
  const generation = historyGeneration;
  try {
    const [active, history, stats] = await Promise.all([
      listActiveRuns(), listRuns(100, { status: HISTORY_STATUS, project: historyProject }), getStats("day"),
    ]);
    if (generation !== historyGeneration) return;
    const fresh = [...active, ...history.filter(r => !ACTIVE.has(r.status))];
    // Note which runs changed status so paint() can flash exactly those.
    const before = new Map(runs.map((r) => [r.id, r.status]));
    for (const r of fresh) {
      const was = before.get(r.id);
      if (was !== undefined && was !== r.status) changed.add(r.id);
    }
    const merged = new Map(runs.filter(r => !ACTIVE.has(r.status)).map(r => [r.id, r]));
    for (const r of fresh) merged.set(r.id, r);
    runs = [...merged.values()].sort((a, b) => b.created_at - a.created_at || b.id.localeCompare(a.id));
    if (!historyCursor) {
      historyCursor = history[history.length - 1];
      historyDone = history.length < 100;
    }
    updateHistoryButton();
    paint();
    for (const run of fresh) notifyRunChanged(run);
    refreshTranscript();

    const cells = mastheadEnsured();
    if (cells) {
      const ok = stats.by_status.succeeded ?? 0;
      const bad = (stats.by_status.failed ?? 0) + (stats.by_status.timed_out ?? 0);
      cells.runs.set(String(stats.total_runs));
      cells.ok.set(String(ok));
      cells.bad.set(String(bad), bad > 0 ? "bad" : "dim");
      cells.tokens.set(
        fmtCount(stats.total_tokens ?? (stats.total_input_tokens + stats.total_output_tokens
          + (stats.total_cache_read_tokens || 0) + (stats.total_cache_creation_tokens || 0))));
    }
    showBanner(null);
  } catch (e) {
    console.error("[dashboard] reconcile failed", e);
    showBanner("Cannot reach the JARVIS server.");
  }
  await refreshUsage();
}

function updateHistoryButton(): void {
  const button = document.getElementById("history-more") as HTMLButtonElement | null;
  if (button) { button.hidden = historyDone; button.disabled = historyLoading; }
}

async function loadOlderRuns(): Promise<void> {
  if (historyDone || historyLoading || !historyCursor) return;
  historyLoading = true;
  updateHistoryButton();
  const generation = historyGeneration;
  try {
    const page = await listRuns(100, { status: HISTORY_STATUS, project: historyProject,
      before: historyCursor.created_at, before_id: historyCursor.id });
    if (generation !== historyGeneration) return;
    const merged = new Map(runs.map(r => [r.id, r]));
    for (const r of page) if (!merged.has(r.id)) merged.set(r.id, r);
    runs = [...merged.values()].sort((a, b) => b.created_at - a.created_at || b.id.localeCompare(a.id));
    historyCursor = page[page.length - 1] ?? historyCursor;
    historyDone = page.length < 100;
    paint();
  } catch {
    showBanner("Could not load older runs. Try again.");
  } finally {
    historyLoading = false;
    updateHistoryButton();
  }
}

/**
 * The usage reading, fetched on its own so a failure here can never blank the
 * runs list — and so an idle dashboard still ages its "measured 4h ago" line
 * rather than freezing on whatever it said at load.
 */
async function refreshUsage(): Promise<void> {
  try {
    renderUsage(await getUsageLimits());
  } catch (e) {
    console.error("[dashboard] usage fetch failed", e);
    renderUsage(null);
  }
}

function showBanner(text: string | null): void {
  const banner = document.getElementById("connection-banner");
  if (!banner) return;
  if (text === null) {
    banner.hidden = true;
    setLink("ok");
    return;
  }
  banner.hidden = false;
  banner.textContent = text;
  setLink(text.startsWith("Reconnecting") ? "retrying" : "down");
}

function onRunChanged(run: RunRow): void {
  const i = runs.findIndex((r) => r.id === run.id);
  if (i >= 0) {
    if (runs[i].status !== run.status) changed.add(run.id);
    runs[i] = run;
  } else {
    changed.add(run.id);
    runs.unshift(run);
  }
  paint();
  notifyRunChanged(run);
}

function setupTabs(): void {
  const tabs = Array.from(document.querySelectorAll<HTMLButtonElement>(".tab"));
  const views: Record<string, HTMLElement | null> = {
    runs: document.getElementById("runs-view"),
    sessions: document.getElementById("sessions-view"),
    memory: document.getElementById("memory-view"),
    projects: document.getElementById("projects-view"),
    specs: document.getElementById("specs-view"),
    usage: document.getElementById("usage-view"),
  };
  for (const tab of tabs) {
    tab.addEventListener("click", () => {
      const view = tab.dataset.view ?? "runs";
      for (const t of tabs) {
        const on = t === tab;
        t.classList.toggle("active", on);
        t.setAttribute("aria-selected", String(on));
      }
      for (const [key, target] of Object.entries(views)) {
        if (target) target.hidden = key !== view;
      }
      // The memory folder is edited on disk with no live socket pushing
      // changes, so re-fetch each time the tab is opened.
      if (view === "memory") refreshMemory();
      // Same idea for Projects: no live socket, so a poll-on-open keeps a
      // stale "needs you" from sitting there after it's been answered.
      if (view === "projects") refreshProjects();
      // Specs DOES have a socket, but a document JARVIS rewrote while the
      // tab was hidden is exactly what the user has come back to read: cost
      // one fetch rather than risk showing yesterday's words.
      if (view === "specs") refreshSpecs();
      // Usage polls on a minute, which is the right cadence for a tab
      // sitting open and the wrong one for a tab just opened — the numbers
      // would be up to a minute stale at the moment they are looked at.
      if (view === "usage") void refreshUsageView();
    });
  }
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("history-more")?.addEventListener("click", () => void loadOlderRuns());
  document.getElementById("history-filter")?.addEventListener("submit", e => {
    e.preventDefault();
    historyProject = (document.getElementById("history-project") as HTMLInputElement).value.trim();
    historyGeneration++;
    historyCursor = undefined;
    historyDone = false;
    runs = runs.filter(r => ACTIVE.has(r.status));
    void reconcile();
  });
  void reconcile();
  setupTabs();
  // Loads and live-updates in the background regardless of which tab is
  // showing, so switching to Sessions never shows a blank first paint.
  initSessions();
  // Same idea for Memory: load it up front so switching tabs is instant.
  // No live socket for this one — the folder is user-edited on disk, not
  // pushed to — so it's a one-shot fetch, not an ongoing connection.
  initMemory();
  // Projects has its own light poll (no live socket backs the join either),
  // so it loads and stays current regardless of which tab is showing.
  initProjects();
  // Specs opens its own socket (/ws/specs), which only ever says "something
  // moved" — the view re-reads /api/specs for the truth.
  initSpecs();
  // Usage has no socket either: the limit reading only arrives when the
  // brain takes a turn, and the transcripts are written by other processes
  // entirely. It loads up front and polls.
  initUsage();

  connectLive({
    onReconcile: () => void reconcile(),
    onRunChanged,
    onRunEvent: (runId, seq, kind, payload) => {
      if (noteEvent(runId, kind, payload)) paint();
      appendEvent(runId, seq, kind, payload);
    },
    onConnectionChange: (connected) =>
      showBanner(connected ? null : "Reconnecting to JARVIS…"),
  });

  // Elapsed time ticks locally so active rows stay honest between frames.
  setInterval(() => {
    if (runs.some((r) => ACTIVE.has(r.status))) paint();
  }, 1000);

  // The usage reading has no socket — it changes when the brain takes a turn,
  // which the dashboard never hears about. Poll it, so a page left open goes
  // on telling the truth about how old its numbers are.
  setInterval(() => void refreshUsage(), 60_000);
});

document.getElementById("btn-business")?.addEventListener("click", openBusiness);
