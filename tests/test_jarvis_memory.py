import pytest
from pathlib import Path

import data_paths
import jarvis_memory as jm


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    data_paths.ensure_memory_layout()
    return data_paths.brain_home()


def test_the_layout_is_created_once_and_is_idempotent(home):
    for _ in range(2):
        data_paths.ensure_memory_layout()
    assert (home / "memory").is_dir()
    assert (home / "projects").is_dir()
    assert (home / "journal").is_dir()
    assert (home / "CLAUDE.md").exists(), "the persona is seeded by ensure_brain_home"


def test_a_memory_is_one_file_with_a_readable_name(home):
    path = jm.write_memory("Tony prefers Postgres over SQLite",
                           "He said so while we were fixing chitauri.")

    assert path.parent == home / "memory"
    assert path.name == "tony-prefers-postgres-over-sqlite.md"
    text = path.read_text(encoding="utf-8")
    assert "Tony prefers Postgres over SQLite" in text
    assert "chitauri" in text


def test_writing_the_same_title_twice_updates_rather_than_duplicating(home):
    jm.write_memory("A fact", "first version")
    p = jm.write_memory("A fact", "second version")

    assert len(jm.list_memories()) == 1
    assert "second version" in p.read_text(encoding="utf-8")


def test_slugify_makes_a_filename_out_of_anything_sayable(home):
    assert jm.slugify("Tony's 'chitauri' work — Q3/2026!") == "tonys-chitauri-work-q3-2026"
    assert jm.slugify("   ") == "note"
    assert len(jm.slugify("x" * 200)) <= 60


def test_reading_a_memory_that_does_not_exist_returns_none(home):
    assert jm.read_memory("nope") is None


def test_the_index_gains_one_line_per_memory(home):
    jm.add_to_index("Tony prefers Postgres", "said during chitauri work")
    jm.add_to_index("Hammer is the worker project", "not to be confused with hammer-private")

    lines = jm.index_lines()
    assert len(lines) == 2
    assert all(line.startswith("- [") for line in lines)
    assert "tony-prefers-postgres.md" in lines[0]


def test_the_index_does_not_duplicate_a_title(home):
    jm.add_to_index("A fact", "first hook")
    jm.add_to_index("A fact", "second hook")

    lines = jm.index_lines()
    assert len(lines) == 1
    assert "second hook" in lines[0], "the newer hook wins"


def test_a_full_index_is_reported_so_the_brain_can_be_asked_to_consolidate(home):
    for i in range(jm.MEMORY_INDEX_MAX):
        jm.add_to_index(f"Fact number {i}", "hook")
    assert jm.index_is_full() is True


def test_a_project_note_appends_rather_than_replacing(home):
    jm.write_project_note("chitauri", "Uses WordPress for the marketing site.")
    p = jm.write_project_note("chitauri", "The 301 redirect was fixed on the 2nd.")

    text = p.read_text(encoding="utf-8")
    assert "WordPress" in text and "301 redirect" in text
    assert text.index("WordPress") < text.index("301 redirect"), "chronological"


def test_a_project_note_is_named_for_the_project(home):
    p = jm.write_project_note("webapp-fresh", "note")
    assert p.name == "webapp-fresh.md"
    assert jm.read_project_note("webapp-fresh").endswith("note\n")


def test_reading_an_unknown_project_note_returns_none(home):
    assert jm.read_project_note("nope") is None


def test_a_journal_entry_is_timestamped_and_labelled(home):
    p = jm.write_journal("Worked on chitauri. Tony wants Postgres.", reason="rotation")

    assert p.parent == home / "journal"
    assert p.name.endswith("-rotation.md")
    assert "Postgres" in p.read_text(encoding="utf-8")


def test_the_latest_journal_is_the_most_recent_and_is_bounded(home):
    jm.write_journal("older entry", reason="shutdown")
    jm.write_journal("x" * 5000, reason="rotation")

    latest = jm.latest_journal(limit=1200)
    assert "older entry" not in latest
    assert len(latest) <= 1200


def test_latest_journal_is_none_when_nothing_has_been_written(home):
    assert jm.latest_journal() is None


def test_add_to_index_preserves_hand_written_prose(home):
    # add_to_index rewrites MEMORY.md; a user's own paragraph around the
    # generated lines must survive the round trip untouched.
    path = home / "MEMORY.md"
    path.write_text(
        "# What JARVIS remembers\n\n"
        "Tony asked me to always check the chitauri staging env before prod.\n\n"
        "- [Old fact](old-fact.md) — some hook\n"
    , encoding="utf-8")

    jm.add_to_index("New fact", "a hook")

    text = path.read_text(encoding="utf-8")
    assert "Tony asked me to always check the chitauri staging env before prod." in text
    assert "- [Old fact](old-fact.md) — some hook" in text
    assert "- [New fact](new-fact.md) — a hook" in text


def test_two_journal_entries_with_the_same_reason_in_one_minute_both_survive(home):
    # Two rotations can land in the same minute-resolution timestamp. Both
    # files must exist (no overwrite) and latest_journal() must return the
    # one written second, not first.
    p1 = jm.write_journal("first entry", reason="rotation")
    p2 = jm.write_journal("second entry", reason="rotation")

    assert p1 != p2
    assert p1.exists() and p2.exists()
    assert "first entry" in p1.read_text(encoding="utf-8")
    assert "second entry" in p2.read_text(encoding="utf-8")

    latest = jm.latest_journal()
    assert "second entry" in latest
    assert "first entry" not in latest


def test_latest_journal_is_correct_even_when_reasons_sort_out_of_order(home):
    # "rotation" < "shutdown" alphabetically, but shutdown is written first
    # here. Filename string-sort alone would return the wrong entry;
    # latest_journal must reflect actual write order, not label order.
    jm.write_journal("first, alphabetically later reason", reason="shutdown")
    jm.write_journal("second, alphabetically earlier reason", reason="rotation")

    latest = jm.latest_journal()
    assert "second, alphabetically earlier reason" in latest
    assert "first, alphabetically later reason" not in latest


def test_search_finds_a_memory_by_its_words(home):
    jm.write_memory("Tony prefers Postgres", "He said so during chitauri work.")
    jm.write_memory("Hammer is the worker project", "Runs on the Desktop.")

    hits = jm.search("postgres")

    assert hits and hits[0]["name"] == "tony-prefers-postgres"
    assert hits[0]["kind"] == "memory"
    assert "Postgres" in hits[0]["excerpt"]


def test_search_covers_memories_projects_and_journal(home):
    jm.write_memory("A memory", "mentions kestrel")
    jm.write_project_note("chitauri", "also mentions kestrel")
    jm.write_journal("the journal mentions kestrel too", reason="rotation")

    kinds = {h["kind"] for h in jm.search("kestrel", limit=10)}

    assert kinds == {"memory", "project", "journal"}


def test_a_title_match_outranks_a_body_match(home):
    jm.write_memory("Kestrel", "unrelated body")
    jm.write_memory("Something else", "the body mentions kestrel once")

    assert jm.search("kestrel")[0]["name"] == "kestrel"


def test_search_returns_nothing_for_an_unknown_word(home):
    jm.write_memory("A memory", "body")
    assert jm.search("nonexistentword") == []


def test_search_is_safe_on_an_empty_or_missing_folder(home):
    assert jm.search("anything") == []
    assert jm.search("") == []


def test_search_respects_the_limit(home):
    for i in range(10):
        jm.write_memory(f"Memory {i}", "all of them mention kestrel")
    assert len(jm.search("kestrel", limit=3)) == 3


def test_more_mentions_outranks_fewer_mentions(home):
    # Same title-relevance (no title match either way); the file that says
    # the word five times is almost certainly more on-topic than the one
    # that mentions it in passing.
    jm.write_memory("Frequent one", "kestrel kestrel kestrel kestrel kestrel")
    jm.write_memory("Rare one", "kestrel mentioned once here")

    assert jm.search("kestrel")[0]["name"] == "frequent-one"


def test_excerpt_is_never_a_bare_heading_marker(home):
    # A memory whose title is informative prose ("Kestrel" alone isn't, but
    # a real title usually is): the only line containing the query word is
    # the "# Title" heading. The excerpt must not carry the "#" marker.
    jm.write_memory("The kestrel nests on the ledge", "unrelated body")

    hit = jm.search("kestrel")[0]
    assert not hit["excerpt"].startswith("#")
    assert hit["excerpt"] == "The kestrel nests on the ledge"


def test_excerpt_does_not_just_echo_the_search_term_back(home):
    # A project note named exactly for the thing being searched: the query
    # word matches nowhere except the "# chitauri" heading, which is
    # identical to the file's own name and the query itself — echoing it
    # back as the excerpt tells the brain nothing it didn't already know.
    # A real line of the note's own content is a far better spoken excerpt.
    jm.write_project_note("chitauri", "Uses WordPress for the marketing site.")

    hit = jm.search("chitauri")[0]
    assert hit["excerpt"] != "chitauri"
    assert "WordPress" in hit["excerpt"]


def test_excerpt_from_a_project_note_drops_the_leading_timestamp(home):
    # write_project_note() prefixes every entry with "_stamp_ — "; that
    # prefix is metadata, not prose, and would sound like garbled noise if
    # spoken verbatim ("underscore 2026 dash..."). The excerpt should start
    # with the actual sentence.
    jm.write_project_note("chitauri", "Uses WordPress for the marketing site.")

    hit = jm.search("wordpress")[0]
    assert hit["excerpt"] == "Uses WordPress for the marketing site."


def test_a_query_of_only_stopwords_finds_nothing(home):
    jm.write_memory("A memory about the thing", "we did this and that")
    assert jm.search("what did we do") == []
    assert jm.search("the") == []


def test_a_stopword_query_with_one_real_word_still_finds_it(home):
    jm.write_project_note("chitauri", "Moved the database to Postgres.")
    hits = jm.search("what did we do on chitauri")
    assert hits and hits[0]["name"] == "chitauri"


# --- Finding 1: differently-titled memories must not clobber each other ---

def test_two_genuinely_different_titles_that_slugify_identically_both_survive(home):
    # Both titles are 60+ chars and share their first SLUG_MAX (60) chars,
    # so slugify() truncates them to the identical filename — but their
    # full titles (and content) are genuinely different, not a trivial
    # punctuation/case variant of each other.
    prefix = "x" * 60
    p1 = jm.write_memory(prefix + " first topic", "first body")
    p2 = jm.write_memory(prefix + " second topic", "second body")

    assert p1 != p2
    assert p1.exists() and p2.exists()
    assert "first body" in p1.read_text(encoding="utf-8")
    assert "second body" in p2.read_text(encoding="utf-8")
    assert len(jm.list_memories()) == 2


def test_the_same_title_written_twice_still_produces_one_file(home):
    p1 = jm.write_memory("Tony's DB choice", "uses postgres")
    p2 = jm.write_memory("Tony's DB choice", "uses sqlite")

    assert p1 == p2
    assert len(jm.list_memories()) == 1
    text = p1.read_text(encoding="utf-8")
    assert "sqlite" in text
    assert "postgres" not in text


def test_a_title_differing_only_by_apostrophe_or_case_is_the_same_memory(home):
    p1 = jm.write_memory("Tony's DB choice", "uses postgres")
    p2 = jm.write_memory("tonys db choice", "uses sqlite")

    assert p1 == p2
    assert len(jm.list_memories()) == 1
    text = p1.read_text(encoding="utf-8")
    assert "sqlite" in text
    assert "postgres" not in text


# --- Finding 2: latest_journal() must reflect write order, not mtime ---

def test_two_back_to_back_journal_entries_are_distinct_and_ordered(home):
    p1 = jm.write_journal("first entry", reason="rotation")
    p2 = jm.write_journal("second entry", reason="rotation")

    assert p1 != p2
    latest = jm.latest_journal()
    assert "second entry" in latest
    assert "first entry" not in latest


def test_editing_an_old_journal_entrys_mtime_does_not_change_the_latest(home):
    import os
    import time

    p1 = jm.write_journal("older entry", reason="shutdown")
    p2 = jm.write_journal("newer entry", reason="rotation")

    # Touch/rewrite the OLDER entry well after the newer one was written.
    time.sleep(0.01)
    p1.write_text(p1.read_text(encoding="utf-8") + "\ncorrected typo\n", encoding="utf-8")
    future = time.time() + 10_000
    os.utime(p1, (future, future))

    latest = jm.latest_journal()
    assert "newer entry" in latest
    assert "older entry" not in latest


def test_a_journal_file_with_an_unparseable_name_is_skipped_not_crashed(home):
    jm.write_journal("real entry", reason="shutdown")
    (data_paths.journal_dir() / "renamed-by-hand.md").write_text("# mystery\n\nstray note\n", encoding="utf-8")

    latest = jm.latest_journal()
    assert "real entry" in latest


# --- Finding 3: an excerpt line must actually match the query ---

def test_excerpt_prefers_a_matching_line_over_an_unrelated_first_line(home):
    jm.write_memory(
        "Random notes",
        "This is unrelated.\nzeltar!!! ???\nMore unrelated.",
    )
    hit = jm.search("zeltar")[0]
    assert "zeltar" in hit["excerpt"].lower()


def test_title_only_hit_still_falls_back_to_real_body_content(home):
    jm.write_project_note("chitauri", "Uses WordPress for the marketing site.")
    hit = jm.search("chitauri")[0]
    assert hit["excerpt"] != "chitauri"
    assert "WordPress" in hit["excerpt"]


def test_a_genuine_body_match_returns_the_matching_line_not_the_first_line(home):
    jm.write_memory("Notes", "First unrelated line.\nThe kestrel is fast.\nLast line.")
    hit = jm.search("kestrel")[0]
    assert hit["excerpt"] == "The kestrel is fast."


# --- Finding 4: excerpts read poorly for table rows and long URLs ---

def test_a_table_row_excerpt_collapses_pipes_into_commas(home):
    # Title deliberately doesn't contain the query word, so the matching
    # line is the table row itself, not the heading.
    jm.write_memory("Pricing sheet", "| zeltar pro | $99 |")
    hit = jm.search("zeltar")[0]
    assert "|" not in hit["excerpt"]
    assert "zeltar pro" in hit["excerpt"]
    assert "$99" in hit["excerpt"]


def test_a_long_url_excerpt_is_replaced_with_something_sayable(home):
    url = "https://example.com/very/long/path/that/goes/on/and/on?query=params&more=stuff"
    jm.write_memory("Reference doc", f"See {url} for zeltar details.")
    hit = jm.search("zeltar")[0]
    assert url not in hit["excerpt"]
    assert "example.com" in hit["excerpt"] or "a link" in hit["excerpt"]


def test_a_short_url_excerpt_is_left_alone(home):
    jm.write_memory("Reference note", "See http://x.co for zeltar details.")
    hit = jm.search("zeltar")[0]
    assert "http://x.co" in hit["excerpt"]


# --- Placeholder journal entries: written, but never carried forward ---

def test_a_placeholder_is_not_what_gets_carried_forward(home):
    jm.write_journal("the real handover", reason="shutdown")
    jm.write_journal("Session ended; the brain wrote no handover.",
                     reason="shutdown-silent")

    assert "the real handover" in jm.latest_journal()


def test_a_placeholder_only_journal_carries_nothing(home):
    jm.write_journal("Session ended; the brain wrote no handover.",
                     reason="shutdown-silent")

    assert jm.latest_journal() is None


def test_a_placeholder_is_still_written_to_disk(home):
    """Their purpose is to prove a generation ended rather than vanished, so
    they must stay on disk and stay readable — just never be handed on."""
    path = jm.write_journal("Session ended; the brain wrote no handover.",
                            reason="shutdown-silent")

    assert path.exists()
    assert "shutdown-silent" in path.name
    assert "wrote no handover" in jm.latest_journal(include_placeholders=True)


def test_the_placeholder_marker_lives_in_the_filename(home):
    """Decidable from a directory listing, and not lost to a hand-edit of the
    body — this folder is the user's to edit."""
    path = jm.write_journal("nothing to say", reason="rotation-silent")

    assert jm.journal_reason(path) == "rotation-silent"
    assert jm.is_placeholder_reason(jm.journal_reason(path))
    assert not jm.is_placeholder_reason("shutdown")
    assert not jm.is_placeholder_reason("rotation")


def test_a_collision_suffixed_placeholder_is_still_a_placeholder(home):
    """write_journal appends -2 to break a filename collision."""
    assert jm.is_placeholder_reason("shutdown-silent-2")
    assert not jm.is_placeholder_reason("milestone-3")


def test_journal_entries_are_ordered_by_the_filename_stamp(home):
    import os
    import time

    p1 = jm.write_journal("first", reason="shutdown")
    time.sleep(0.01)
    jm.write_journal("second", reason="rotation")
    future = time.time() + 10_000
    os.utime(p1, (future, future))        # mtime lies; the name does not

    stamps = [reason for _stamp, reason, _p in jm.journal_entries()]
    assert stamps == ["shutdown", "rotation"]


def test_an_empty_journal_folder_carries_nothing(home):
    assert jm.journal_entries() == []
    assert jm.latest_journal() is None


# --- the index and the folder can disagree ---------------------------------
#
# Measured live on 2026-09-22: four notes in memory/, written outside
# `tool_remember`, and a MEMORY.md still at its seeded header. The index is
# the ONLY thing that tells the brain a note exists at boot, so those four
# were invisible for five days and nothing said so.

def test_a_memory_with_no_index_line_is_reported_as_unindexed(home):
    jm.write_memory("StarNet station and how to work it", "Start with starnet_status.")
    jm.write_memory("Tony prefers Postgres", "said during chitauri work")
    jm.add_to_index("Tony prefers Postgres", "database preference")

    assert [e["slug"] for e in jm.unindexed_memories()] == \
        ["starnet-station-and-how-to-work-it"]


def test_reindex_gives_every_orphan_a_line_the_brain_will_see(home):
    jm.write_memory("StarNet station and how to work it",
                    "Start with starnet_status for health, model and roster.\n\n"
                    "The crew is a sales-pipeline team.")

    result = jm.reindex()

    assert result == {"indexed": ["starnet-station-and-how-to-work-it"],
                      "left_out": [], "full": False}
    assert jm.unindexed_memories() == []
    [entry] = jm.index_entries()
    assert entry["title"] == "StarNet station and how to work it"
    assert entry["hook"] == "Start with starnet_status for health, model and roster."


def test_reindex_hooks_are_one_short_line_never_the_title_or_the_stamp(home):
    jm.write_memory("A long story", "x" * 400)
    jm.reindex()
    [entry] = jm.index_entries()
    assert entry["hook"] != "A long story"
    assert len(entry["hook"]) <= jm.HOOK_MAX_CHARS + 1      # room for the ellipsis
    assert entry["hook"].endswith("…")


def test_reindex_is_idempotent_and_leaves_a_hand_written_hook_alone(home):
    jm.write_memory("A fact", "first")
    jm.add_to_index("A fact", "hand-written hook")

    assert jm.reindex() == {"indexed": [], "left_out": [], "full": False}
    assert jm.index_entries()[0]["hook"] == "hand-written hook"


def test_reindex_stops_at_the_cap_and_says_who_was_left_out(home):
    for i in range(jm.MEMORY_INDEX_MAX):
        jm.add_to_index(f"Fact number {i}", "hook")
    jm.write_memory("One more", "body")

    result = jm.reindex()

    assert result == {"indexed": [], "left_out": ["one-more"], "full": True}


def test_reindex_never_points_the_index_at_a_file_it_cannot_name(home):
    """Two titles, one slug (see `write_memory`'s collision rule): the second
    file is `<slug>-2.md`, and `add_to_index` would name `<slug>.md` for it.
    An index line that names the wrong file is worse than no line."""
    prefix = "x" * jm.SLUG_MAX
    jm.write_memory(prefix + " first topic", "first body")
    jm.write_memory(prefix + " second topic", "second body")

    result = jm.reindex()

    assert result["indexed"] == [prefix]
    assert result["left_out"] == [prefix + "-2"]
    files = set(jm.list_memories())
    assert all(e["slug"] in files for e in jm.index_entries())


# --- project notes are read, not only written ------------------------------

def test_project_history_reads_the_notes_back_oldest_first(home):
    import re
    jm.write_project_note("chitauri", "Uses WordPress.")
    jm.write_project_note("chitauri", "301 fixed.")

    text = jm.project_history("chitauri")

    assert text.index("WordPress") < text.index("301 fixed")
    assert "_" not in text, "a stamp is said as a date, not as markdown"
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} — Uses WordPress\.", text)


def test_project_history_is_none_for_a_project_never_noted(home):
    assert jm.project_history("nope") is None


def test_project_history_keeps_the_newest_lines_when_it_must_cut(home):
    import re
    for i in range(200):
        jm.write_project_note("big", f"note number {i}")

    text = jm.project_history("big", limit=600)

    assert len(text) <= 600
    assert "note number 199" in text
    assert not re.search(r"note number 0\b", text)


def test_project_names_are_the_titles_of_the_note_files(home):
    jm.write_project_note("chitauri", "x")
    jm.write_project_note("Hammer", "y")
    assert sorted(jm.project_names()) == ["Hammer", "chitauri"]


def test_nothing_before_a_fresh_start_is_carried(home):
    """"Start fresh" leaves a wall in the journal: no later start — this
    process's, or the next — reads past it."""
    jm.write_journal("before the fresh start", reason="rotation")
    jm.write_journal("the wall", reason=jm.FRESH_START_REASON)
    assert jm.latest_journal() is None
    jm.write_journal("after it", reason="rotation")
    assert "after it" in jm.latest_journal()
    assert jm.is_placeholder_reason(jm.FRESH_START_REASON)



def test_the_dashboard_marks_what_a_new_brain_will_carry_after_a_fresh_start(home):
    jm.write_journal("before", reason="rotation")
    jm.write_journal("the wall", reason=jm.FRESH_START_REASON)
    assert jm.latest_journal_slug() is None
    jm.write_journal("after", reason="rotation")
    assert jm.latest_journal_slug().endswith("-rotation")
