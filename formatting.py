"""
Pure formatting and text-processing helpers for JARVIS.

All functions here are deterministic, stateless, and fully unit-tested.
No module-level globals, no side effects.
"""

import re as _re
import time

STT_CORRECTIONS = {
    r"\bcloud code\b": "Claude Code",
    r"\bclock code\b": "Claude Code",
    r"\bquad code\b": "Claude Code",
    r"\bclawed code\b": "Claude Code",
    r"\bclod code\b": "Claude Code",
    r"\bcloud\b": "Claude",
    r"\bquad\b": "Claude",
    r"\btravis\b": "JARVIS",
    r"\bjarves\b": "JARVIS",
}

_BANNED_PHRASES = [
    "my apologies",
    "i apologize",
    "absolutely",
    "great question",
    "i'd be happy to",
    "of course",
    "how can i help",
    "is there anything else",
    "i should clarify",
    "let me know if",
    "feel free to",
]

_ACTION_TAG_RE = _re.compile(
    r"\[ACTION:(BUILD|BROWSE|RESEARCH|OPEN_TERMINAL|PROMPT_PROJECT|ADD_TASK|ADD_NOTE|COMPLETE_TASK|REMEMBER|CREATE_NOTE|READ_NOTE|SCREEN|SET_TIMER)\]\s*(.*?)$",
    _re.DOTALL,
)


def apply_speech_corrections(text: str) -> str:
    """Fix common speech-to-text errors before processing."""
    result = text
    for pattern, replacement in STT_CORRECTIONS.items():
        result = _re.sub(pattern, replacement, result, flags=_re.IGNORECASE)
    return result


def strip_markdown_for_tts(text: str) -> str:
    """Strip ALL markdown from text before sending to TTS."""
    result = text
    # Remove code blocks (``` ... ```)
    result = _re.sub(r"```[\s\S]*?```", "", result)
    # Remove inline code
    result = result.replace("`", "")
    # Remove bold/italic markers
    result = result.replace("**", "").replace("*", "")
    # Remove headers
    result = _re.sub(r"^#{1,6}\s*", "", result, flags=_re.MULTILINE)
    # Convert [text](url) to just text
    result = _re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", result)
    # Remove bullet points
    result = _re.sub(r"^\s*[-*+]\s+", "", result, flags=_re.MULTILINE)
    # Remove numbered lists
    result = _re.sub(r"^\s*\d+\.\s+", "", result, flags=_re.MULTILINE)
    # Double newlines to period
    result = _re.sub(r"\n{2,}", ". ", result)
    # Single newlines to space
    result = result.replace("\n", " ")
    # Clean up multiple spaces
    result = _re.sub(r"\s{2,}", " ", result)

    # Strip banned phrases
    result_lower = result.lower()
    for phrase in _BANNED_PHRASES:
        idx = result_lower.find(phrase)
        while idx != -1:
            end = idx + len(phrase)
            if end < len(result) and result[end] in " ,—-":
                end += 1
            result = result[:idx] + result[end:]
            result_lower = result.lower()
            idx = result_lower.find(phrase)

    return result.strip().strip(",").strip("—").strip("-").strip()


def extract_action(response: str) -> tuple[str, dict | None]:
    """Extract [ACTION:X] tag from LLM response.

    Returns (clean_text_for_tts, action_dict_or_none).
    """
    match = _ACTION_TAG_RE.search(response)
    if match:
        action_type = match.group(1).lower()
        action_target = match.group(2).strip()
        clean_text = response[: match.start()].strip()
        return clean_text, {"action": action_type, "target": action_target}
    return response, None


def format_projects_for_prompt(projects: list[dict]) -> str:
    if not projects:
        return "No projects found on Desktop."
    lines = []
    for p in projects:
        lines.append(f"- {p['name']} ({p['branch']}) @ {p['path']}")
    return "\n".join(lines)


def format_mc_tasks_for_voice(tasks: list[dict]) -> str:
    """Format Mission Control tasks for voice response."""
    if not tasks:
        return "No open tasks, sir."
    count = len(tasks)
    active = [t for t in tasks if t.get("kanban") == "in-progress"]
    pending = [t for t in tasks if t.get("kanban") == "not-started"]

    parts = []
    if active:
        parts.append(f"{len(active)} in progress")
    if pending:
        parts.append(f"{len(pending)} pending")
    result = f"You have {count} tasks: {', '.join(parts)}."

    for t in tasks[:3]:
        status = "working on" if t.get("kanban") == "in-progress" else ""
        agent = t.get("assignedTo", "")
        title = t.get("title", "untitled")
        if status:
            result += f" {agent} is {status} {title}."
        else:
            result += f" {title}, assigned to {agent}."
    if count > 3:
        result += f" And {count - 3} more."
    return result


def format_mc_inbox_for_voice(messages: list[dict]) -> str:
    """Format Mission Control inbox messages for voice response."""
    if not messages:
        return "Inbox is empty, sir."
    count = len(messages)
    if count == 1:
        m = messages[0]
        return f"One message from {m.get('from', 'unknown')}: {m.get('subject', '')}."
    result = f"You have {count} unread messages."
    for m in messages[:3]:
        result += f" {m.get('from', 'unknown')}: {m.get('subject', '')}."
    if count > 3:
        result += f" And {count - 3} more."
    return result


def format_mc_decisions_for_voice(decisions: list[dict]) -> str:
    """Format Mission Control pending decisions for voice response."""
    if not decisions:
        return "No decisions pending, sir."
    count = len(decisions)
    if count == 1:
        d = decisions[0]
        return f"One decision pending from {d.get('requestedBy', 'an agent')}: {d.get('question', '')}"
    return f"{count} decisions pending, sir."


# time import retained in case we want to extend with time-based formatters
_ = time
