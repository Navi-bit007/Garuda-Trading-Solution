import time

from app.execution.runtime import TradingRuntime


class FakePipeline:
    def __init__(self):
        self.scanner = type("Scanner", (), {"token_to_symbol": {1: "AAA"}})()
        self.sync_calls = 0

    def sync_broker_positions(self):
        self.sync_calls += 1
        return []

    def on_ticks(self, ticks):
        return []


class FakeStream:
    def __init__(self):
        self.started = False
        self.stopped = False

    def start(self, tokens, on_ticks, **callbacks):
        self.started = True

    def stop(self):
        self.stopped = True


def test_runtime_periodically_reconciles_broker_positions():
    pipeline = FakePipeline()
    stream = FakeStream()
    runtime = TradingRuntime(pipeline, stream, reconcile_interval_seconds=0.05)

    runtime.start()
    try:
        deadline = time.monotonic() + 2.0
        while pipeline.sync_calls < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        runtime.stop()

    assert stream.started
    assert pipeline.sync_calls >= 2
    assert stream.stopped


def test_runtime_stop_joins_reconcile_thread():
    pipeline = FakePipeline()
    stream = FakeStream()
    runtime = TradingRuntime(pipeline, stream, reconcile_interval_seconds=0.05)

    runtime.start()
    runtime.stop()

    assert runtime._reconcile_thread is None
