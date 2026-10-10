import platform_capabilities as platform
import jarvis_mcp


def test_platform_tool_discovery_and_execution_contract(monkeypatch):
    monkeypatch.setattr(platform.sys, "platform", "linux")
    assert not platform.tool_supported("look_at_screen")
    assert platform.tool_supported("business_propose")
    reply = jarvis_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {item["name"] for item in reply["result"]["tools"]}
    assert "look_at_screen" not in names
    assert "business_propose" in names


def test_windows_adapters_advertised_with_limitations(monkeypatch):
    monkeypatch.setattr(platform.sys, "platform", "win32")
    value = platform.capabilities()
    assert value["screen_capture"] and value["session_steering"]
    assert value["limitations"]
