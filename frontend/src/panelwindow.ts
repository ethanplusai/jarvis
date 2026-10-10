// A panel as a window: drag it by its header, minimise it to that header,
// move it by arrow keys, and remember where it was left. ONE module for the
// Conversation panel and the Business desk — the decisions (clamping,
// formatting, remembering) are pure in panelstate.ts; this is the DOM half.
//
// Nothing here uses innerHTML: the header, body and toggle are the caller's
// own elements, and this only toggles attributes and classes on them.

import {
  clampPosition, loadPanelState, savePanelState, nudge, type StorageLike,
} from "./panelstate";

export interface WindowParts {
  /** The element that moves: `position: fixed` (or a dialog opened with `show()`). */
  panel: HTMLElement;
  /** The drag handle. Made focusable so a keyboard user can nudge and toggle. */
  head: HTMLElement;
  /** Everything that hides when minimised. */
  body: HTMLElement;
  /** The Minimise / Expand button; lives inside `head`. */
  toggle: HTMLButtonElement;
  /** The localStorage slot — one per panel. */
  key: string;
}

export interface PanelWindow {
  setMinimized(minimized: boolean): void;
  isMinimized(): boolean;
}

function storage(): StorageLike | null {
  try { return localStorage; } catch { return null; }
}

export function makeWindow({ panel, head, body, toggle, key }: WindowParts): PanelWindow {
  // Remembered per browser. Until the user moves it, the stylesheet's
  // position stands — the stylesheet is what knows about narrow screens.
  const state = loadPanelState(storage(), key);
  function persist() { savePanelState(storage(), state, key); }
  function positioned(): boolean { return state.left !== null && state.top !== null; }
  function place(left: number, top: number) {
    const rect = panel.getBoundingClientRect();
    const at = clampPosition(left, top, { width: rect.width, height: rect.height },
                             { width: window.innerWidth, height: window.innerHeight });
    state.left = at.left; state.top = at.top;
    panel.style.left = `${at.left}px`; panel.style.top = `${at.top}px`;
    panel.style.right = "auto"; panel.style.bottom = "auto";
  }
  function paintMinimized() {
    panel.classList.toggle("minimized", state.minimized);
    body.hidden = state.minimized;
    toggle.textContent = state.minimized ? "Expand" : "Minimise";
    toggle.title = state.minimized ? "Show the panel" : "Collapse to the title bar";
    toggle.setAttribute("aria-expanded", String(!state.minimized));
  }
  function setMinimized(minimized: boolean) {
    state.minimized = minimized;
    paintMinimized();
    // Expanding at the bottom edge would overflow it: re-clamp to the new size.
    if (positioned()) place(state.left as number, state.top as number);
    persist();
  }

  if (!head.hasAttribute("tabindex")) head.tabIndex = 0;
  if (!toggle.getAttribute("aria-controls") && body.id) toggle.setAttribute("aria-controls", body.id);
  paintMinimized();
  if (positioned()) place(state.left as number, state.top as number);
  window.addEventListener("resize", () => {
    if (positioned()) { place(state.left as number, state.top as number); persist(); }
  });
  toggle.addEventListener("click", () => setMinimized(!state.minimized));
  head.addEventListener("keydown", (event: KeyboardEvent) => {
    if (event.target !== head) return;                 // buttons handle their own keys
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      setMinimized(!state.minimized);
      return;
    }
    const step = nudge(event.key);
    if (!step) return;
    event.preventDefault();
    const rect = panel.getBoundingClientRect();
    place(rect.left + step.dx, rect.top + step.dy);
    persist();
  });

  // Dragging: pointer capture, so a fast drag that leaves the header still
  // follows the pointer, and every kind of pointer — mouse, pen, finger.
  let drag: { pointerId: number; dx: number; dy: number } | null = null;
  head.addEventListener("pointerdown", (event: PointerEvent) => {
    if ((event.target as HTMLElement).closest("button, a, input, select, textarea")) return; // a click, not a drag
    const rect = panel.getBoundingClientRect();
    drag = { pointerId: event.pointerId, dx: event.clientX - rect.left, dy: event.clientY - rect.top };
    head.setPointerCapture(event.pointerId);
    panel.classList.add("dragging");
    event.preventDefault();
  });
  head.addEventListener("pointermove", (event: PointerEvent) => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    place(event.clientX - drag.dx, event.clientY - drag.dy);
  });
  function endDrag(event: PointerEvent) {
    if (!drag || event.pointerId !== drag.pointerId) return;
    drag = null;
    panel.classList.remove("dragging");
    try { head.releasePointerCapture(event.pointerId); } catch { /* already released */ }
    persist();
  }
  head.addEventListener("pointerup", endDrag);
  head.addEventListener("pointercancel", endDrag);

  return { setMinimized, isMinimized: () => state.minimized };
}
