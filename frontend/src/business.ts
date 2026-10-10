import "./business.css";
import { formatStamp } from "./panelstate";
import { makeWindow } from "./panelwindow";
import { el, armed, timeElement } from "./uibits";

type Action = { id: string; seq: number; provider: string; operation: string; state: string; digest: string; created: number; updated: number; expires: number; payload: unknown; result: unknown; repeat_note?: string | null };
type RecordRow = { id: string; seq: number; kind: string; version: number; updated: number; body: {title: string; notes: string; status: string; due: string; amount_minor: number; currency: string; contact: string} };

const PANEL_KEY = "jarvis-business-panel-v1";

/** "Staged 14:05:09 · Updated 14:06:00 · Expires 25 Sep 14:05:09" — one
 * <time> per instant (machine-readable `datetime`, full date on hover),
 * to the second, the day too when it is not today. Absent times are left
 * out rather than shown as the epoch. */
function clockLine(parts: [string, number | undefined][]) {
  const line = el("p"); line.className = "business-meta";
  for (const [label, epoch] of parts) {
    const when = timeElement(epoch);
    if (!when) continue;
    if (line.childNodes.length) line.append(" · ");
    line.append(`${label} `, when);
  }
  return line;
}
async function api(path: string, body?: unknown, method = body === undefined ? "GET" : "POST") {
  const init: RequestInit = { method };
  if (body !== undefined) { init.headers = { "Content-Type": "application/json" }; init.body = JSON.stringify(body); }
  const response = await fetch(`/api/business/${path}`, init);
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : `Request failed (${response.status})`);
  return result;
}
/** An approval that can no longer do anything — the only kind Delete is offered for. */
function finished(item: Action) {
  return !["pending", "executing", "approved"].includes(item.state) || item.expires * 1000 <= Date.now();
}
const LEDGER_NOTE = "It stays in the audit ledger and the export.";
function field(parent: HTMLElement, name: string, value = "", type = "text") {
  const label = el("label", name); const input = el("input"); input.type = type; input.value = value;
  label.append(input); parent.append(label); return input;
}
function select(parent: HTMLElement, name: string, options: string[]) {
  const label = el("label", name); const input = el("select");
  input.setAttribute("aria-label", name);
  for (const option of options) { const node = el("option", option); node.value = option; input.append(node); }
  label.append(input); parent.append(label); return input;
}
function button(parent: HTMLElement, label: string, action: () => Promise<void>, status: HTMLElement) {
  const node = el("button", label); node.type = "button";
  node.addEventListener("click", async () => {
    node.disabled = true; status.textContent = "Working…";
    try { await action(); } catch (error) { status.textContent = String(error); }
    finally { node.disabled = false; }
  }); parent.append(node); return node;
}
function details(parent: HTMLElement, label: string, value: unknown, open = false) {
  const box = el("details"); box.open = open;
  const text = el("pre", JSON.stringify(value, null, 2)); box.append(el("summary", label), text); parent.append(box);
}
/** A request's long or multi-line text fields as the reader will see them —
 * a post with its line breaks, not a JSON string full of `\n`. The JSON
 * view below stays the exact bytes. textContent only: this is a model's
 * text, and the desk never parses it as markup. */
function readableText(parent: HTMLElement, payload: unknown) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return;
  for (const [key, value] of Object.entries(payload as Record<string, unknown>)) {
    if (typeof value !== "string" || (value.length <= 60 && !value.includes("\n"))) continue;
    const box = el("div"); box.className = "business-text";
    box.append(el("p", `${key} — exactly as it will read (${value.length} characters):`), el("pre", value));
    parent.append(box);
  }
}

type LinkedInAccount = { app_configured: boolean; connected: boolean; name: string | null; expires_at: number | null; page?: string | null };
type LinkedInStatus = {
  limits: { posts_per_day: number; comments_per_day: number; min_post_gap_hours: number };
  halted: { reason: string; at: number; source: string } | null;
  api: { member: LinkedInAccount; organization: LinkedInAccount };
};
/** LinkedIn at the top of the queue: whether it is stopped (and the owner's
 * Resume), the interim limits, and the official API's connections with a
 * Connect link each. The owner signs in on LinkedIn's own page; JARVIS never
 * sees the password. */
async function linkedinPanel(parent: HTMLElement, status: HTMLElement, reload: () => Promise<void>) {
  let info: LinkedInStatus;
  try {
    const response = await fetch("/api/linkedin/status");
    if (!response.ok) return;
    info = await response.json();
  } catch { return; }
  const box = el("section"); box.className = "business-linkedin"; box.append(el("h4", "LinkedIn"));
  if (info.halted) {
    const warn = el("p", `Stopped ${formatStamp(info.halted.at)}: LinkedIn objected ("${info.halted.reason}"). Every LinkedIn action is refused until you look at LinkedIn yourself and resume.`);
    warn.className = "business-warning"; warn.setAttribute("role", "alert"); box.append(warn);
    button(box, "Resume LinkedIn", async () => {
      const response = await fetch("/api/linkedin/resume", { method: "POST" });
      if (!response.ok) throw new Error(`Resume refused (${response.status})`);
      await reload(); status.textContent = "LinkedIn resumed.";
    }, status);
  }
  const limits = info.limits;
  box.append(el("p", `Limits: ${limits.posts_per_day} post${limits.posts_per_day === 1 ? "" : "s"} a day per account, at least ${limits.min_post_gap_hours} hours apart; ${limits.comments_per_day} comments a day.`));
  for (const [key, label] of [["member", "Your profile"], ["organization", "Company page"]] as const) {
    const account = info.api[key];
    const line = el("p");
    if (account.connected) {
      line.append(`${label}: connected through LinkedIn's API${account.name ? ` as ${account.name}` : ""}${account.expires_at ? `, until ${formatStamp(account.expires_at)}` : ""}. `);
    } else if (account.app_configured) {
      line.append(`${label}: not connected. `);
    } else {
      line.append(`${label}: no LinkedIn app yet — see docs/linkedin.md.`);
    }
    if (account.app_configured) {
      const link = el("a", account.connected ? "Reconnect" : "Connect"); link.href = `/api/linkedin/connect?account=${key}`;
      link.target = "_blank"; link.rel = "noopener"; line.append(link);
    }
    box.append(line);
  }
  parent.append(box);
}

export function openBusiness() {
  const existing = document.getElementById("business") as HTMLDialogElement | null;
  if (existing) { existing.show(); return; }
  // A window, not a modal: `show()`, so there is no backdrop and the page
  // behind stays usable — a desk minimised to its title bar under a modal
  // backdrop would be a desk nobody could get past.
  const dialog = el("dialog"); dialog.id = "business"; dialog.setAttribute("aria-labelledby", "business-title");
  const head = el("header"); head.id = "business-head";
  head.setAttribute("aria-label", "Business desk window. Drag to move it; arrow keys move it; Enter minimises or expands it.");
  const title = el("h2", "Business desk"); title.id = "business-title";
  const toggle = el("button", "Minimise"); toggle.id = "business-toggle"; toggle.type = "button";
  const close = el("button", "Close"); close.type = "button"; close.addEventListener("click", () => dialog.close());
  head.append(title, toggle, close); dialog.append(head);
  const body = el("div"); body.id = "business-body"; dialog.append(body);
  const status = el("p"); status.setAttribute("role", "status"); body.append(status);
  body.append(el("p", "Prepare work, review changes, track the result. Every provider change and phone call requires your approval."));
  const connections = el("section"); body.append(connections);
  const tabs = el("nav"); tabs.setAttribute("aria-label", "Business sections"); body.append(tabs);
  const panel = el("section"); body.append(panel);
  let generation = 0;

  async function summary() {
    const current = ++generation; panel.replaceChildren(el("h3", "Business briefing"));
    const result = await api("briefing");
    if (current !== generation) return;
    const madeAt = typeof result.generated_at === "number" ? result.generated_at : Date.now() / 1000;
    panel.append(clockLine([["As of", madeAt]]));
    for (const [label, value] of [["Open tasks", result.open_tasks], ["Overdue tasks", result.overdue_tasks], ["Active leads", result.leads]]) {
      panel.append(el("p", `${label}: ${value}`));
    }
    for (const [label, values] of [["Outstanding invoices", result.receivables_minor], ["Unpaid expenses", result.unpaid_expenses_minor]] as [string, Record<string, number>][]) {
      panel.append(el("h4", label));
      if (!Object.keys(values).length) panel.append(el("p", "None recorded."));
      for (const [currency, amount] of Object.entries(values)) panel.append(el("p", `${currency}: ${amount} minor units`));
    }
    details(panel, "Action states", result.actions, true);
    status.textContent = `Briefing refreshed at ${formatStamp(madeAt)} from all local records.`;
  }

  async function queue() {
    const current = ++generation; panel.replaceChildren(el("h3", "Approval queue & receipts"));
    await linkedinPanel(panel, status, queue);
    if (current !== generation) return;
    const tools = el("div"); tools.className = "business-tools"; panel.append(tools);
    const list = el("div"); panel.append(list); let before: number | undefined;
    const load = button(panel, "Load older actions", async () => { await page(); }, status);
    async function page() {
      const result = await api(`actions${before ? `?before=${before}` : ""}`);
      if (current !== generation) return;
      // Offered once there is something finished to clear, and not before.
      if ((result.items as Action[]).some(finished) && !tools.childElementCount) {
        armed(tools, "Clear completed", "Confirm clear", async () => {
          const cleared = await api("actions/clear-completed", undefined, "POST");
          await queue(); status.textContent = `Cleared ${cleared.deleted} completed approval${cleared.deleted === 1 ? "" : "s"}. ${LEDGER_NOTE}`;
        }, status);
      }
      for (const item of result.items as Action[]) {
        before = item.seq;
        // A connector action is one JARVIS was BLOCKED from making on a
        // server the user declared themselves. Approving it does not send
        // anything — JARVIS cannot call that server at all; it lifts the
        // refusal for exactly this call, once, the next time he asks. Saying
        // "Approval sends this exact request now" here would be false in the
        // one direction that hurts: a user who approves, sees nothing
        // happen and asks again is how a post gets published twice.
        const connector = item.provider.startsWith("connector:");
        // The hold is 120s; the card is offered for 24h. Past the hold there
        // is no call left to release, so "it goes out immediately" is false
        // in the one direction that causes duplicates — the user approves,
        // sees nothing happen, and asks again. `created` is when the gate
        // staged it, which is when the hold began.
        const stillHeld = connector && (Date.now() - item.created * 1000) < 120_000;
        const card = el("article"); card.append(el("h4", `${item.provider} · ${item.operation}`), el("p", item.state));
        const live = item.state === "pending" && item.expires * 1000 > Date.now();
        card.append(clockLine([["Staged", item.created], ["Updated", item.updated],
                               ...(live ? [["Expires", item.expires] as [string, number]] : [])]));
        if (item.repeat_note) { const warn = el("p", item.repeat_note); warn.className = "business-warning"; warn.setAttribute("role", "alert"); card.append(warn); }
        if (connector && item.state === "pending") readableText(card, item.payload);
        if ((item.provider === "linkedin" || item.provider === "linkedin_hand") && item.state === "pending") readableText(card, (item.payload as { request?: unknown }).request);
        details(card, connector ? "Review exactly what JARVIS would send" : "Review exact destination, budget, creative and targeting", item.payload, item.state === "pending");
        if (item.state === "pending" && item.expires * 1000 > Date.now()) {
          card.append(el("p", connector
            ? (stillHeld
              ? "JARVIS is holding this call open right now while you decide. Approve and it goes out immediately, with exactly these bytes — he does not rebuild it, so what you see is what is sent. Nothing has gone out yet."
              : "He is no longer holding this one — he gave up waiting and told you so. Approving does not send it now: it allows this exact request, once, the next time you ask him. It lapses when the card expires.")
            : "Approval sends this exact request now. Active ads may spend under the provider’s budget rules. Calls incur provider charges. Check currency, destination, dates, and existing campaign budgets before proceeding."));
          const label = el("label", "I reviewed this request and authorize it"); const check = el("input"); check.type = "checkbox"; label.prepend(check); card.append(label);
          const approve = button(card, !connector ? "Approve & send"
                                 : stillHeld ? "Allow it through now" : "Allow this once", async () => {
            if (!check.checked) return;
            await api(`actions/${item.id}/decision`, { digest: item.digest, approve: true });
            await queue(); status.textContent = connector
              ? (stillHeld
                ? "Allowed. It is going out now, with exactly these bytes."
                : "Allowed. Ask JARVIS again and this exact request will go through once.")
              : "Receipt recorded. Submitted means accepted by the provider; check its report for delivery.";
          }, status);
          approve.disabled = true; check.addEventListener("change", () => approve.disabled = !check.checked);
          button(card, "Reject", async () => { await api(`actions/${item.id}/decision`, { digest: item.digest, approve: false }); await queue(); status.textContent = "Rejected."; }, status);
        } else {
          details(card, "Receipt", item.result, true);
          if (item.state === "unknown") card.append(el("p", "Do not recreate this action until you check the provider console: it may already have succeeded."));
        }
        button(card, "Refresh receipt & audit", async () => { const receipt = await api(`actions/${item.id}`); details(card, "Latest receipt", receipt, true); status.textContent = "Receipt refreshed."; }, status);
        if (finished(item)) {
          armed(card, "Delete", "Confirm delete", async () => {
            await api(`actions/${item.id}`, undefined, "DELETE");
            await queue(); status.textContent = `Deleted. ${LEDGER_NOTE}`;
          }, status);
        }
        list.append(card);
      }
      load.hidden = result.items.length < 50;
      if (!list.childElementCount) list.append(el("p", "No actions yet. Prepare a campaign change or a call first."));
      status.textContent = "Queue loaded.";
    }
    await page();
  }

  async function prepare() {
    ++generation; panel.replaceChildren(el("h3", "Prepare an action"));
    const provider = select(panel, "Provider", ["google", "meta", "twilio", "chatgpt", "linkedin", "linkedin_hand"]);
    const operation = field(panel, "Action", "mutate");
    const label = el("label", "Request details"); const payload = el("textarea"); payload.rows = 15; label.append(payload); panel.append(label);
    const callLabel = el("label", "Message to read during your call"); const callMessage = el("textarea"); callMessage.rows = 4; callMessage.maxLength = 2000; callLabel.append(callMessage); panel.append(callLabel);
    const duration = field(panel, "Maximum call duration in seconds", "120", "number"); duration.min = "10"; duration.max = "300";
    panel.append(el("p", "The examples create paused campaigns or an owner notification. Replace account/resource IDs and fill in the exact creative, targeting and budget you want. Use JARVIS text input to prepare a complete proposal in plain language. Nothing is sent until you review it in the queue."));
    function template() {
      const examples: Record<string, [string, unknown]> = {
        google: ["mutate", { mutateOperations: [{ campaignBudgetOperation: { create: { name: "Launch budget", amountMicros: "10000000", deliveryMethod: "STANDARD", explicitlyShared: false, resourceName: "customers/YOUR_CUSTOMER_ID/campaignBudgets/-1" } } }, { campaignOperation: { create: { name: "Launch", status: "PAUSED", advertisingChannelType: "SEARCH", campaignBudget: "customers/YOUR_CUSTOMER_ID/campaignBudgets/-1", manualCpc: {}, containsEuPoliticalAdvertising: "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING" } } }] }],
        meta: ["campaigns", { name: "Launch", objective: "OUTCOME_TRAFFIC", status: "PAUSED", special_ad_categories: [], daily_budget: "1000", bid_strategy: "LOWEST_COST_WITHOUT_CAP" }],
        twilio: ["call", { message: "Hello. This is your JARVIS business update.", time_limit: 120 }],
        chatgpt: ["campaigns", { name: "Launch", status: "paused", budget: { lifetime_spend_limit_micros: 25000000 } }],
      };
      const example = examples[provider.value]; operation.value = example[0]; payload.value = JSON.stringify(example[1], null, 2);
      const call = provider.value === "twilio";
      callLabel.hidden = !call; duration.parentElement!.hidden = !call; label.hidden = call;
      callMessage.value = "Hello. This is your JARVIS business update.";
    }
    operation.addEventListener("input", () => {
      const call = provider.value === "twilio" && operation.value === "call";
      callLabel.hidden = !call; duration.parentElement!.hidden = !call; label.hidden = call;
    });
    provider.addEventListener("change", template); template();
    button(panel, "Save for review", async () => {
      const body = provider.value === "twilio" && operation.value === "call"
        ? { message: callMessage.value, time_limit: Number(duration.value) } : JSON.parse(payload.value);
      await api("proposals", { provider: provider.value, operation: operation.value, payload: body });
      await queue(); status.textContent = "Saved for your review. No external action has been taken.";
    }, status);
    status.textContent = "Drafts expire after 24 hours.";
  }

  async function records(kind: string) {
    const current = ++generation; panel.replaceChildren(el("h3", `${kind[0].toUpperCase()}${kind.slice(1)} records`));
    const form = el("form"); form.addEventListener("submit", event => event.preventDefault()); panel.append(form);
    const titleInput = field(form, "Title / name"); const notes = field(form, "Notes"); const contact = field(form, "Contact / customer");
    const choices: Record<string, string[]> = { task: ["open", "done"], contact: ["lead", "qualified", "won", "lost"], invoice: ["draft", "sent", "paid", "void"], expense: ["open", "paid", "void"] };
    const state = select(form, "Status", choices[kind]); const due = field(form, "Due / follow-up date", "", "date");
    const amount = field(form, "Amount in currency minor units (e.g. cents)", "0", "number"); amount.min = "0"; amount.step = "1";
    const currency = field(form, "Currency", "USD"); let editing: RecordRow | null = null;
    if (kind === "task" || kind === "contact") { amount.parentElement!.hidden = true; currency.parentElement!.hidden = true; }
    const save = button(form, "Save record", async () => {
      await api("records", { kind, title: titleInput.value, notes: notes.value, contact: contact.value, status: state.value,
        due: due.value, amount_minor: Number(amount.value), currency: currency.value.toUpperCase(),
        ...(editing ? { id: editing.id, version: editing.version } : {}) });
      await records(kind); status.textContent = "Saved locally. No message or payment was sent.";
    }, status);
    button(form, "New record", async () => { await records(kind); }, status);
    const list = el("div"); panel.append(list); let before: number | undefined;
    const more = button(panel, "Load older records", async () => { await page(); }, status);
    async function page() {
      const result = await api(`records/${kind}${before ? `?before=${before}` : ""}`);
      if (current !== generation) return;
      for (const item of result.items as RecordRow[]) {
        before = item.seq; const body = item.body; const card = el("article");
        card.append(el("h4", body.title), el("p", `${body.status}${body.due ? ` · Due ${body.due}` : ""}`),
                    clockLine([["Updated", item.updated]]), el("p", body.notes));
        if (kind === "invoice" || kind === "expense") card.append(el("p", `${body.amount_minor} minor units · ${body.currency}`));
        if (body.contact) card.append(el("p", body.contact));
        button(card, "Edit", async () => {
          editing = item; titleInput.value = body.title; notes.value = body.notes; contact.value = body.contact;
          state.value = body.status; due.value = body.due; amount.value = String(body.amount_minor); currency.value = body.currency;
          save.textContent = "Save changes"; titleInput.focus(); status.textContent = "Editing selected record.";
        }, status); list.append(card);
      }
      more.hidden = result.items.length < 100;
      if (!list.childElementCount) list.append(el("p", "No records yet."));
      status.textContent = "Records loaded.";
    }
    await page();
  }

  for (const [label, action] of [["Briefing", summary], ["Approvals", queue], ["Prepare action", prepare], ...["task", "contact", "invoice", "expense"].map(kind => [kind[0].toUpperCase() + kind.slice(1) + "s", () => records(kind)])] as [string, () => Promise<void>][]) button(tabs, label, action, status);
  const exportLink = el("a", "Export business records & audit"); exportLink.href = "/api/business/export"; exportLink.download = "business.jsonl"; body.append(exportLink);
  body.append(el("p", "Private business data is saved in the JARVIS database and included in verified backups. No automatic deletion. Invoices and expenses are local tracking records; they do not send invoices, charge cards, or replace accounting software."));
  document.body.append(dialog); dialog.show();
  makeWindow({ panel: dialog, head, body, toggle, key: PANEL_KEY });
  void (async () => {
    try {
      const result = await api("connections"); connections.append(el("h3", "Connections"));
      for (const [provider, info] of Object.entries(result.providers) as [string, {configured: boolean; missing: string[]; issue: string | null}][]) {
        const row = el("div"); row.append(el("strong", provider), el("span", info.configured ? " · Configured; not yet verified" : " · Setup required"));
        if (info.missing.length || info.issue) details(row, "Local setup", { environment: info.missing, issue: info.issue });
        const report = button(row, "Check connection & report", async () => {
          const result = await api(`reports/${provider}`); details(row, "Recent provider results", result, true); status.textContent = "Provider responded. Results may be paginated.";
        }, status); report.disabled = !info.configured; connections.append(row);
      }
      if (generation === 0) await queue();
    } catch (error) { status.textContent = String(error); }
  })();
}
