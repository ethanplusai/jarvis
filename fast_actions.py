"""
Fast keyword-based action detection for JARVIS.

Pure function — no side effects, no dependencies. Called BEFORE the LLM
for short, obvious commands (< 12 words). Returns an action dict if a
keyword pattern matches, otherwise None (which routes to the LLM).
"""


def detect_action_fast(text: str) -> dict | None:
    """Keyword-based action detection — ONLY for short, obvious commands.

    Everything else goes to the LLM which uses [ACTION:X] tags when it decides
    to act based on conversational understanding.
    """
    t = text.lower().strip()
    words = t.split()

    # Only trigger on SHORT, clear commands (< 12 words)
    if len(words) > 12:
        return None  # Long messages are conversation, not commands

    # Screen requests — checked BEFORE project matching to prevent misrouting
    if any(
        p in t
        for p in [
            "look at my screen",
            "what's on my screen",
            "whats on my screen",
            "what am i looking at",
            "what do you see",
            "see my screen",
            "what's running on my",
            "whats running on my",
            "check my screen",
        ]
    ):
        return {"action": "describe_screen"}

    # Terminal / Claude Code — explicit open requests
    if any(w in t for w in ["open claude", "start claude", "launch claude", "run claude"]):
        return {"action": "open_terminal"}

    # Show recent build
    if any(w in t for w in ["show me what you built", "pull up what you made", "open what you built"]):
        return {"action": "show_recent"}

    # Screen awareness — explicit look/see requests
    if any(
        p in t
        for p in [
            "what's on my screen",
            "whats on my screen",
            "what do you see",
            "can you see my screen",
            "look at my screen",
            "what am i looking at",
            "what's open",
            "whats open",
            "what apps are open",
        ]
    ):
        return {"action": "describe_screen"}

    # Calendar — explicit schedule requests
    if any(
        p in t
        for p in [
            "what's my schedule",
            "whats my schedule",
            "what's on my calendar",
            "whats on my calendar",
            "do i have any meetings",
            "any meetings",
            "what's next on my calendar",
            "my schedule today",
            "what do i have today",
            "my calendar",
            "upcoming meetings",
            "next meeting",
            "what's my next meeting",
        ]
    ):
        return {"action": "check_calendar"}

    # Mail — explicit email requests
    if any(
        p in t
        for p in [
            "check my email",
            "check my mail",
            "any new emails",
            "any new mail",
            "unread emails",
            "unread mail",
            "what's in my inbox",
            "whats in my inbox",
            "read my email",
            "read my mail",
            "any emails",
            "any mail",
            "email update",
            "mail update",
        ]
    ):
        return {"action": "check_mail"}

    # Dispatch / build status check
    if any(
        p in t
        for p in [
            "where are we",
            "where were we",
            "project status",
            "how's the build",
            "hows the build",
            "status update",
            "status report",
            "where is that",
            "how's it going with",
            "hows it going with",
            "is it done",
            "is that done",
            "what happened with",
        ]
    ):
        return {"action": "check_dispatch"}

    # Session check
    if any(
        p in t
        for p in [
            "what sessions",
            "active sessions",
            "running sessions",
            "list sessions",
            "what's running",
            "whats running",
        ]
    ):
        return {"action": "check_sessions"}

    # Inbox check (Mission Control)
    if any(
        p in t
        for p in [
            "what's in my inbox",
            "whats in my inbox",
            "check inbox",
            "any reports",
            "agent reports",
            "inbox messages",
        ]
    ):
        return {"action": "check_inbox"}

    # Decisions check (Mission Control)
    if any(
        p in t
        for p in [
            "any decisions",
            "decisions to make",
            "pending decisions",
            "what needs my approval",
            "approval queue",
        ]
    ):
        return {"action": "check_decisions"}

    # Task list check
    if any(
        p in t
        for p in [
            "what's on my list",
            "whats on my list",
            "my tasks",
            "my to do",
            "my todo",
            "what do i need to do",
            "open tasks",
            "task list",
        ]
    ):
        return {"action": "check_tasks"}

    # Usage / cost check
    if any(
        p in t
        for p in [
            "usage",
            "how much have you cost",
            "how much am i spending",
            "what's the cost",
            "whats the cost",
            "api cost",
            "token usage",
            "how expensive",
            "what's my bill",
        ]
    ):
        return {"action": "check_usage"}

    return None  # Everything else goes to the LLM for conversational routing
