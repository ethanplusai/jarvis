// The small DOM pieces the voice page's panels share — the Conversation
// panel and the Business desk had each grown their own copy of every one of
// these. One copy, so a fix lands in both.
//
// Nothing here uses innerHTML: every node is built with createElement and
// textContent, because both panels render text other people wrote.

import { formatStamp, stampTitle } from "./panelstate";

/** An element with its text set. */
export function el<K extends keyof HTMLElementTagNameMap>(tag: K, text = ""): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  node.textContent = text;
  return node;
}

/** Somewhere to say what happened: an element's text, or a function. */
export type StatusSink = { textContent: string | null } | ((message: string) => void);

function report(sink: StatusSink | undefined, message: string): void {
  if (!sink) return;
  if (typeof sink === "function") sink(message);
  else sink.textContent = message;
}

/**
 * A button that has to be pressed twice: the first click arms it (the label
 * changes and lapses after a few seconds), the second acts. For removing
 * records — approvals, messages — where a stray click must not be enough.
 */
export function armed(
  parent: HTMLElement, label: string, confirmLabel: string,
  action: () => Promise<void>, status?: StatusSink,
  opts: { className?: string; title?: string; busyText?: string | null } = {},
): HTMLButtonElement {
  const node = el("button", label);
  node.type = "button";
  if (opts.className) node.className = opts.className;
  if (opts.title) node.title = opts.title;
  let timer: number | undefined;
  const disarm = () => { node.textContent = label; node.classList.remove("armed"); };
  node.addEventListener("click", async () => {
    if (node.textContent !== confirmLabel) {
      node.textContent = confirmLabel;
      node.classList.add("armed");
      window.clearTimeout(timer);
      timer = window.setTimeout(disarm, 6000);
      return;
    }
    window.clearTimeout(timer);
    node.disabled = true;
    if (opts.busyText !== null) report(status, opts.busyText ?? "Working…");
    try {
      await action();
    } catch (error) {
      report(status, String(error));
      node.disabled = false;
      disarm();
    }
  });
  parent.append(node);
  return node;
}

/** `<time datetime=… title=…>HH:MM:SS</time>` for an epoch-seconds instant,
 * or null when there is no honest time to show (never "1970"). */
export function timeElement(epochSec: number | undefined): HTMLTimeElement | null {
  const text = formatStamp(epochSec ?? Number.NaN);
  if (!text) return null;
  const when = el("time", text);
  when.dateTime = new Date((epochSec as number) * 1000).toISOString();
  when.title = stampTitle(epochSec as number);
  return when;
}
