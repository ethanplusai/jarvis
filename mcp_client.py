"""Minimal live MCP client for connected JARVIS tool servers.

Supports JSON-RPC MCP calls over stdio and HTTP endpoints from the registry. The
client intentionally exposes only two primitives JARVIS needs today: list tools
and call a named tool. Higher-level guardrails live in server.py/action_log.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from typing import Any

import httpx

import mcp_registry

MCP_PROTOCOL_VERSION = "2024-11-05"


class McpClientError(RuntimeError):
    pass


def _headers(server: dict[str, Any]) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    auth_env = server.get("auth_env") or ""
    token = os.environ.get(auth_env, "").strip() if auth_env else ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _rpc(method: str, params: dict[str, Any] | None = None, request_id: int = 1) -> dict[str, Any]:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    return body


def _extract_result(payload: dict[str, Any]) -> Any:
    if "error" in payload:
        raise McpClientError(payload["error"].get("message") if isinstance(payload["error"], dict) else str(payload["error"]))
    return payload.get("result", payload)


async def _http_rpc(server: dict[str, Any], method: str, params: dict[str, Any] | None = None) -> Any:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(server["launch"], headers=_headers(server), json=_rpc(method, params))
        response.raise_for_status()
        return _extract_result(response.json())


async def _stdio_session(server: dict[str, Any], calls: list[tuple[dict[str, Any], bool]]) -> list[Any]:
    cmd = shlex.split(server["launch"])
    if not cmd:
        raise McpClientError("MCP stdio launch command is empty")
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ.copy(),
    )
    assert process.stdin and process.stdout
    results: list[Any] = []
    try:
        for call, expect_response in calls:
            process.stdin.write((json.dumps(call) + "\n").encode())
            await process.stdin.drain()
            if not expect_response:
                continue
            line = await asyncio.wait_for(process.stdout.readline(), timeout=30)
            if not line:
                stderr = (await process.stderr.read()).decode(errors="replace") if process.stderr else ""
                raise McpClientError(stderr.strip() or "MCP server closed without a response")
            results.append(_extract_result(json.loads(line.decode())))
        return results
    finally:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except asyncio.TimeoutError:
            process.kill()


async def _stdio_rpc(server: dict[str, Any], method: str, params: dict[str, Any] | None = None) -> Any:
    calls = [
        (_rpc("initialize", {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "jarvis", "version": "0.1.0"}}, 1), True),
        ({"jsonrpc": "2.0", "method": "notifications/initialized"}, False),
        (_rpc(method, params, 2), True),
    ]
    results = await _stdio_session(server, calls)
    return results[-1]


async def call(server_id: str, method: str, params: dict[str, Any] | None = None) -> Any:
    server = mcp_registry.get_connected_server(server_id)
    if not server:
        raise McpClientError(f"MCP server '{server_id}' is not connected")
    if server.get("auth_required") and not server.get("auth_present"):
        raise McpClientError(f"MCP server '{server_id}' needs {server.get('auth_env')} in Settings")
    # A per-connection URL or stdio command (saved in Settings) overrides the
    # catalog default — required for servers without a public hosted endpoint.
    config = server.get("config") or {}
    custom = (config.get("url") or config.get("command") or "").strip()
    if custom:
        server["launch"] = custom
        server["transport"] = "http" if custom.startswith(("http://", "https://")) else "stdio"
    if not server.get("launch"):
        raise McpClientError(
            f"MCP server '{server_id}' has no endpoint — paste its URL or command in Settings → Tools"
        )
    if server["transport"] == "http":
        result = await _http_rpc(server, method, params)
    elif server["transport"] == "stdio":
        result = await _stdio_rpc(server, method, params)
    else:
        raise McpClientError(f"Unsupported MCP transport: {server['transport']}")
    mcp_registry.mark_used(server_id)
    return result


async def list_tools(server_id: str) -> list[dict[str, Any]]:
    result = await call(server_id, "tools/list")
    return result.get("tools", []) if isinstance(result, dict) else []


async def call_tool(server_id: str, tool_name: str, arguments: dict[str, Any] | None = None) -> Any:
    return await call(server_id, "tools/call", {"name": tool_name, "arguments": arguments or {}})
