import json

import server


def test_format_location_label_uses_city_and_region():
    assert server._format_location_label("Edmonton", "AB", "Canada") == "Edmonton, AB"


def test_get_weather_location_prefers_env_override(monkeypatch):
    monkeypatch.setenv("WEATHER_LOCATION_LABEL", "Edmonton, AB")
    monkeypatch.setenv("WEATHER_LATITUDE", "53.5461")
    monkeypatch.setenv("WEATHER_LONGITUDE", "-113.4938")

    location = server._get_weather_location()

    assert location == {
        "latitude": 53.5461,
        "longitude": -113.4938,
        "label": "Edmonton, AB",
    }


def test_fetch_weather_sync_reports_celsius(monkeypatch):
    monkeypatch.setattr(
        server,
        "_get_weather_location",
        lambda: {"latitude": 53.5461, "longitude": -113.4938, "label": "Edmonton, AB"},
    )

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"current": {"temperature_2m": 12.7}}).encode()

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: _FakeResponse())

    text = server._fetch_weather_sync()

    assert text == "Current weather in Edmonton, AB: 12.7°C"
