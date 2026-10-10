export function openDiagnostics() {
  const existing = document.getElementById("diagnostics") as HTMLDialogElement | null;
  if (existing) { existing.showModal(); return; }
  const dialog = document.createElement("dialog");
  dialog.id = "diagnostics";
  const heading = document.createElement("h2");
  heading.textContent = "Diagnostics & data";
  const status = document.createElement("p");
  status.setAttribute("role", "status");
  const content = document.createElement("div");
  const controls = document.createElement("div");
  const close = document.createElement("button");
  close.textContent = "Close";
  close.addEventListener("click", () => dialog.close());
  async function load(url = "/api/readiness", method = "GET") {
    status.textContent = "Checking…";
    controls.querySelectorAll("button").forEach(button => button.disabled = true);
    try {
      const response = await fetch(url, { method });
      if (!response.ok) throw new Error(`Request failed (${response.status})`);
      const body = await response.json();
      content.replaceChildren();
      status.textContent = body.ready ? "Ready" : "Needs attention";
      for (const [name, value] of Object.entries({
        "Brain": body.brain_ready ? "Ready" : "Not ready",
        "Login": body.login || "Unchecked",
        "Voice": body.tts || "Unchecked",
        "Voice usable": body.voice_ready ? "Yes" : "Not verified",
        "Bind": body.bind ? `${body.bind.scheme}://${body.bind.host}:${body.bind.port} (${body.bind.source})` : "Unknown",
        "Database": body.database, "Port": body.port,
      })) {
        const line = document.createElement("p"); line.textContent = `${name}: ${value}`; content.append(line);
      }
      for (const check of body.checks || []) {
        const line = document.createElement("p");
        line.textContent = `${check.name}: ${check.status} — ${check.message}${check.remedy ? ` ${check.remedy}` : ""}`;
        content.append(line);
      }
      const capabilities = document.createElement("h3");
      capabilities.textContent = "This computer"; content.append(capabilities);
      for (const [name, enabled] of Object.entries(body.capabilities || {})) {
        if (typeof enabled !== "boolean") continue;
        const line = document.createElement("p");
        line.textContent = `${name.replace(/_/g, " ")}: ${enabled ? "Available" : "Unavailable"}`;
        content.append(line);
      }
      for (const limitation of body.capabilities?.limitations || []) {
        const line = document.createElement("p"); line.textContent = limitation; content.append(line);
      }
    } catch (error) { status.textContent = String(error); }
    finally { controls.querySelectorAll("button").forEach(button => button.disabled = false); }
  }
  for (const [label, url] of [
    ["Run checks", "/api/diagnostics/check"],
    ["Restart brain", "/api/diagnostics/repair/restart-brain"],
    ["Open Claude login", "/api/diagnostics/repair/open-login"],
  ]) {
    const button = document.createElement("button"); button.textContent = label;
    button.addEventListener("click", () => void load(url, "POST")); controls.append(button);
  }
  dialog.append(heading, status, content, controls, close);
  const data = document.createElement("section");
  const dataHeading = document.createElement("h3");
  dataHeading.textContent = "Your data";
  const result = document.createElement("p");
  result.setAttribute("role", "status");
  const days = document.createElement("input");
  days.type = "number"; days.min = "1"; days.max = "36500"; days.value = "90";
  days.setAttribute("aria-label", "Retain runs for this many days");
  async function request(url: string, payload?: unknown) {
    const response = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
      body: payload === undefined ? undefined : JSON.stringify(payload) });
    if (!response.ok) throw new Error(`Data operation failed (${response.status})`);
    return response.json();
  }
  const dataControls = document.createElement("div");
  for (const [label, action] of [
    ["Download verified backup", async () => request("/api/data/backup")],
    ["Export runs", async () => request("/api/data/export")],
    ["Preview retention", async () => request("/api/data/retention", { days: Number(days.value) })],
    ["Back up & delete eligible old runs", async () => request("/api/data/retention", { days: Number(days.value), apply: true })],
  ] as const) {
    const button = document.createElement("button"); button.textContent = label;
    button.addEventListener("click", async () => {
      dataControls.querySelectorAll("button").forEach(b => b.disabled = true);
      result.textContent = "Working…";
      try {
        const body = await action();
        result.textContent = body.eligible !== undefined ? `${body.eligible} eligible; ${body.deleted} deleted. Active runs and referenced parents are preserved.` : "Ready to download.";
        const url = body.download || body.backup;
        if (url) {
          const link = document.createElement("a"); link.href = url; link.textContent = " Download";
          link.download = ""; result.append(link);
        }
      } catch (error) { result.textContent = String(error); }
      finally { dataControls.querySelectorAll("button").forEach(b => b.disabled = false); }
    });
    dataControls.append(button);
  }
  const help = document.createElement("p");
  help.textContent = "Backups contain private memory and configuration. To restore, stop JARVIS and run: python maintenance.py restore path-to-backup.zip. Restore verifies checksums and SQLite integrity and retains the previous data for rollback.";
  data.append(dataHeading, days, dataControls, result, help);
  dialog.insertBefore(data, close);
  document.body.append(dialog);
  dialog.showModal(); void load();
}
