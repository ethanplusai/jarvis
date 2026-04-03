import server


def test_detect_action_fast_handles_what_is_on_my_screen():
    assert server.detect_action_fast("what is on my screen") == {"action": "describe_screen"}


def test_detect_action_fast_handles_tell_me_what_is_on_my_screen():
    assert server.detect_action_fast("tell me what is on my screen") == {"action": "describe_screen"}
