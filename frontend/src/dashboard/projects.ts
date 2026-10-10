/**
 * The Projects view: master-detail. The list on the left is the cheap JOIN
 * (`/api/projects/view`); clicking a project fetches the expensive half — a
 * repo walk and a plan read — for that one project only.
 *
 * Ordering (set by the server, `projects_view.order_projects`): anything
 * needing the user first, then anything active, then the rest by recency.
 *
 * `repo.body`, session titles/summaries/reasons and run prompts all
 * originate in a README, a transcript, or model output — attacker-influenced
 * content, same discipline as Sessions and Memory: always textContent, never
 * innerHTML.
 */
import {
  listProjectViews, getProjectDetail, openProject,
  type ProjectListItem, type ProjectDetail, type SessionRow, type RunRow,
  type OpenTarget,
} from "./api";
import {
  el, row, panel, bar, pill, button, statusDot, statusPill, stateStyle,
  emptyState, stack, type Tone,
} from "./ui";
import { handFor, hostWaitLine } from "./promptowner";

let started = false;
let desktopCapabilities: Record<string, boolean> = {};
let projects: ProjectListItem[] = [];
let selectedName: string | null = null;
let detail: ProjectDetail | null = null;
/**
 * The project whose detail fetch FAILED, or null.
 *
 * `detail === null` had two meanings — "nothing chosen" and "the fetch
 * failed" — and the pane rendered the first one for both, so a failure read
 * as "Select a project to see more." while the row the user had just
 * clicked sat highlighted beside it.
 */
let detailFailed: string | null = null;
/** Guards against a slow detail fetch landing after the user picked another
 * project, or after the list moved on without them. */
let detailToken = 0;

// ── Formatting ───────────────────────────────────────────────────────────

function fmtAge(epochSec: number | null): string {
  if (epochSec === null) return "—";
  const secs = Math.max(0, Math.round(Date.now() / 1000 - epochSec));
  if (secs < 60) return "just now";
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 48) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

function fmtClock(epochSec: number): string {
  return new Date(epochSec * 1000)
    .toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function fmtDuration(run: RunRow): string {
  if (!run.started_at) return "—";
  const end = run.ended_at ?? Date.now() / 1000;
  const secs = Math.max(0, Math.round(end - run.started_at));
  return `${Math.floor(secs / 60)}m ${String(secs % 60).padStart(2, "0")}s`;
}

function fmtTokens(run: RunRow): string {
  const total = run.input_tokens + run.output_tokens;
  if (total <= 0) return "—";
  if (total < 1000) return `${total} tok`;
  if (total < 1_000_000) return `${(total / 1000).toFixed(total < 10_000 ? 1 : 0)}k tok`;
  return `${(total / 1_000_000).toFixed(1)}M tok`;
}

/** One state word for the project as a whole: the worst/most-attention-
 * worthy thing true of it right now. Reuses the same six-run/seven-session
 * vocabulary `stateStyle` already knows — no new state, no new colour.
 *
 * `live_session_count`, never `session_count`: the latter counts `gone` and
 * `fresh` conversations too, so a project whose windows had all been closed
 * wore the same green idle dot as one somebody is sitting at. */
function aggregateState(p: ProjectListItem): string {
  if (p.needs_you_count > 0) return "needs_you";
  if (p.active) return "working";
  if (p.latest_run && (p.latest_run.status === "failed" || p.latest_run.status === "timed_out")) {
    return p.latest_run.status;
  }
  if (p.live_session_count > 0) return "idle";
  if (p.latest_run) return p.latest_run.status;
  return "unknown";
}

// ── The list (master) ───────────────────────────────────────────────────

function projectRow(p: ProjectListItem): HTMLElement {
  const state = aggregateState(p);
  const style = stateStyle(state);
  const r = row({
    tone: style.tone,
    attn: p.needs_you_count > 0,
    dim: !p.directory_exists,
    onOpen: () => void selectProject(p.name),
    label: p.name,
  });
  r.root.classList.toggle("is-open", p.name === selectedName);
  r.root.dataset.project = p.name;

  r.setLead(statusDot(state));
  r.setTitle(p.name);
  r.setSub(p.directory_exists ? p.primary_path : "directory not found");

  if (p.needs_you_count > 0) {
    r.addTrail(pill(
      p.needs_you_count === 1 ? "needs you" : `${p.needs_you_count} need you`,
      "bad"));
  } else {
    r.addTrail(statusPill(state));
  }
  if (!p.directory_exists) r.addTrail(pill("missing", "warn", "ghost"));
  r.addMeta(fmtAge(p.last_activity), { w: "68px" });

  return r.root;
}

function paintList(): void {
  const listEl = document.getElementById("proj-list");
  const metaEl = document.getElementById("proj-list-meta");
  const badge = document.getElementById("projects-tab-badge");
  if (metaEl) metaEl.textContent = projects.length > 0 ? String(projects.length) : "";
  if (badge) {
    const needing = projects.reduce((n, p) => n + (p.needs_you_count > 0 ? 1 : 0), 0);
    badge.textContent = String(needing);
    badge.hidden = needing === 0;
  }
  if (!listEl) return;
  listEl.replaceChildren();
  if (projects.length === 0) {
    listEl.append(emptyState("No projects found yet."));
    return;
  }
  for (const p of projects) listEl.append(projectRow(p));
}

// ── The detail (right column) ───────────────────────────────────────────

function whereItIs(d: ProjectDetail): HTMLElement {
  const p = panel("Where It Is", { tone: d.directory_exists ? undefined : "warn" });
  const body = p.body;

  const pathRow = el("div", "proj-path-row");
  pathRow.append(el("code", "proj-path-value", d.primary_path || "(unknown)"));
  if (!d.directory_exists) {
    pathRow.append(pill("not found on disk", "warn"));
  }
  body.append(pathRow);

  if (d.paths.length > 1) {
    const others = d.paths.filter((x) => x !== d.primary_path);
    body.append(el("div", "proj-path-note",
      `Also seen at ${others.length} other location${others.length === 1 ? "" : "s"}: ${others.join(", ")}`));
  }

  const actions = el("div", "pane-actions proj-open-actions");
  const targets: [OpenTarget, string][] = [
    ["editor", "Open in Editor"], ["terminal", "Open in Terminal"],
    ["browser", "Open in Browser"],
  ];
  for (const [target, label] of targets) {
    const btn = button(label, () => void doOpen(d, target, btn));
    btn.disabled = !d.directory_exists || desktopCapabilities[`open_${target}`] !== true;
    if (desktopCapabilities[`open_${target}`] !== true) btn.title = "This action is unavailable on this server.";
    actions.append(btn);
  }
  body.append(actions);

  return p.root;
}

async function doOpen(d: ProjectDetail, target: OpenTarget, btn: HTMLButtonElement): Promise<void> {
  const original = btn.textContent ?? "";
  btn.disabled = true;
  try {
    const ok = await openProject(d.name, d.primary_path, target);
    btn.textContent = ok ? "Opened" : "Failed";
  } catch (e) {
    console.error("[projects] open failed", e);
    btn.textContent = "Failed";
  }
  setTimeout(() => {
    btn.textContent = original;
    btn.disabled = !d.directory_exists || desktopCapabilities[`open_${target}`] !== true;
  }, 1500);
}

function sessionRow(s: SessionRow): HTMLElement {
  const style = stateStyle(s.state);
  const needsYou = s.state === "needs_you";
  const r = row({ tone: style.tone, attn: needsYou, dim: s.state === "fresh" });
  r.setLead(statusDot(s.state));
  r.setTitle(s.voice_name);
  r.addTrail(statusPill(s.state));
  r.addMeta(fmtAge(s.since), { w: "62px" });

  if (needsYou) {
    const reason = s.needs || (s.last_text ? s.last_text.replace(/\s+/g, " ").slice(0, 200)
                                            : "stopped and wants you");
    r.addBody(el("div", "session-reason", reason));
    // Whose it is and where — the same words as the Sessions view.
    if (s.needs_a_human_hand || s.origin === "background") {
      const owner = handFor(s.origin);
      const hand = el("div", "session-hand");
      hand.append(pill(owner.pill, owner.tone),
        el("span", "row-meta", owner.note));
      r.addBody(hand);
    }
  } else if ((s.waiting_on_host ?? null) !== null) {
    r.addBody(el("div", "session-summary", hostWaitLine(s.waiting_on_host ?? "")));
  } else if (s.summary) {
    r.addBody(el("div", "session-summary", s.summary.replace(/\s+/g, " ")));
  }
  return r.root;
}

function conversationsPanel(d: ProjectDetail): HTMLElement {
  const needing = d.sessions.filter((s) => s.state === "needs_you").length;
  const p = panel("Conversations", {
    meta: d.sessions.length > 0 ? String(d.sessions.length) : "",
    tone: needing > 0 ? "bad" : undefined,
    alert: needing > 0,
  });
  if (d.sessions.length === 0) {
    p.body.append(emptyState("No conversations for this project."));
    return p.root;
  }
  const sorted = [...d.sessions].sort((a, b) => {
    const an = a.state === "needs_you" ? 0 : 1;
    const bn = b.state === "needs_you" ? 0 : 1;
    return an - bn || (b.since ?? 0) - (a.since ?? 0);
  });
  const list = stack();
  for (const s of sorted) list.append(sessionRow(s));
  p.body.append(list);
  return p.root;
}

function runRow(r: RunRow): HTMLElement {
  const style = stateStyle(r.status);
  const built = row({ tone: style.tone, attn: r.status === "failed" || r.status === "timed_out" });
  built.setLead(statusDot(r.status));
  built.setTitle(r.prompt.replace(/\s+/g, " "));
  built.addTrail(statusPill(r.status));
  built.addMeta(fmtDuration(r), { w: "70px" });
  built.addMeta(fmtTokens(r), { w: "70px" });
  built.addMeta(fmtClock(r.created_at), { w: "52px" });
  return built.root;
}

function runsPanel(d: ProjectDetail): HTMLElement {
  const failing = d.runs.filter((r) => r.status === "failed" || r.status === "timed_out").length;
  const p = panel("Runs", {
    meta: d.runs.length > 0 ? String(d.runs.length) : "",
    tone: failing > 0 ? "bad" : undefined,
  });
  if (d.runs.length === 0) {
    p.body.append(emptyState("No runs yet."));
    return p.root;
  }
  const list = stack();
  for (const r of d.runs) list.append(runRow(r));
  p.body.append(list);
  return p.root;
}

function buildPanel(d: ProjectDetail): HTMLElement {
  const b = d.build;
  const hasAnything = b.has_spec || b.has_plan || b.progress !== null;
  const stalled = !!b.progress && !b.progress.finished && !d.active;
  const p = panel("Build", { tone: stalled ? "bad" : undefined });

  if (!hasAnything) {
    p.body.append(emptyState("No build started for this project."));
    return p.root;
  }

  const pills = el("div", "proj-build-pills");
  pills.append(
    pill(b.has_spec ? "spec written" : "no spec", b.has_spec ? "ok" : "dim", "ghost"),
    pill(b.has_plan ? "plan written" : "no plan", b.has_plan ? "ok" : "dim", "ghost"),
  );
  if (stalled) pills.append(pill("stalled", "bad"));
  p.body.append(pills);

  if (b.progress === null) {
    p.body.append(el("div", "proj-build-note",
      b.has_spec ? "Still planning — no plan file yet." : ""));
    return p.root;
  }

  const progress = b.progress;
  const pct = progress.total > 0 ? (progress.done / progress.total) * 100 : 0;
  const tone: Tone = progress.finished ? "ok" : stalled ? "bad" : "accent";
  const g = bar("Tasks done", { tone, pct, value: `${progress.done} of ${progress.total}` });
  p.body.append(g.root);

  if (progress.current_task && !progress.finished) {
    p.body.append(el("div", "proj-build-note",
      `On Task ${progress.current_task.number}: ${progress.current_task.title}`));
  } else if (progress.finished) {
    p.body.append(el("div", "proj-build-note", "All tasks done."));
  }
  return p.root;
}

function repoPanel(d: ProjectDetail): HTMLElement {
  const p = panel("Repository");
  if (!d.directory_exists) {
    p.body.append(emptyState("Directory not found on disk — nothing to read."));
    return p.root;
  }
  if (!d.repo.exists || !d.repo.headline) {
    p.body.append(emptyState("Nothing readable here yet."));
    return p.root;
  }
  p.body.append(el("div", "proj-repo-headline", d.repo.headline));
  if (d.repo.body) {
    p.body.append(el("pre", "doc proj-repo-body", d.repo.body));
  }
  return p.root;
}

function renderDetail(): void {
  const host = document.getElementById("proj-detail");
  if (!host) return;
  host.replaceChildren();

  if (projects.length === 0) {
    host.append(emptyState("No projects found yet. JARVIS lists a project as "
      + "soon as a conversation or a run touches it."));
    return;
  }
  if (detail === null) {
    // Two different nulls, and they said the same thing. A fetch that FAILED
    // rendered "Select a project to see more." with the row still
    // highlighted — an instruction to do the thing the user had just done.
    host.append(emptyState(detailFailed === null
      ? "Select a project to see more."
      : `Could not load ${detailFailed}. The server answered, but not with `
        + "this project."));
    return;
  }

  const d = detail;
  const head = el("div", "proj-detail-head");
  head.append(statusDot(aggregateState(d)), el("h2", "proj-detail-title", d.name));
  if (!d.directory_exists) head.append(pill("directory missing", "bad"));
  host.append(head);

  host.append(whereItIs(d), conversationsPanel(d), runsPanel(d), buildPanel(d), repoPanel(d));
}

// ── Selection and fetching ──────────────────────────────────────────────

async function selectProject(name: string): Promise<void> {
  if (name === selectedName && detail !== null) return;
  selectedName = name;
  paintList();
  const token = ++detailToken;
  try {
    const loaded = await getProjectDetail(name);
    if (token !== detailToken) return;
    detail = loaded;
    detailFailed = null;
  } catch (e) {
    if (token !== detailToken) return;
    console.error("[projects] detail fetch failed", e);
    detail = null;
    detailFailed = name;
  }
  renderDetail();
}

function showBanner(text: string | null): void {
  const banner = document.getElementById("proj-banner");
  if (!banner) return;
  if (text === null) {
    banner.hidden = true;
    return;
  }
  banner.hidden = false;
  banner.textContent = text;
}

async function reconcile(): Promise<void> {
  try {
    const response = await fetch("/api/capabilities");
    desktopCapabilities = response.ok ? await response.json() : {};
  } catch { desktopCapabilities = {}; }
  try {
    projects = await listProjectViews();
    showBanner(null);
  } catch (e) {
    console.error("[projects] reconcile failed", e);
    showBanner("Cannot reach the JARVIS server.");
    return;
  }
  paintList();

  if (projects.length === 0) {
    selectedName = null;
    detail = null;
    renderDetail();
    return;
  }
  const stillThere = selectedName !== null && projects.some((p) => p.name === selectedName);
  if (!stillThere) {
    // Nothing picked yet, or the selection vanished from the list: land on
    // whatever the server put first — the thing that most deserves
    // attention right now.
    void selectProject(projects[0].name);
    return;
  }
  // Re-render the list (tones/counts may have moved) without re-fetching a
  // detail pane that is still valid.
  if (detail === null) void selectProject(selectedName as string);
  else renderDetail();
}

export function initProjects(): void {
  if (started) return;
  started = true;
  void reconcile();
  setInterval(() => void reconcile(), 10000);
}

/** Re-fetch on demand, e.g. when the tab is opened. */
export function refreshProjects(): void {
  void reconcile();
}
