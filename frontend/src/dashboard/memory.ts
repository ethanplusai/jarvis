/**
 * The Memory view: what JARVIS carries into every conversation, and a way
 * to read (and know you can edit) the plain-Markdown folder behind it.
 *
 * Backed by `GET /api/memory` and `GET /api/memory/<kind>/<slug>` (and
 * `POST /api/memory/reindex`, the one write). An empty memory is a 200 with
 * empty lists; a 404 means the route is not wired at all — an older server
 * — and degrades calmly with no console noise.
 *
 * Every string here — titles, hooks, and especially journal/memory body
 * text — can contain content JARVIS copied out of someone else's Claude
 * Code session. Attacker-influenced, same discipline as Sessions: always
 * textContent, never innerHTML.
 */
import {
  getMemory, getMemoryDoc, reindexMemory, ApiError,
  type MemorySnapshot, type MemoryKind,
  type MemoryIndexEntry, type MemoryFileEntry, type ProjectNoteEntry, type JournalEntry,
} from "./api";
import { el, row, button, callout, emptyState } from "./ui";

let started = false;
let openDocToken = 0;

function fmtWhen(epochSec: number): string {
  if (!epochSec) return "—";
  return new Date(epochSec * 1000).toLocaleString([], {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

function section(id: string): HTMLElement | null {
  return document.getElementById(id);
}

function setMeta(id: string, count: number): void {
  const meta = section(id);
  if (meta) meta.textContent = count > 0 ? String(count) : "";
}

function showBanner(text: string | null): void {
  const banner = section("memory-banner");
  if (!banner) return;
  if (text === null) {
    banner.hidden = true;
    return;
  }
  banner.hidden = false;
  banner.textContent = text;
}

function setUnavailable(unavailable: boolean): void {
  const notice = section("memory-unavailable");
  const body = section("memory-body");
  if (notice) notice.hidden = !unavailable;
  if (body) body.hidden = unavailable;
}

/** A single clickable row: title, a muted subtitle, and a "when" on the
 * right — the shape shared by memories, projects and journal entries. */
function docRow(
  titleText: string, subText: string, whenText: string,
  onOpen: () => void, highlight = false,
): HTMLElement {
  const r = row({
    onOpen,
    label: titleText,
    tone: highlight ? "accent" : undefined,
  });
  r.setTitle(titleText);
  if (subText) r.setSub(subText);
  r.addMeta(whenText, { cls: "memory-row-when" });
  return r.root;
}

function openDoc(kind: MemoryKind, slug: string, titleText: string): void {
  const pane = section("memory-detail");
  if (!pane) return;
  const token = ++openDocToken;

  pane.hidden = false;
  pane.replaceChildren();

  const head = el("header", "pane-head");
  const close = button("Close", closeDoc, { quiet: true });
  close.classList.add("pane-close");
  head.append(el("h2", "pane-title", titleText), close);

  const body = el("div", "pane-body");
  const doc = el("pre", "doc", "Loading…");
  body.append(el("div", "pane-meta", `${kind} · ${slug}`), doc);

  pane.append(head, body);

  getMemoryDoc(kind, slug)
    .then((loaded) => {
      if (token !== openDocToken) return; // superseded by another open/close
      doc.textContent = loaded.text;
    })
    .catch((e) => {
      if (token !== openDocToken) return;
      if (!(e instanceof ApiError && e.status === 404)) {
        console.error("[memory] doc fetch failed", e);
      }
      doc.textContent = "Could not load this file. It may have been moved or deleted on disk.";
    });
}

function closeDoc(): void {
  openDocToken++;
  const pane = section("memory-detail");
  if (!pane) return;
  pane.hidden = true;
  pane.replaceChildren();
}

function paintPath(path: string): void {
  const holder = section("memory-path");
  if (!holder) return;
  holder.replaceChildren();

  const code = el("code", "memory-path-value", path);
  const copy = button("Copy path", () => {
    navigator.clipboard?.writeText(path).then(
      () => { copy.textContent = "Copied"; setTimeout(() => { copy.textContent = "Copy path"; }, 1500); },
      () => { copy.textContent = "Could not copy"; setTimeout(() => { copy.textContent = "Copy path"; }, 1500); },
    );
  });

  const pathRow = el("div", "memory-path-row");
  pathRow.append(code, copy);

  holder.append(
    el("div", "memory-hint", "Plain Markdown on disk. Edit it directly — JARVIS reads it fresh every conversation."),
    pathRow,
  );
}

/**
 * The index and the folder can disagree, and until this existed the page
 * stated the fact twice ("Nothing indexed yet." beside four memory files)
 * and the problem nowhere. The brain cannot see an unindexed note at boot,
 * so this names them and offers the one repair. Never automatic: a note
 * without a line may be one the user deliberately let go of.
 */
function unindexedNotice(orphans: MemoryFileEntry[]): HTMLElement {
  const names = orphans.map((o) => o.title).join(", ");
  const note = callout({
    tone: "warn",
    label: `${orphans.length} memory ${orphans.length === 1 ? "file is" : "files are"} not in the index`,
  });
  note.body.textContent =
    `JARVIS does not know ${orphans.length === 1 ? "it exists" : "they exist"} until ${orphans.length === 1 ? "it is" : "they are"} listed here: ${names}.`;
  const add = button("Add them to the index", () => {
    add.disabled = true;
    add.textContent = "Adding…";
    reindexMemory()
      .then((result) => {
        if (result.left_out.length > 0) {
          note.body.textContent = result.full
            ? `The index is full — eighty is all that fits in every conversation. Left out: ${result.left_out.join(", ")}.`
            : `Could not index (the index cannot name the file by its title): ${result.left_out.join(", ")}.`;
        }
        void reconcile();
      })
      .catch((e) => {
        console.error("[memory] reindex failed", e);
        add.disabled = false;
        add.textContent = "Add them to the index";
        note.body.textContent = "Could not update the index. Is the JARVIS server reachable?";
      });
  });
  note.foot.hidden = false;
  note.foot.append(add);
  return note.root;
}

function paintIndex(entries: MemoryIndexEntry[], orphans: MemoryFileEntry[]): void {
  const list = section("memory-index-list");
  if (!list) return;
  list.replaceChildren();
  setMeta("memory-index-meta", entries.length);
  if (orphans.length > 0) list.append(unindexedNotice(orphans));
  if (entries.length === 0) {
    list.append(emptyState("Nothing indexed yet.", true));
    return;
  }
  for (const entry of entries) {
    list.append(docRow(
      entry.title, entry.hook, "",
      () => openDoc("memory", entry.slug, entry.title),
    ));
  }
}

function paintMemories(entries: MemoryFileEntry[]): void {
  const list = section("memory-files-list");
  if (!list) return;
  list.replaceChildren();
  setMeta("memory-files-meta", entries.length);
  if (entries.length === 0) {
    list.append(emptyState("No memory files yet.", true));
    return;
  }
  const sorted = [...entries].sort((a, b) => b.modified - a.modified);
  for (const m of sorted) {
    list.append(docRow(
      m.title, m.slug, fmtWhen(m.modified),
      () => openDoc("memory", m.slug, m.title),
    ));
  }
}

function paintProjects(entries: ProjectNoteEntry[]): void {
  const list = section("memory-projects-list");
  if (!list) return;
  list.replaceChildren();
  setMeta("memory-projects-meta", entries.length);
  if (entries.length === 0) {
    list.append(emptyState("No project notes yet.", true));
    return;
  }
  const sorted = [...entries].sort((a, b) => b.modified - a.modified);
  for (const p of sorted) {
    list.append(docRow(
      p.title, p.slug, fmtWhen(p.modified),
      () => openDoc("project", p.slug, p.title),
    ));
  }
}

function paintJournalList(entries: JournalEntry[], latestSlug: string | null): void {
  const list = section("memory-journal-list");
  if (!list) return;
  list.replaceChildren();
  setMeta("memory-journal-meta", entries.length);
  if (entries.length === 0) {
    list.append(emptyState("No journal entries yet.", true));
    return;
  }
  const sorted = [...entries].sort((a, b) => b.when - a.when);
  for (const j of sorted) {
    const isLatest = j.slug === latestSlug;
    list.append(docRow(
      j.reason || "(handover)", j.slug, fmtWhen(j.when),
      () => openDoc("journal", j.slug, j.reason || j.slug),
      isLatest,
    ));
  }
}

async function paintLatestJournal(latestSlug: string | null): Promise<void> {
  const wrap = section("journal-latest-section");
  const body = section("journal-latest-body");
  const meta = section("journal-latest-meta");
  if (!wrap || !body) return;

  if (!latestSlug) {
    wrap.hidden = true;
    return;
  }
  wrap.hidden = false;
  if (meta) meta.textContent = latestSlug;
  body.textContent = "Loading…";
  try {
    const doc = await getMemoryDoc("journal", latestSlug);
    body.textContent = doc.text;
  } catch (e) {
    if (!(e instanceof ApiError && e.status === 404)) {
      console.error("[memory] latest journal fetch failed", e);
    }
    body.textContent = "Could not load the latest journal entry.";
  }
}

async function reconcile(): Promise<void> {
  let snap: MemorySnapshot;
  try {
    snap = await getMemory();
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) {
      // Expected until the backend endpoint ships — a calm empty state,
      // not an error. No console noise.
      setUnavailable(true);
      showBanner(null);
      return;
    }
    console.error("[memory] reconcile failed", e);
    showBanner("Cannot reach the JARVIS server.");
    return;
  }

  setUnavailable(false);
  showBanner(null);
  paintPath(snap.path);
  paintIndex(snap.index ?? [], snap.unindexed ?? []);
  paintMemories(snap.memories);
  paintProjects(snap.projects);
  paintJournalList(snap.journal, snap.latest_journal_slug);
  void paintLatestJournal(snap.latest_journal_slug);
}

export function initMemory(): void {
  if (started) return;
  started = true;
  void reconcile();
}

/** Re-fetch on demand — e.g. when the user switches back to this tab,
 * since the folder is user-edited on disk and there is no live socket
 * pushing changes the way runs/sessions get. */
export function refreshMemory(): void {
  void reconcile();
}
