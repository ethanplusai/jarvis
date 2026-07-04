"""
JARVIS MCP Registry — connectable tool servers via the Model Context Protocol.

MCP lets JARVIS reach external tools (email, docs, CRM, databases, design) through
a standard protocol instead of bespoke integrations. This module maintains a
catalog of well-known MCP servers a small business is likely to want, plus a
persistent record of which the user has connected and with what config.

This is the connection/registry layer: it stores connections, reports status, and
makes JARVIS aware (via the system prompt) of which tools it can reach. Actually
spawning MCP client sessions is done by the connector layer at call time; servers
are described here with the command/URL needed to launch them.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger("jarvis.mcp")

DB_PATH = Path(__file__).parent / "data" / "jarvis.db"


@dataclass(frozen=True)
class McpServer:
    id: str
    name: str
    category: str
    description: str
    transport: str          # "stdio" or "http"
    launch: str             # command (stdio) or URL (http) template; "" → user must supply one
    auth_env: str = ""      # env var holding the token/key, if any
    docs_url: str = ""
    capabilities: tuple[str, ...] = ()
    needs_config: bool = False   # no public hosted endpoint; user pastes a URL/command
    config_hint: str = ""        # shown in Settings next to the config input


# Curated catalog of MCP servers useful to a small company.
MCP_CATALOG: tuple[McpServer, ...] = (
    McpServer("gmail", "Gmail", "Email", "Read, search, label, and draft email.", "http",
              "", "GMAIL_MCP_TOKEN",
              "https://modelcontextprotocol.io/", ("search", "draft", "label"),
              needs_config=True,
              config_hint="No public hosted endpoint — paste the MCP server URL or stdio command from your Google Workspace MCP provider."),
    McpServer("google-drive", "Google Drive", "Docs & Storage", "Search and read files and docs.", "http",
              "", "GDRIVE_MCP_TOKEN",
              "https://modelcontextprotocol.io/", ("search", "read"),
              needs_config=True,
              config_hint="No public hosted endpoint — paste the MCP server URL or stdio command from your Google Workspace MCP provider."),
    McpServer("notion", "Notion", "Docs & Wiki", "Create, search, and update Notion pages and databases.", "http",
              "https://mcp.notion.com/mcp", "NOTION_MCP_TOKEN",
              "https://developers.notion.com/", ("search", "create", "update")),
    McpServer("slack", "Slack", "Comms", "Post messages and read channels.", "stdio",
              "npx -y @modelcontextprotocol/server-slack", "SLACK_BOT_TOKEN",
              "https://modelcontextprotocol.io/", ("post", "read")),
    McpServer("github", "GitHub", "Dev", "Issues, pull requests, and repository operations.", "http",
              "https://api.githubcopilot.com/mcp/", "GITHUB_TOKEN",
              "https://github.com/github/github-mcp-server", ("issues", "prs", "code")),
    McpServer("supabase", "Supabase", "Database", "Query and manage a Postgres database.", "stdio",
              "npx -y @supabase/mcp-server-supabase", "SUPABASE_ACCESS_TOKEN",
              "https://supabase.com/docs", ("sql", "schema")),
    McpServer("stripe", "Stripe", "Payments", "Customers, invoices, and payment data.", "http",
              "https://mcp.stripe.com", "STRIPE_API_KEY",
              "https://docs.stripe.com/mcp", ("invoices", "customers")),
    McpServer("hubspot", "HubSpot CRM", "CRM", "Contacts, deals, and pipeline.", "http",
              "https://mcp.hubspot.com/anthropic", "HUBSPOT_TOKEN",
              "https://developers.hubspot.com/mcp", ("contacts", "deals")),
    McpServer("google-calendar", "Google Calendar", "Scheduling", "Read and create calendar events.", "http",
              "", "GCAL_MCP_TOKEN",
              "https://modelcontextprotocol.io/", ("read", "create"),
              needs_config=True,
              config_hint="No public hosted endpoint — paste the MCP server URL or stdio command from your Google Workspace MCP provider."),
    McpServer("figma", "Figma", "Design", "Read designs and generate UI from them.", "http",
              "https://mcp.figma.com/mcp", "FIGMA_MCP_TOKEN",
              "https://www.figma.com/developers", ("read", "generate")),
    McpServer("filesystem", "Local Files", "Storage", "Read and write files in an allowed folder.", "stdio",
              "npx -y @modelcontextprotocol/server-filesystem", "",
              "https://modelcontextprotocol.io/", ("read", "write")),
    McpServer("web-search", "Web Search", "Research", "Search the web for current information.", "stdio",
              "npx -y @modelcontextprotocol/server-brave-search", "BRAVE_API_KEY",
              "https://modelcontextprotocol.io/", ("search",)),
    McpServer("linear", "Linear", "Project Tracking", "Issues, projects, and cycles in Linear.", "http",
              "https://mcp.linear.app/sse", "LINEAR_MCP_TOKEN",
              "https://linear.app/docs/mcp", ("issues", "projects")),
    McpServer("sentry", "Sentry", "Monitoring", "Errors, issues, and release health.", "http",
              "https://mcp.sentry.dev/mcp", "SENTRY_MCP_TOKEN",
              "https://docs.sentry.io/product/sentry-mcp/", ("errors", "issues")),
    McpServer("asana", "Asana", "Project Tracking", "Tasks, projects, and goals in Asana.", "http",
              "https://mcp.asana.com/sse", "ASANA_MCP_TOKEN",
              "https://developers.asana.com/docs/mcp-server", ("tasks", "projects")),
    McpServer("atlassian", "Atlassian (Jira & Confluence)", "Project Tracking", "Jira issues and Confluence pages.", "http",
              "https://mcp.atlassian.com/v1/sse", "ATLASSIAN_MCP_TOKEN",
              "https://www.atlassian.com/platform/remote-mcp-server", ("issues", "pages")),
    McpServer("zapier", "Zapier", "Automation", "Trigger thousands of app actions through Zapier.", "http",
              "https://mcp.zapier.com/api/mcp/mcp", "ZAPIER_MCP_TOKEN",
              "https://zapier.com/mcp", ("actions", "automation")),
    McpServer("canva", "Canva", "Design", "Create and manage Canva designs.", "http",
              "https://mcp.canva.com/mcp", "CANVA_MCP_TOKEN",
              "https://www.canva.dev/docs/connect/mcp-server/", ("designs", "export")),
)

_BY_ID = {s.id: s for s in MCP_CATALOG}


def _get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_mcp_db():
    conn = _get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS mcp_connections (
            server_id TEXT PRIMARY KEY,
            status TEXT DEFAULT 'connected',   -- connected, disconnected, error
            config TEXT DEFAULT '{}',          -- JSON: any non-secret connection config
            connected_at REAL,
            last_used REAL
        );
    """)
    conn.commit()
    conn.close()


def auth_env_keys() -> set[str]:
    """Env vars holding MCP auth tokens — whitelisted by the settings API."""
    return {s.auth_env for s in MCP_CATALOG if s.auth_env}


def _auth_present(server: McpServer) -> bool:
    if not server.auth_env:
        return True
    return bool(os.environ.get(server.auth_env, "").strip())


def list_servers() -> list[dict]:
    """All catalog servers with connection + auth status."""
    conn = _get_db()
    rows = {r["server_id"]: dict(r) for r in conn.execute("SELECT * FROM mcp_connections").fetchall()}
    conn.close()
    out = []
    for s in MCP_CATALOG:
        item = asdict(s)
        item["capabilities"] = list(s.capabilities)
        conn_row = rows.get(s.id)
        item["connected"] = bool(conn_row and conn_row["status"] == "connected")
        item["status"] = conn_row["status"] if conn_row else "disconnected"
        item["auth_required"] = bool(s.auth_env)
        item["auth_present"] = _auth_present(s)
        out.append(item)
    return out


def connect(server_id: str, config: dict | None = None) -> dict:
    server = _BY_ID.get(server_id)
    if not server:
        return {"error": f"Unknown MCP server '{server_id}'"}
    init_mcp_db()
    now = time.time()
    conn = _get_db()
    conn.execute(
        "INSERT INTO mcp_connections (server_id, status, config, connected_at) "
        "VALUES (?, 'connected', ?, ?) "
        "ON CONFLICT(server_id) DO UPDATE SET status='connected', config=excluded.config, connected_at=excluded.connected_at",
        (server_id, json.dumps(config or {}), now),
    )
    conn.commit()
    conn.close()
    log.info(f"MCP connected: {server_id}")
    return {"success": True, "server_id": server_id, "auth_present": _auth_present(server)}


def disconnect(server_id: str) -> dict:
    conn = _get_db()
    conn.execute("UPDATE mcp_connections SET status='disconnected' WHERE server_id = ?", (server_id,))
    conn.commit()
    conn.close()
    log.info(f"MCP disconnected: {server_id}")
    return {"success": True, "server_id": server_id}


def connected_servers() -> list[dict]:
    return [s for s in list_servers() if s["connected"]]


def get_connected_server(server_id: str) -> dict | None:
    """Return a connected server descriptor enriched with auth/config details."""
    init_mcp_db()
    server = _BY_ID.get(server_id)
    if not server:
        return None
    conn = _get_db()
    row = conn.execute(
        "SELECT * FROM mcp_connections WHERE server_id = ? AND status = 'connected'",
        (server_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    item = asdict(server)
    item["capabilities"] = list(server.capabilities)
    item["connected"] = True
    item["status"] = row["status"]
    item["config"] = json.loads(row["config"] or "{}")
    item["auth_required"] = bool(server.auth_env)
    item["auth_present"] = _auth_present(server)
    return item


def mark_used(server_id: str) -> None:
    conn = _get_db()
    conn.execute("UPDATE mcp_connections SET last_used = ? WHERE server_id = ?", (time.time(), server_id))
    conn.commit()
    conn.close()


def recommend_servers(profile_tools: str, limit: int = 5) -> list[dict]:
    """Recommend MCP servers based on a free-text description of the user's tools."""
    text = (profile_tools or "").lower()
    if not text:
        return []
    scored = []
    for s in MCP_CATALOG:
        blob = f"{s.name} {s.category} {s.description}".lower()
        score = sum(1 for word in blob.split() if len(word) > 3 and word in text)
        # Direct name hits weigh more.
        if s.name.lower() in text or s.id in text:
            score += 3
        if score > 0:
            item = asdict(s)
            item["capabilities"] = list(s.capabilities)
            scored.append((score, item))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in scored[:limit]]


def mcp_prompt() -> str:
    """Tell JARVIS which external tools it can reach via MCP."""
    connected = connected_servers()
    if not connected:
        return ""
    lines = ["CONNECTED TOOLS (via MCP — you can use these with [ACTION:MCP_CALL] server_id ||| tool_name ||| {json args}):"]
    for s in connected:
        caps = ", ".join(s["capabilities"]) if s["capabilities"] else ""
        warn = "" if s["auth_present"] else " (needs auth key in Settings)"
        lines.append(f"- {s['name']} ({s['category']}, id: {s['id']}): {caps}{warn}")
    return "\n".join(lines)


# Initialize on import
init_mcp_db()
