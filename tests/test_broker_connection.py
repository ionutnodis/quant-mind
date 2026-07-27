import pytest

from quantmind.broker.base import ExecutionDisabledError
from quantmind.broker.connection import ConnectionManager


class FakeIB:
    """Test double for ib_async.IB: fails to connect `fail_times` times, then succeeds."""

    def __init__(self, fail_times=0):
        self.fail_times = fail_times
        self.connect_calls = []  # (host, port, client_id) per attempt
        self._connected = False

    def isConnected(self):
        return self._connected

    async def connectAsync(self, host, port, clientId, readonly=False):
        self.connect_calls.append((host, port, clientId))
        self.readonly_flags = getattr(self, "readonly_flags", []) + [readonly]
        if len(self.connect_calls) <= self.fail_times:
            raise ConnectionRefusedError("gateway not ready")
        self._connected = True

    def drop(self):
        self._connected = False


class FakeSleeper:
    def __init__(self):
        self.delays = []

    async def __call__(self, seconds):
        self.delays.append(seconds)


async def test_connects_first_try_no_backoff():
    ib, sleeper = FakeIB(), FakeSleeper()
    mgr = ConnectionManager(ib, host="127.0.0.1", port=4002, client_id=17, sleep=sleeper)
    await mgr.ensure_connected()
    assert ib.isConnected()
    assert sleeper.delays == []


async def test_retries_with_exponential_backoff_and_fixed_client_id():
    ib, sleeper = FakeIB(fail_times=3), FakeSleeper()
    mgr = ConnectionManager(ib, host="127.0.0.1", port=4002, client_id=17, sleep=sleeper, backoff_base=1.0)
    await mgr.ensure_connected()
    assert ib.isConnected()
    assert sleeper.delays == [1.0, 2.0, 4.0]
    assert all(call == ("127.0.0.1", 4002, 17) for call in ib.connect_calls)


async def test_backoff_capped_at_max():
    ib, sleeper = FakeIB(fail_times=8), FakeSleeper()
    mgr = ConnectionManager(
        ib, host="h", port=1, client_id=17, sleep=sleeper, backoff_base=1.0, backoff_max=10.0, max_attempts=20
    )
    await mgr.ensure_connected()
    assert max(sleeper.delays) == 10.0


async def test_gives_up_after_max_attempts_with_clear_error():
    ib, sleeper = FakeIB(fail_times=100), FakeSleeper()
    mgr = ConnectionManager(ib, host="h", port=1, client_id=17, sleep=sleeper, max_attempts=4)
    with pytest.raises(ConnectionError, match="4 attempts"):
        await mgr.ensure_connected()


async def test_reconnects_after_drop():
    ib, sleeper = FakeIB(), FakeSleeper()
    mgr = ConnectionManager(ib, host="h", port=1, client_id=17, sleep=sleeper)
    await mgr.ensure_connected()
    ib.drop()  # gateway nightly restart
    await mgr.ensure_connected()  # health probe before scheduled job
    assert ib.isConnected()
    assert len(ib.connect_calls) == 2


async def test_connects_readonly_never_requesting_order_access():
    # Live-account incident 2026-07-27: ib_async's default connect handshake
    # syncs order state (open/completed orders), which the Gateway classifies
    # as requiring full API access — with "Read-Only API" checked it pops a
    # "remove read-only?" dialog at the user on every connect. v1 is
    # read-only by design (Engineering Constraint: place_order disabled), so
    # every connect must pass readonly=True and skip the order sync entirely.
    ib, sleeper = FakeIB(), FakeSleeper()
    mgr = ConnectionManager(ib, host="127.0.0.1", port=4001, client_id=17, sleep=sleeper)
    await mgr.ensure_connected()
    assert ib.readonly_flags == [True]


def test_place_order_is_disabled_in_v1():
    from quantmind.broker.base import ReadOnlyBroker

    class Stub(ReadOnlyBroker):
        pass

    with pytest.raises(ExecutionDisabledError):
        Stub().place_order(object())
