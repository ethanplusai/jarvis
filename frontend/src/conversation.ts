import type { JarvisSocket } from "./ws";
import { PANEL_KEY } from "./panelstate";
import { makeWindow } from "./panelwindow";
import { armed, timeElement } from "./uibits";

/** `created_at` is epoch seconds: the server's for anything it recorded,
 * the browser's for an unsent draft and for his live text reply. */
interface Message {
  id: string; seq?: number; role: string; text: string; status: string; source?: string;
  created_at?: number;
}
const KEY = "jarvis-unsent-messages-v1";
/** His reply while it is still arriving, before its record exists. */
const LIVE = "live-reply";

/** Durable server history plus a local outbox; never automatically replay commands. */
export function createConversation(socket: JarvisSocket) {
  const messages = new Map<string, Message>();
  const outbox = new Map<string, Message>();
  let oldest: number | undefined;
  let loading = false;
  let historyLoaded = false;
  const panel = document.createElement("section");
  panel.id = "conversation";
  panel.setAttribute("aria-label", "Conversation");
  const heading = document.createElement("h2");
  heading.textContent = "Conversation";
  const connection = document.createElement("p");
  connection.id = "voice-connection";
  connection.setAttribute("role", "status");
  const older = document.createElement("button");
  older.textContent = "Load earlier messages";
  older.type = "button";
  const log = document.createElement("div");
  log.id = "conversation-log";
  log.setAttribute("role", "log");
  log.setAttribute("aria-live", "polite");
  const form = document.createElement("form");
  form.id = "composer";
  const label = document.createElement("label");
  label.htmlFor = "message-input";
  label.textContent = "Message JARVIS";
  const input = document.createElement("textarea");
  input.id = "message-input";
  input.maxLength = 16000;
  input.rows = 2;
  input.placeholder = "Type a message, or speak…";
  const send = document.createElement("button");
  send.type = "submit";
  send.textContent = "Send";
  form.append(label, input, send);

  // The header is the handle: it drags the panel, it holds the one button
  // that minimises it, and it is what a keyboard user focuses to do either.
  const head = document.createElement("header");
  head.id = "conversation-head";
  head.tabIndex = 0;
  head.setAttribute("aria-label",
    "Conversation panel. Drag to move it; arrow keys move it; Enter minimises or expands it.");
  const toggle = document.createElement("button");
  toggle.id = "conversation-toggle";
  toggle.type = "button";
  toggle.setAttribute("aria-controls", "conversation-body");
  head.append(heading, toggle);
  const body = document.createElement("div");
  body.id = "conversation-body";
  body.append(connection, older, log, form);
  panel.append(head, body);
  document.body.append(panel);

  // Where it sits and whether it is open: the shared window behaviour
  // (panelwindow.ts) — drag by the header, minimise to it, arrow keys,
  // remembered per browser under this panel's own key.
  makeWindow({ panel, head, body, toggle, key: PANEL_KEY });

  function save() {
    try { localStorage.setItem(KEY, JSON.stringify([...outbox.values()])); }
    catch { connection.textContent = "Browser storage is unavailable. Keep this page open to retain unsent messages."; }
  }
  function paint() {
    const bottom = log.scrollHeight - log.scrollTop - log.clientHeight < 50;
    log.replaceChildren();
    for (const m of messages.values()) {
      const item = document.createElement("article");
      item.className = `message message-${m.role}`;
      const title = document.createElement("strong");
      title.textContent = m.role === "user" ? "You" : "JARVIS";
      const text = document.createElement("p");
      text.textContent = m.text;
      // When, to the second, then the delivery status. The time is a
      // <time> with the machine-readable instant and the full date on hover.
      const meta = document.createElement("div");
      meta.className = "message-meta";
      const when = timeElement(m.created_at);
      if (when) meta.append(when);
      const status = document.createElement("small");
      status.textContent = m.status === "accepted" ? "Received" : m.status;
      if (when && status.textContent) {
        const dot = document.createElement("span");
        dot.className = "dot";
        dot.textContent = "·";
        meta.append(dot);
      }
      meta.append(status);
      if (m.id !== LIVE) deleteButton(meta, m);
      item.append(title, text, meta);
      if (outbox.has(m.id)) {
        const retry = document.createElement("button");
        retry.type = "button";
        retry.textContent = "Send / check receipt";
        retry.disabled = !socket.isConnected();
        retry.addEventListener("click", () => transmit(m));
        item.append(retry);
      }
      log.append(item);
    }
    if (bottom) log.scrollTop = log.scrollHeight;
  }
  /** Delete one message: two clicks (arm, then act), so a stray click cannot
   * take a line out of the record. An unsent draft never reached the server
   * and is dropped locally; a saved message is a DELETE — soft on the server
   * side, so a retried draft with the same id is still a duplicate. */
  function deleteButton(parent: HTMLElement, m: Message): HTMLButtonElement {
    return armed(parent, "Delete", "Confirm", async () => {
      if (outbox.has(m.id)) {                       // never reached the server
        outbox.delete(m.id); messages.delete(m.id); save(); paint();
        return;
      }
      const res = await fetch(`/api/conversation/${encodeURIComponent(m.id)}`, { method: "DELETE" });
      if (!res.ok && res.status !== 404) throw new Error(`Could not delete (${res.status})`);
      messages.delete(m.id); paint();
    }, connection, { className: "message-delete", title: "Delete this message (asks once more)", busyText: null });
  }
  function transmit(message: Message) {
    const sent = socket.send({ type: "transcript", id: message.id,
      text: message.text, isFinal: true, source: message.source });
    message.status = sent ? "Awaiting receipt" : "Not sent — disconnected";
    outbox.set(message.id, message);
    messages.set(message.id, message);
    save(); paint();
  }
  function submit(text: string, source = "voice") {
    text = text.trim();
    if (!text) return;
    transmit({ id: crypto.randomUUID(), role: "user", text, status: "Not sent", source,
               created_at: Date.now() / 1000 });
  }
  async function refresh(before?: number) {
    if (loading) return;
    loading = true;
    try {
      const response = await fetch(`/api/conversation?limit=100${before ? `&before=${before}` : ""}`);
      if (!response.ok) throw new Error("Conversation history unavailable");
      const body = await response.json() as { messages: Message[] };
      const combined = new Map([...messages]);
      for (const message of body.messages) {
        combined.set(message.id, message);
        outbox.delete(message.id);
      }
      messages.clear();
      [...combined.values()].sort((a, b) => (a.seq ?? Number.MAX_SAFE_INTEGER) - (b.seq ?? Number.MAX_SAFE_INTEGER))
        .forEach(m => messages.set(m.id, m));
      oldest = Math.min(oldest ?? Infinity, ...body.messages.map(m => m.seq ?? Infinity));
      if (before || !historyLoaded) older.hidden = body.messages.length < 100;
      historyLoaded = true;
      settleReply();
      save(); paint();
    } catch {
      connection.textContent = "Conversation history unavailable. Unsent messages are retained here.";
    } finally { loading = false; }
  }
  try {
    const saved: unknown = JSON.parse(localStorage.getItem(KEY) || "[]");
    if (Array.isArray(saved)) for (const item of saved) {
      if (item && typeof item.id === "string" && typeof item.text === "string" && item.text.length <= 16000) {
        const m: Message = { ...item, role: "user", status: "Receipt unknown — check before sending again" };
        outbox.set(m.id, m); messages.set(m.id, m);
      }
    }
  } catch { /* Corrupt local drafts must not prevent typed input. */ }
  // His reply, live. While his voice is off the server sends each sentence
  // as a `text` frame the moment it is ready; the durable record only exists
  // once the turn ends, and history is polled every ten seconds. A text
  // conversation cannot wait that long, so the reply is shown at once as
  // one provisional message (no `seq`, so it sorts last) and is replaced by
  // the record when the turn ends and the record has actually arrived — a
  // failed history fetch must not make his words vanish.
  let liveSince = 0;                 // the newest durable seq when the live reply began
  function showReply(text: string) {
    if (!messages.has(LIVE)) {
      liveSince = Math.max(0, ...[...messages.values()].map(m => m.seq ?? 0));
    }
    const live = messages.get(LIVE)
      ?? { id: LIVE, role: "assistant", text: "", status: "", created_at: Date.now() / 1000 };
    live.text = live.text ? `${live.text} ${text}` : text;
    messages.set(LIVE, live);
    paint();
  }
  /** Called by every `refresh()`: once a durable reply newer than the live
   * one has arrived, the live one has done its job. Measured live: the
   * record lands AFTER the turn's `idle`, so a single check at `endReply`
   * left the reply on screen twice. */
  function settleReply() {
    if (!messages.has(LIVE)) return;
    const settled = [...messages.values()].some(
      m => m.id !== LIVE && m.role !== "user" && (m.seq ?? 0) > liveSince);
    if (settled) messages.delete(LIVE);
  }
  async function endReply() {
    if (!messages.has(LIVE)) return;
    await refresh();
    // The record may still be on its way; look once more shortly, and the
    // regular poll covers anything slower than that.
    if (messages.has(LIVE)) setTimeout(() => { void refresh(); }, 1500);
  }
  form.addEventListener("submit", event => {
    event.preventDefault(); submit(input.value, "typed"); input.value = "";
  });
  older.addEventListener("click", () => void refresh(oldest));
  socket.onState(connected => {
    connection.textContent = connected ? "Connected" : "Reconnecting — messages will remain unsent until you send them";
    if (!connected) for (const m of outbox.values()) {
      if (m.status === "Awaiting receipt") m.status = "Receipt unknown — check before sending again";
    }
    paint();
    if (connected) void refresh();
  });
  socket.onMessage(message => {
    if (message.type === "receipt") {
      const m = messages.get(String(message.id));
      if (m) {
        m.status = String(message.status);
        if (!["unavailable", "rejected"].includes(m.status)) outbox.delete(m.id);
        save(); paint();
      }
    }
    if (message.transcript_id) void refresh();
  });
  void refresh();
  setInterval(() => { if (socket.isConnected()) void refresh(); }, 10000);
  return { submit, showReply, endReply };
}
