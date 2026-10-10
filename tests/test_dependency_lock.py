import pytest
import scripts.lock_dependencies as lock
from scripts.lock_dependencies import parse


def test_normalizes_exact_constraints():
    assert parse("# pins\nTyping_Extensions==4.16.0\nhttpx==0.28.1\n") == {
        "typing-extensions": "4.16.0", "httpx": "0.28.1"}


@pytest.mark.parametrize("text", ["httpx>=0.28", "httpx @ https://example/x.whl", "httpx==1\nHTTPX==2"])
def test_rejects_nonreproducible_constraints(text):
    with pytest.raises(ValueError):
        parse(text)


def test_refuses_an_interpreter_that_is_not_a_virtualenv(monkeypatch):
    """A shared Python carries tools of its own (pipx, on GitHub's Windows
    image), which the check would call unpinned. It names the problem instead."""
    monkeypatch.setattr(lock.sys, "base_prefix", lock.sys.prefix)
    monkeypatch.setattr(lock.sys, "argv", ["lock_dependencies.py"])
    with pytest.raises(SystemExit, match="not a virtualenv"):
        lock.main()
