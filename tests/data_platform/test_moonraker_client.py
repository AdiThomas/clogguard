import asyncio
import logging

import pytest

from clogguard.moonraker import (
    MoonrakerClient,
    MoonrakerConnectionError,
    MoonrakerError,
    MoonrakerTimeoutError,
    get_path,
    merge_status,
)
from tests.data_platform.fake_moonraker import FakeMoonraker


@pytest.fixture
async def server():
    s = FakeMoonraker(
        {
            "extruder": {"temperature": 209.5, "target": 210.0, "power": 0.4},
            "print_stats": {"state": "standby", "filename": "", "info": {"current_layer": None}},
        }
    )
    await s.start()
    yield s
    await s.stop()


@pytest.fixture
async def client(server):
    c = MoonrakerClient(server.url, api_key_env=None, backoff_initial_s=0.05, backoff_max_s=0.2)
    await c.connect()
    yield c
    await c.close()


async def _next(it, timeout=2.0):
    return await asyncio.wait_for(it.__anext__(), timeout)


def test_merge_status_deep():
    status = {"extruder": {"temperature": 200.0, "target": 210.0}, "toolhead": {"position": [0]}}
    diff = {"extruder": {"temperature": 205.0}, "toolhead": {"position": [1, 2, 3]}, "fan": {}}
    merge_status(status, diff)
    assert status["extruder"] == {"temperature": 205.0, "target": 210.0}
    assert status["toolhead"]["position"] == [1, 2, 3]
    diff["toolhead"]["position"].append(4)  # merged copy must not alias the diff
    assert status["toolhead"]["position"] == [1, 2, 3]
    assert get_path(status, "toolhead", "position", 2) == 3
    assert get_path(status, "toolhead", "position", 9) is None
    assert get_path(status, "nope", "x") is None
    assert get_path(status, "extruder", "temperature", "x") is None


async def test_identify_and_call(server, client):
    assert client.connected
    assert client.connection_id == 1
    assert server.identify_params[0]["type"] == "agent"
    assert "api_key" not in server.identify_params[0]
    assert await client.call("printer.gcode.script", {"script": "M115"}) == "ok"


async def test_ids_matched_out_of_order(client):
    slow = asyncio.create_task(client.call("test.delay", {"delay": 0.3, "value": "slow"}))
    fast = asyncio.create_task(client.call("test.delay", {"delay": 0.0, "value": "fast"}))
    done, _ = await asyncio.wait({slow, fast}, return_when=asyncio.FIRST_COMPLETED)
    assert done == {fast}
    assert fast.result() == "fast"
    assert await slow == "slow"


async def test_rpc_error_and_timeout(client):
    with pytest.raises(MoonrakerError) as ei:
        await client.call("no.such.method", {})
    assert ei.value.code == -32601
    assert ei.value.method == "no.such.method"
    with pytest.raises(MoonrakerTimeoutError):
        await client.call("test.never", {}, timeout=0.1)


async def test_api_key_sent_never_logged(server, monkeypatch, caplog):
    monkeypatch.setenv("CG_TEST_KEY", "s3cret-key-123")
    caplog.set_level(logging.DEBUG, logger="clogguard")
    c = MoonrakerClient(server.url, api_key_env="CG_TEST_KEY")
    await c.connect()
    await c.call("printer.gcode.script", {"script": "M115"})
    await c.close()
    assert server.identify_params[-1]["api_key"] == "s3cret-key-123"
    assert "s3cret-key-123" not in caplog.text
    assert "s3cret-key-123" not in repr(vars(c))


async def test_subscribe_and_notifications(server, client):
    status = await client.subscribe({"extruder": ["temperature", "power"], "print_stats": None})
    assert status["extruder"] == {"temperature": 209.5, "power": 0.4}
    assert status["print_stats"]["state"] == "standby"
    updates = client.updates()
    await server.notify({"extruder": {"temperature": 210.1}})
    diff, et = await _next(updates)
    assert diff == {"extruder": {"temperature": 210.1}}
    assert et == pytest.approx(1000.1)
    merge_status(status, diff)
    assert status["extruder"]["temperature"] == 210.1


async def test_klippy_events(server, client):
    seen = []
    client.on_klippy_state(seen.append)
    events = client.klippy_events()
    await server.klippy("notify_klippy_shutdown")
    assert await _next(events) == "shutdown"
    await server.klippy("notify_klippy_ready")
    assert await _next(events) == "ready"
    assert client.klippy_state == "ready"
    assert seen == ["shutdown", "ready"]


async def test_reconnect_resubscribes(server, client):
    await client.subscribe({"extruder": ["temperature"]})
    updates = client.updates()
    server.status["extruder"]["temperature"] = 215.0
    await server.drop_all()
    await asyncio.sleep(0.01)
    assert not client.connected  # backoff (50 ms) has not elapsed yet
    await client.wait_connected(timeout=3.0)
    assert client.reconnects == 1
    assert len(server.subscriptions) == 2
    assert server.subscriptions[-1] == {"extruder": ["temperature"]}
    diff, _ = await _next(updates)  # full status re-pushed after resubscribe
    assert diff == {"extruder": {"temperature": 215.0}}
    assert await client.call("printer.gcode.script", {"script": "M115"}) == "ok"


async def test_pending_call_fails_on_drop(server, client):
    task = asyncio.create_task(client.call("test.never", {}, timeout=5))
    await asyncio.sleep(0.05)
    await server.drop_all()
    with pytest.raises(MoonrakerConnectionError):
        await task


async def test_close_ends_iterators(client):
    updates = client.updates()
    await client.close()
    with pytest.raises(StopAsyncIteration):
        await _next(updates)
    assert not client.connected
    with pytest.raises(MoonrakerConnectionError):
        await client.call("x", {})


async def test_connect_failure_and_retry(server):
    bad = MoonrakerClient("ws://127.0.0.1:1/websocket", api_key_env=None, open_timeout_s=1)
    with pytest.raises(OSError):
        await bad.connect()
    with pytest.raises(ValueError):
        await MoonrakerClient(None).connect()

    # retry=True keeps trying until the server appears
    late = FakeMoonraker()
    url = await late.start()
    await late.stop()
    c = MoonrakerClient(url, api_key_env=None, backoff_initial_s=0.05)
    task = asyncio.create_task(c.connect(retry=True))
    await asyncio.sleep(0.15)
    assert not task.done()
    port = int(url.rsplit(":", 1)[1].split("/")[0])
    from websockets.asyncio.server import serve

    async with serve(late._handler, "127.0.0.1", port, compression=None):
        await asyncio.wait_for(task, 3)
        assert c.connected
        await c.close()


async def test_no_reconnect_mode(server):
    c = MoonrakerClient(server.url, api_key_env=None, reconnect=False)
    async with c:
        updates = c.updates()
        await server.drop_all()
        with pytest.raises(StopAsyncIteration):
            await _next(updates)
        assert not c.connected
