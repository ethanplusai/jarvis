import asyncio
import logging

import server


class _FakeWS:
    def __init__(self):
        self.messages = []

    async def send_json(self, payload):
        self.messages.append(payload)


def test_debug_log_stream_sends_snapshot_and_live_entries():
    ws = _FakeWS()
    stream = server.DebugLogStream(max_entries=10)

    async def run_test():
        stream.set_loop(asyncio.get_running_loop())
        await stream.subscribe(ws)

        record = logging.LogRecord(
            name="jarvis.debug",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="live test message",
            args=(),
            exc_info=None,
        )
        stream.publish(record)
        await asyncio.sleep(0)

    asyncio.run(run_test())

    assert ws.messages[0]["type"] == "debug_log_snapshot"
    assert ws.messages[0]["entries"] == []
    assert ws.messages[1]["type"] == "debug_log"
    assert ws.messages[1]["entry"]["logger"] == "jarvis.debug"
    assert ws.messages[1]["entry"]["message"] == "live test message"
