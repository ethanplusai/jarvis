"""Shared platform contract for REST, MCP discovery and execution."""
import socket
import sys

TOOL_CAPABILITIES = {
    "open_in_browser": "open_browser", "open_in_editor": "open_editor",
    "open_in_terminal": "open_terminal", "look_at_screen": "screen_capture",
    "what_is_on_screen": "window_list", "answer_dialog": "answer_dialog",
    "steer_session": "session_steering",
}


def _line_configured(module_name):
    # Lazy: this module is imported by the MCP child too, whose environment
    # is scrubbed of the phone lines' settings on purpose (claude_env). There
    # it answers False and nothing depends on it; `message_user` is not gated
    # on these keys so the tool stays listed and the SERVER — which has the
    # settings — is what refuses or sends.
    try:
        import importlib
        return bool(importlib.import_module(module_name).configured())
    except Exception:
        return False


def capabilities():
    desktop = sys.platform in {"darwin", "win32"}
    return {
        "platform": sys.platform, "typed_input": True, "conversation_history": True,
        **{key: desktop for key in ("open_browser", "open_editor", "open_terminal",
                                   "screen_capture", "window_list", "notifications", "answer_dialog")},
        "session_steering": sys.platform == "win32" or hasattr(socket, "AF_UNIX"),
        "whatsapp": _line_configured("whatsapp"),
        "telegram": _line_configured("telegram"),
        "limitations": [
            "Desktop availability describes adapter support; OS permissions and an interactive desktop are required.",
            "Session steering requires the CLI inbox transport and matching authorization.",
            "Permission-dialog control requires a verifiable terminal session; unsupported targets are refused.",
            "WhatsApp and Telegram reach the configured owner only; calls are not available (voice notes are).",
        ],
    }


def tool_supported(name):
    required = TOOL_CAPABILITIES.get(name)
    return required is None or capabilities().get(required, False)
