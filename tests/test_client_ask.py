"""Client.ask (#49): the add-on's own request to Homecraft (homecraftcz/homecraft#944)
against a fake Homecraft: reply, error, timeout, disconnect, pause."""

import asyncio
import uuid

import aiohttp
import pytest
from aiohttp import web
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from connector.client import AskError, Client


def run(on_frame, scenario, paused=False):
    """A fake Homecraft calls `on_frame(ws, frame)` for each frame the agent sends
    after the hello; `scenario(agent)` runs once the agent is connected."""

    async def go():
        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_json({"op": "challenge", "nonce": "AAAA"})
            assert (await ws.receive_json())["op"] == "hello"
            await ws.send_json({"id": 1, "op": "updates.set", "items": []})  # = accepted
            async for msg in ws:
                frame = msg.json()
                if frame.get("id") != 1:
                    await on_frame(ws, frame)
            return ws

        app = web.Application()
        app.router.add_get("/ws", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        agent = Client(
            f"ws://127.0.0.1:{port}/ws", uuid.uuid4(), Ed25519PrivateKey.generate(), "t", paused
        )
        async with aiohttp.ClientSession() as session:
            conn = asyncio.create_task(agent._connect_once(session))
            for _ in range(500):
                if agent.connected:
                    break
                await asyncio.sleep(0.01)
            try:
                return await scenario(agent)
            finally:
                agent.reconnect_now()
                await asyncio.wait_for(conn, 5)
                await runner.cleanup()

    return asyncio.run(go())


def test_reply_with_a_result():
    frames = []

    async def on_frame(ws, frame):
        frames.append(frame)
        await ws.send_json(
            {"op": "reply", "rid": ["x"], "ok": True, "result": 1}
        )  # not a rid: ignored
        await ws.send_json({"op": "reply", "rid": "other", "ok": True, "result": 2})
        await ws.send_json({"op": "reply", "rid": frame["rid"], "ok": True, "result": {"s": 1}})

    async def scenario(agent):
        result = await agent.ask("heating.save", {"instance": "c1", "op": "x", "rid": "y"})
        return result, agent._conn.pending

    result, pending = run(on_frame, scenario)
    assert result == {"s": 1}
    assert pending == {}
    [frame] = frames
    assert frame["op"] == "heating.save" and frame["instance"] == "c1"
    assert len(frame["rid"]) == 16 and int(frame["rid"], 16) >= 0  # the payload cannot set them


def test_reply_with_an_error():
    async def on_frame(ws, frame):
        error = {"code": "blocked", "message": "Technik má rozpracované změny."}
        await ws.send_json({"op": "reply", "rid": frame["rid"], "ok": False, "error": error})

    async def scenario(agent):
        with pytest.raises(AskError) as e:
            await agent.ask("heating.save", {})
        return e.value

    e = run(on_frame, scenario)
    assert (e.code, e.message) == ("blocked", "Technik má rozpracované změny.")


def test_malformed_error_reply_is_internal():
    async def on_frame(ws, frame):
        await ws.send_json({"op": "reply", "rid": frame["rid"], "ok": "yes", "error": "x"})

    async def scenario(agent):
        with pytest.raises(AskError) as e:
            await agent.ask("heating.save", {})
        return e.value.code

    assert run(on_frame, scenario) == "internal"


def test_no_reply_in_time_is_unknown():
    """Sent, but no answer: it may have happened (not an unknown op, the hub
    answers those at once)."""

    async def on_frame(ws, frame):
        pass

    async def scenario(agent):
        with pytest.raises(AskError) as e:
            await agent.ask("heating.save", {}, timeout=0.2)
        return e.value.code, agent._conn.pending

    assert run(on_frame, scenario) == ("unknown", {})


def test_disconnect_after_sending_is_unknown():
    async def on_frame(ws, frame):
        await ws.close()

    async def scenario(agent):
        with pytest.raises(AskError) as e:
            await agent.ask("heating.save", {}, timeout=5)
        return e.value.code

    assert run(on_frame, scenario) == "unknown"


def test_send_failure_is_offline_and_leaves_no_future():
    """Not sent: offline; the future is cancelled, never left with an unread exception."""

    async def on_frame(ws, frame):
        pass

    async def scenario(agent):
        futures = []
        conn = agent._conn

        async def broken(data):
            futures.extend(conn.pending.values())
            raise ConnectionResetError

        conn.send = broken
        with pytest.raises(AskError) as e:
            await agent.ask("heating.save", {})
        return e.value.code, conn.pending, futures

    code, pending, [fut] = run(on_frame, scenario)
    assert (code, pending, fut.cancelled()) == ("offline", {}, True)


def test_payload_cannot_set_id():
    frames = []

    async def on_frame(ws, frame):
        frames.append(frame)
        await ws.send_json({"op": "reply", "rid": frame["rid"], "ok": True, "result": None})

    async def scenario(agent):
        return await agent.ask("heating.save", {"id": 7, "instance": "c1"})

    assert run(on_frame, scenario) is None
    assert "id" not in frames[0] and frames[0]["instance"] == "c1"


def test_paused_sends_nothing():
    frames = []

    async def on_frame(ws, frame):
        frames.append(frame)

    async def scenario(agent):
        with pytest.raises(AskError) as e:
            await agent.ask("heating.save", {})
        return e.value.code

    assert run(on_frame, scenario, paused=True) == "paused"
    assert frames == []


def test_offline_without_a_connection(agent):
    with pytest.raises(AskError) as e:
        asyncio.run(agent.ask("heating.save", {}))
    assert e.value.code == "offline"
