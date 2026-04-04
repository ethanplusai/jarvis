"""Tests for _generate_project_name in actions.py."""

from actions import _generate_project_name


class TestGenerateProjectName:
    """Test kebab-case project name generation from prompts."""

    def test_quoted_name(self):
        result = _generate_project_name('build a "tiktok-analytics-dashboard"')
        assert result == "tiktok-analytics-dashboard"

    def test_quoted_name_with_spaces(self):
        result = _generate_project_name('create "My Cool Project"')
        assert result == "my-cool-project"

    def test_quoted_name_special_chars_stripped(self):
        result = _generate_project_name('build a "my_project!@#v2"')
        # Non-alphanumeric (except hyphens) stripped, spaces collapsed
        assert "!" not in result
        assert "@" not in result
        assert "#" not in result

    def test_called_pattern(self):
        result = _generate_project_name("build a dashboard called my-app")
        assert result == "my-app"

    def test_named_pattern(self):
        result = _generate_project_name("create something named cool-tool")
        assert result == "cool-tool"

    def test_called_short_name_falls_through(self):
        # "foo" is 3 chars, len > 3 is False, so it falls through to keyword extraction
        result = _generate_project_name("build a thing called foo")
        assert result != "foo"  # Too short, should fall through

    def test_keyword_extraction(self):
        result = _generate_project_name("build a real estate listing aggregator")
        assert "real" in result
        assert "estate" in result
        assert "listing" in result
        assert "aggregator" in result
        assert result == "real-estate-listing-aggregator"

    def test_all_skip_words_returns_default(self):
        result = _generate_project_name("build me a new simple web page")
        assert result == "jarvis-project"
