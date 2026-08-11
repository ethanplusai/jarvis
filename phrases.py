"""
JARVIS fixed phrases — the lines JARVIS says that no LLM wrote.

Acknowledgements, lookup results and error lines are built in code, so they
stayed English no matter which language the user had configured: a reply would
arrive as "Checking your calendar now, sir." followed by fluent Italian.

Rather than ship hand-written translations for every language the panel offers
— inventing Japanese butler register is not something to do blind — the
English catalogue below is translated once per language by the same model
JARVIS already talks through, and cached on disk. The cache is keyed by a hash
of the catalogue, so editing any phrase re-translates rather than serving a
stale set.

Until a translation exists the English original is used, which keeps a first
run talking rather than silent.
"""

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path

log = logging.getLogger("jarvis.phrases")

_CACHE_DIR = Path(__file__).parent / "data"

# Placeholders are named, never positional, so a translator can reorder them
# freely — word order is exactly what changes between languages.
CATALOGUE: dict[str, str] = {
    # Acknowledgements — spoken while something slower happens
    "ack.generic": "Right away, {honorific}.",
    "ack.on_it": "On it, {honorific}.",
    "ack.understood": "Understood, {honorific}.",
    "ack.cancelled": "Cancelled, {honorific}.",
    "ack.looking_into": "Looking into that now, {honorific}.",
    "ack.taking_look": "Taking a look now, {honorific}.",
    "ack.checking_calendar": "Checking your calendar now, {honorific}.",
    "ack.checking_mail": "Checking your inbox now, {honorific}.",
    "ack.building": "Building it now, {honorific}.",
    "ack.connecting": "Connecting to {project} now, {honorific}.",

    # Modes
    "mode.work_self": "Work mode active in my own repo, {honorific}. Tell me what needs fixing.",
    "mode.back_to_chat": "Back to conversation mode, {honorific}.",
    "mode.already_chat": "Already in conversation mode, {honorific}.",

    # Build and project status
    "build.none_recent": "No recent builds on record, {honorific}.",
    "build.still_working": "Still working on {project}, {honorific}. Been at it for {seconds} seconds.",
    "build.problems": "{project} ran into problems, {honorific}.",
    "build.status": "{project} is {status}, {honorific}.",
    "build.finished": "{honorific}, {project} finished. Here's the gist: {summary}",
    "build.done": "{honorific}, {project} is done. {summary}",
    "build.issue": "{honorific}, I ran into an issue with {project}. {detail}",
    "build.no_directory": "Couldn't find the {project} project directory, {honorific}.",
    "build.connect_trouble": "Had trouble connecting to {project}, {honorific}.",
    "build.work_complete": "Work is complete, {honorific}.",

    # Research
    "research.complete": "Research is complete, {honorific}. The report is open in your browser.",
    "research.timed_out": "Research timed out, {honorific}.",
    "research.declined": "I'm not able to research that one, {honorific}.",
    "research.empty": "That research came back empty, {honorific}.",

    # Lookups
    "lookup.slow": "That {kind} check is taking too long, {honorific}. The data may still be syncing.",

    # Calendar
    "calendar.clear": "Your schedule is clear today, {honorific}.",
    "calendar.one_all_day": "You have one all-day event: {title}.",
    "calendar.one_event": "You have one event: {title} at {time}.",

    # Mail
    "mail.clear": "Inbox is clear, {honorific}. No unread messages.",
    "mail.one_account": "You have {total} unread in {detail}.",
    "mail.many_accounts": "You have {total} unread messages: {detail}.",

    # Notes
    "note.says": "{honorific}, your note '{title}' says: {body}",
    "note.not_found": "Couldn't find a note matching '{query}', {honorific}.",

    # Failures
    "error.generic": "Something went wrong, {honorific}.",
}

_active_lang = "en"
_active: dict[str, str] = {}


def _catalogue_hash() -> str:
    blob = json.dumps(CATALOGUE, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _cache_path(lang: str) -> Path:
    return _CACHE_DIR / f"phrases-{lang}.json"


def phrase(key: str, **kwargs) -> str:
    """The phrase for `key`, in the active language, with placeholders filled.

    Falls back to English whenever a translation is missing or malformed, so a
    bad translation degrades to the original rather than to a crash.
    """
    template = _active.get(key) or CATALOGUE.get(key, "")
    if not template:
        log.warning(f"Unknown phrase key: {key}")
        return ""
    kwargs.setdefault("honorific", os.getenv("HONORIFIC", "sir").strip() or "sir")
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError) as e:
        # A translation that dropped or renamed a placeholder must not take the
        # line down with it.
        log.warning(f"Phrase {key} failed to format ({e}); using English")
        try:
            return CATALOGUE[key].format(**kwargs)
        except Exception:
            return CATALOGUE.get(key, "")


def load_cached(lang: str) -> bool:
    """Load a cached translation for `lang`. Returns whether one was usable."""
    global _active, _active_lang
    _active_lang = lang
    if lang.startswith("en"):
        _active = {}
        return True

    path = _cache_path(lang)
    if not path.exists():
        _active = {}
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"Could not read phrase cache for {lang}: {e}")
        _active = {}
        return False

    if data.get("source_hash") != _catalogue_hash():
        log.info(f"Phrase cache for {lang} is stale — will re-translate")
        _active = {}
        return False

    # Validate on the way in as well as on the way out. A phrase missing a
    # placeholder does not raise — str.format ignores surplus arguments — so it
    # would quietly drop the project name or the count instead of failing
    # loudly. Cache files can be edited, copied between machines, or truncated.
    _active = {}
    for key, text in (data.get("phrases") or {}).items():
        english = CATALOGUE.get(key)
        if english is None or not isinstance(text, str):
            continue
        if _placeholders(text) != _placeholders(english):
            log.warning(f"Cached phrase {key} has the wrong placeholders; using English")
            continue
        _active[key] = text
    return bool(_active)


async def translate(lang: str, language_name: str, client) -> bool:
    """Translate the catalogue into `lang` and cache it. Returns success."""
    global _active
    if lang.startswith("en"):
        _active = {}
        return True

    prompt = (
        f"Translate these interface strings into {language_name}.\n\n"
        "They are spoken aloud by JARVIS, a British butler AI: dry, economical, "
        "never chatty. Match that register in the target language.\n\n"
        "Rules:\n"
        "- Return ONLY a JSON object with exactly the same keys.\n"
        "- Keep every {placeholder} verbatim, including the braces. Reorder them "
        "freely to suit the language, but never rename, drop or add one.\n"
        "- {honorific} is the user's chosen form of address. Leave it as the "
        "placeholder; do not replace it with a word.\n"
        "- Keep them short. These are spoken, not written.\n\n"
        f"{json.dumps(CATALOGUE, ensure_ascii=False, indent=1)}"
    )

    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        translated = json.loads(raw)
    except Exception as e:
        log.warning(f"Phrase translation for {lang} failed: {e}")
        return False

    # Keep only keys we asked for, and only where every placeholder survived.
    # A phrase that lost one would raise at format time on some future call
    # with no way to see it coming.
    clean = {}
    for key, english in CATALOGUE.items():
        candidate = translated.get(key)
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        expected = _placeholders(english)
        if _placeholders(candidate) != expected:
            log.warning(f"Phrase {key} lost a placeholder in {lang}; keeping English")
            continue
        clean[key] = candidate.strip()

    if not clean:
        return False

    _active = clean
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(lang).write_text(
            json.dumps({"source_hash": _catalogue_hash(), "phrases": clean},
                       ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
    except OSError as e:
        log.warning(f"Could not cache phrases for {lang}: {e}")

    log.info(f"Translated {len(clean)}/{len(CATALOGUE)} phrases into {language_name}")
    return True


def _placeholders(text: str) -> set[str]:
    import re
    return set(re.findall(r"\{(\w+)\}", text))


async def ensure_language(lang: str, language_name: str, client) -> None:
    """Make `lang` the active language, translating it if not already cached."""
    if load_cached(lang):
        return
    if client is None:
        return
    await translate(lang, language_name, client)
