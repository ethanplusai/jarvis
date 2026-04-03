import calendar_access


def test_parse_calendar_accounts_treats_auto_as_empty():
    assert calendar_access._parse_calendar_accounts("auto") == []
    assert calendar_access._parse_calendar_accounts(" AUTO ") == []


def test_parse_calendar_accounts_keeps_named_calendars():
    assert calendar_access._parse_calendar_accounts("Work, Personal") == [
        "Work",
        "Personal",
    ]


def test_configure_calendar_accounts_resets_auto_discovery_state():
    calendar_access.USER_CALENDARS = ["Old"]
    calendar_access._auto_discovered = True
    calendar_access._event_cache = [{"title": "Old event"}]
    calendar_access._cache_time = 123.0

    calendar_access.configure_calendar_accounts("auto")

    assert calendar_access.USER_CALENDARS == []
    assert calendar_access._auto_discovered is False
    assert calendar_access._event_cache == []
    assert calendar_access._cache_time == 0.0
