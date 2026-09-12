import sys
from types import SimpleNamespace

from app.broker.tick_stream import KiteTickStream


def test_tick_stream_subscribes_sorted_unique_tokens(monkeypatch):
    state = {}

    class FakeTicker:
        MODE_FULL = "full"

        def __init__(self, api_key, access_token):
            state["credentials"] = (api_key, access_token)
            state["instance"] = self

        def connect(self, threaded):
            state["threaded"] = threaded
            self.on_connect(self, {"connected": True})
            self.on_ticks(self, [{"instrument_token": 1}])
            self.on_error(self, 500, "test error")
            self.on_reconnect(self, 1)
            self.on_noreconnect(self)

        def subscribe(self, tokens):
            state["tokens"] = tokens

        def set_mode(self, mode, tokens):
            state["mode"] = (mode, tokens)

        def close(self):
            state["closed"] = True

    monkeypatch.setitem(sys.modules, "kiteconnect", SimpleNamespace(KiteTicker=FakeTicker))
    ticks = []
    connections = []
    errors = []
    reconnects = []
    stopped = []
    stream = KiteTickStream("key", "token")
    stream.start(
        [3, 1, 3],
        ticks.append,
        on_connect=connections.append,
        on_error=errors.append,
        on_reconnect=reconnects.append,
        on_noreconnect=stopped.append,
    )
    stream.stop()

    assert state["credentials"] == ("key", "token")
    assert state["tokens"] == [1, 3]
    assert state["mode"] == ("full", [1, 3])
    assert state["threaded"] is True
    assert ticks == [[{"instrument_token": 1}]]
    assert connections == [{"connected": True}]
    assert errors == [{"code": 500, "reason": "test error"}]
    assert reconnects == [{"attempts": 1}]
    assert stopped == [{}]
    assert state["closed"] is True