/**
 * The run detail pane: what this run was asked to do, what it cost, and the
 * live transcript of what it did.
 *
 * Event payloads are raw model output. They are rendered with textContent
 * only — never innerHTML.
 */
import {
  getRun, getEvents, cancelRun, retryRun,
  type RunRow, type RunEvent,
} from "./api";
import { el, button, statusDot, statusPill, emptyState, setTone } from "./ui";

const MAX_RENDERED = 200;

let currentRunId: string | null = null;
let lastSeq = 0;
let generation = 0;
let loadingTranscript = false;

// Serializes gap backfills so overlapping fetches can never both append.
let backfillInFlight = false;
let backfillPending = false;

// Live-updated references into the currently open pane, so a status change
// can be applied in place without rebuilding the transcript.
let actionButton: HTMLButtonElement | null = null;
let actionHandler: (() => void) | null = null;
let statusSlot: HTMLElement | null = null;
let dotSlot: HTMLElement | null = null;
let elapsedDd: HTMLElement | null = null;
let costDd: HTMLElement | null = null;

function pane(): HTMLElement {
  return document.getElementById("detail")!;
}

function isActive(run: RunRow): boolean {
  return run.status === "running" || run.status === "queued";
}

function elapsedText(run: RunRow): string {
  if (!run.started_at) return "—";
  return `${Math.round((run.ended_at ?? Date.now() / 1000) - run.started_at)}s`;
}

/** The CLI's cost figure, labelled for what it is. Nothing was billed: the
 * run went through the user's Claude subscription, and claude_env scrubs
 * every ANTHROPIC_* variable so it cannot do otherwise. It is kept as a
 * comparable measure of size, never as money spent. */
function costText(run: RunRow): string {
  return run.cost_usd > 0 ? `$${run.cost_usd.toFixed(4)} if billed` : "—";
}

function bindAction(btn: HTMLButtonElement, run: RunRow): void {
  if (actionHandler) btn.removeEventListener("click", actionHandler);
  if (isActive(run)) {
    btn.textContent = "Cancel";
    setTone(btn, "bad");
  } else {
    btn.textContent = "Retry";
    setTone(btn, "accent");
  }
  const opened = generation;
  actionHandler = async () => {
    if (btn.disabled) return;
    btn.disabled = true;
    const message = document.getElementById("run-action-message");
    if (message) message.textContent = "";
    try {
      if (isActive(run)) {
        await cancelRun(run.id);
        if (opened !== generation) return;
        if (message) message.textContent = "Cancellation requested.";
        const fresh = await getRun(run.id);
        if (opened === generation) notifyRunChanged(fresh);
      } else {
        const id = await retryRun(run.id);
        if (opened === generation) await openDetail(id);
      }
    } catch {
      if (opened === generation && message) {
        message.textContent = `Could not ${isActive(run) ? "cancel" : "retry"} this run. Check the connection and try again.`;
      }
    } finally {
      if (opened === generation) btn.disabled = false;
    }
  };
  btn.addEventListener("click", actionHandler);
}

/** Called by main.ts whenever a run's row changes; only acts if that run's
 * detail pane is currently open, and only patches the bits that go stale
 * (status glyph, action button, elapsed, cost) without touching the
 * transcript. */
export function notifyRunChanged(run: RunRow): void {
  if (run.id !== currentRunId) return;
  if (actionButton) bindAction(actionButton, run);
  if (statusSlot) {
    const next = statusPill(run.status);
    statusSlot.replaceWith(next);
    statusSlot = next;
  }
  if (dotSlot) {
    const next = statusDot(run.status);
    dotSlot.replaceWith(next);
    dotSlot = next;
  }
  if (elapsedDd) elapsedDd.textContent = elapsedText(run);
  if (costDd) costDd.textContent = costText(run);
}

export function closeDetail(): void {
  generation++;
  loadingTranscript = false;
  currentRunId = null;
  lastSeq = 0;
  backfillInFlight = false;
  backfillPending = false;
  actionButton = null;
  actionHandler = null;
  statusSlot = null;
  dotSlot = null;
  elapsedDd = null;
  costDd = null;
  pane().hidden = true;
  pane().replaceChildren();
}

function eventLine(ev: RunEvent): HTMLElement {
  const line = el("div", `ev ${ev.kind}`);
  let text = ev.kind;
  try {
    const parsed = JSON.parse(ev.payload);
    if (parsed.type === "assistant") {
      const blocks = parsed.message?.content ?? [];
      for (const b of blocks) {
        if (b.type === "text" && b.text) { text = b.text; break; }
        if (b.type === "tool_use") { text = `${b.name}: ${b.input?.file_path ?? ""}`; break; }
      }
    } else if (parsed.type === "result") {
      text = `result: ${parsed.result ?? ""}`;
    }
  } catch { /* keep the kind as the label */ }
  line.textContent = text.slice(0, 400);
  return line;
}

/** Fetch events after the current lastSeq and append any not yet rendered.
 * At most one of these runs at a time (backfillInFlight); a gap that shows
 * up mid-fetch just sets backfillPending so we take one more pass once the
 * in-flight fetch settles, instead of racing a second fetch against it. */
function backfill(runId: string): void {
  if (backfillInFlight || loadingTranscript) {
    backfillPending = true;
    return;
  }
  backfillInFlight = true;
  const opened = generation;
  getEvents(runId, lastSeq)
    .then(({ events }) => {
      if (opened !== generation) return;
      const transcript = document.getElementById("transcript");
      if (!transcript) return;
      const before = lastSeq;
      if (events.length) transcript.querySelectorAll(".empty").forEach((e) => e.remove());
      for (const ev of events) {
        if (ev.seq <= lastSeq) continue; // already rendered — never duplicate
        transcript.append(eventLine(ev));
        lastSeq = Math.max(lastSeq, ev.seq);
      }
      trimAndScroll(transcript);
      if (events.length === MAX_RENDERED && lastSeq > before) backfillPending = true;
    })
    .catch((e) => {
      console.error("[detail] backfill failed", e);
    })
    .finally(() => {
      if (opened !== generation) return;
      backfillInFlight = false;
      if (backfillPending) {
        backfillPending = false;
        if (runId === currentRunId) backfill(runId);
      }
    });
}

/** Recover an open transcript even if the final socket event was lost. */
export function refreshTranscript(): void {
  if (currentRunId) backfill(currentRunId);
}

export function appendEvent(
  runId: string, seq: number, kind: string, payload: unknown,
): void {
  if (runId !== currentRunId) return;
  if (loadingTranscript) { backfillPending = true; return; }
  if (seq <= lastSeq) return;
  const transcript = document.getElementById("transcript");
  if (!transcript) return;

  if (seq > lastSeq + 1) {
    // Missed frames — backfill rather than render a hole.
    backfill(runId);
    return;
  }

  transcript.querySelectorAll(".empty").forEach((e) => e.remove());
  transcript.append(eventLine({
    id: 0, run_id: runId, seq, ts: 0, kind,
    payload: JSON.stringify(payload),
  }));
  lastSeq = Math.max(lastSeq, seq);
  trimAndScroll(transcript);
}

function trimAndScroll(transcript: HTMLElement) {
  // Trim oldest lines only — never the "load earlier" button.
  while (transcript.childElementCount > MAX_RENDERED) {
    const oldest = Array.from(transcript.children)
      .find((c) => c.classList.contains("ev"));
    if (!oldest) break;
    oldest.remove();
  }
  const pinned =
    transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 40;
  if (pinned) transcript.scrollTop = transcript.scrollHeight;
}

function field(term: string, value: string): [HTMLElement, HTMLElement] {
  return [el("dt", undefined, term), el("dd", undefined, value)];
}

export async function openDetail(runId: string): Promise<void> {
  const opened = ++generation;
  loadingTranscript = true;
  currentRunId = runId;
  lastSeq = 0;
  backfillInFlight = false;
  backfillPending = false;
  actionButton = null;
  actionHandler = null;
  statusSlot = null;
  dotSlot = null;
  elapsedDd = null;
  costDd = null;

  const host = pane();
  host.hidden = false;
  host.replaceChildren();

  let run: RunRow;
  try {
    run = await getRun(runId);
  } catch {
    if (opened !== generation) return;
    loadingTranscript = false;
    host.append(emptyState("Could not load this run."));
    return;
  }

  // openDetail is async — bail if the user switched (or closed) before the
  // getRun above resolved, so a stale response can't repopulate the pane.
  if (opened !== generation) return;

  const head = el("header", "pane-head");
  dotSlot = statusDot(run.status);
  statusSlot = statusPill(run.status);
  const close = button("Close", closeDetail, { quiet: true });
  close.classList.add("pane-close");
  head.append(dotSlot, el("h2", "pane-title", run.project_name), statusSlot, close);

  const body = el("div", "pane-body");

  const [elapsedDt, elapsedDdEl] = field("elapsed", elapsedText(run));
  const [costDt, costDdEl] = field("api equivalent", costText(run));
  elapsedDd = elapsedDdEl;
  costDd = costDdEl;

  const dl = el("dl", "kv");
  dl.append(
    elapsedDt, elapsedDdEl,
    costDt, costDdEl,
    ...field("tokens", `in ${run.input_tokens} / out ${run.output_tokens} / cache read ${run.cache_read_tokens} / cache write ${run.cache_creation_tokens}`),
    ...field("model", run.model || "—"),
    ...field("session", run.id),
  );
  if (run.error) dl.append(...field("error", run.error));

  const action = document.createElement("button");
  action.type = "button";
  bindAction(action, run);
  actionButton = action;
  const actions = el("div", "pane-actions");
  const actionMessage = el("span");
  actionMessage.id = "run-action-message";
  actionMessage.setAttribute("role", "status");
  actions.append(action, actionMessage);

  const transcript = el("div", "transcript");
  transcript.id = "transcript";

  body.append(el("p", "pane-prompt", run.prompt), dl, actions, transcript);
  host.append(head, body);

  // Load the TAIL, not the head — on a long build the last 200 events are the
  // ones you care about.
  //
  // GUARDED, because this pair of fetches happens AFTER the pane is on
  // screen: an unguarded 500 left an empty `.transcript` div, which is
  // pixel-for-pixel a run that recorded nothing, plus an unhandled
  // rejection in the console and the "No events recorded" branch below
  // never reached. A transcript we could not read has to say so.
  let total: number;
  let events: RunEvent[];
  try {
    ({ total } = await getEvents(runId, 0, 1));
    if (opened !== generation) return;
    const from = Math.max(0, total - MAX_RENDERED);
    ({ events } = await getEvents(runId, from, MAX_RENDERED));
    if (opened !== generation) return;
    if (from > 0) transcript.append(loadEarlierButton(runId, from));
  } catch (e) {
    console.error("[detail] transcript fetch failed", e);
    if (opened !== generation) return;
    loadingTranscript = false;
    transcript.replaceChildren(
      emptyState("Could not load this run's transcript."));
    return;
  }

  for (const ev of events) transcript.append(eventLine(ev));
  lastSeq = events.length ? events[events.length - 1].seq : 0;
  loadingTranscript = false;
  transcript.scrollTop = transcript.scrollHeight;

  if (total === 0) {
    transcript.append(emptyState(
      isActive(run)
        ? "Waiting for the first event…"
        : "No events recorded for this run.",
      true,
    ));
  }
  if (backfillPending) {
    backfillPending = false;
    backfill(runId);
  }
}

function loadEarlierButton(runId: string, before: number): HTMLElement {
  const opened = generation;
  const idleLabel = `Load earlier (${before} more)`;
  const btn = button(idleLabel, () => {
    // Disable before anything async so a double-click can't fire this twice.
    btn.disabled = true;
    btn.textContent = "Loading…";
    const from = Math.max(0, before - MAX_RENDERED);
    getEvents(runId, from, before - from)
      .then(({ events }) => {
        if (opened !== generation) return;
        const transcript = document.getElementById("transcript");
        if (!transcript) return;
        const anchor = btn.nextSibling;
        for (const ev of events) transcript.insertBefore(eventLine(ev), anchor);
        btn.remove();
        if (from > 0) {
          transcript.insertBefore(loadEarlierButton(runId, from),
                                  transcript.firstChild);
        }
      })
      .catch((e) => {
        console.error("[detail] load earlier failed", e);
        btn.disabled = false;
        btn.textContent = idleLabel;
      });
  }, { quiet: true });
  return btn;
}
