"""Error reports to Homecraft (#31): allowlist, log rule, dedup, consent."""

import asyncio
import contextlib
import json
from datetime import UTC, datetime, timedelta

import aiohttp
import pytest
from aiohttp import web

from connector import audit, errors, settings

T0 = datetime(2026, 11, 23, 5, 30, 10, tzinfo=UTC)


def at(s: float) -> datetime:
    return T0 + timedelta(seconds=s)


# --- allowlist -------------------------------------------------------------


def test_clean_keeps_only_the_allowlist():
    r = errors.clean(
        {
            "feature": "heating",
            "version": "1.2.0",
            "kind": "domain",
            "code": "valve_target_not_reached",
            "entity_id": "climate.x",
            "node_id": 7,  # wrong type: left out
            "secret": "y",
            "count": 99,
            "first_seen": "x",
            "trace": [{"evt": "a", "nested": {"x": 1}}],
        }
    )
    assert r == {
        "feature": "heating",
        "version": "1.2.0",
        "kind": "domain",
        "code": "valve_target_not_reached",
        "entity_id": "climate.x",
        "trace": [{"evt": "a"}],
    }


@pytest.mark.parametrize(
    "data",
    [
        {"code": "c"},  # no kind
        {"kind": "log", "code": "c"},  # log only from system_log_event
        {"kind": "other", "code": "c"},
        {"kind": "domain"},  # no code
        {"kind": "domain", "code": 5},
        [],
        None,
    ],
)
def test_clean_drops(data):
    assert errors.clean(data) is None


def test_clean_defaults_and_circuit():
    assert errors.clean({"kind": "exception", "code": "c"})["feature"] == "unknown"
    circuit = lambda v: errors.clean({"kind": "domain", "code": "c", "circuit": v}).get("circuit")  # noqa: E731
    assert circuit(2) == 2
    assert circuit("x" * 40) == "x" * 32
    assert circuit(True) is None
    assert circuit(1.5) is None


def test_clean_truncates():
    r = errors.clean(
        {"kind": "domain", "code": "c" * 80, "message": "m" * 600, "source": "s" * 200}
    )
    assert len(r["message"]) == 500 and len(r["source"]) == 128 and len(r["code"]) == 64


def test_trace_limits():
    trace = [{"i": i} for i in range(80)]
    assert errors._trace(trace) == trace[-50:]
    assert errors._trace([{"a": [1], "b": {}}, "x", 3, {"c": None, 1: "k"}]) == [{"c": None}]
    assert errors._trace("nope") == []
    big = [{"i": i, "pad": "x" * 1000} for i in range(30)]
    out = errors._trace(big)
    assert len(json.dumps(out)) <= errors.MAX_TRACE_BYTES
    assert out == big[-len(out) :] and len(out) < 30


# --- system_log_event ------------------------------------------------------


def _packages(root):
    p = root / "packages"
    p.mkdir()
    (p / "hfb_heating_demo.yaml").write_text(
        "# ha-builder: heating@1.19.0-beta.5\n"
        "automation:\n  - id: heating_plans_sweep_circuit_demo\n    alias: x\n"
        "automation extra:\n  id: hfb_extra\n"
        "script:\n  heating_boost_circuit_demo:\n    sequence: []\n"
    )
    (p / "hfb_energy.yaml").write_text(
        "# ha-builder: energy@1.0.0\nautomation:\n  id: hfb_energy_tariff\n"
    )
    (p / "hfb_nomarker.yaml").write_text("automation:\n  - id: stray\n")
    (p / "hfb_broken.yaml").write_text("# ha-builder: broken@1.0.0\nautomation: [\n")
    (p / "own.yaml").write_text("# ha-builder: own@1.0.0\nautomation:\n  - id: mine\n")


def test_owned(tmp_path):
    _packages(tmp_path)
    (tmp_path / "packages" / "hfb_labelled.yaml").write_text(
        "# ha-builder: notifications@2.0\nscript notify:\n  hfb_notify:\n    sequence: []\n"
    )
    assert errors.owned(tmp_path) == {
        ("automation", "heating_plans_sweep_circuit_demo"): ("heating", "1.19.0-beta.5"),
        ("automation", "hfb_extra"): ("heating", "1.19.0-beta.5"),
        ("script", "heating_boost_circuit_demo"): ("heating", "1.19.0-beta.5"),
        ("automation", "hfb_energy_tariff"): ("energy", "1.0.0"),
        ("script", "hfb_notify"): ("notifications", "2.0"),
    }
    assert errors.owned(tmp_path / "missing") == {}


def test_owned_cached_parses_only_after_a_change(tmp_path, monkeypatch):
    _packages(tmp_path)
    monkeypatch.setattr(errors, "_cache", None)
    calls = []
    real = errors.owned
    monkeypatch.setattr(errors, "owned", lambda root: calls.append(1) or real(root))
    first = errors.owned_cached(tmp_path)
    assert errors.owned_cached(tmp_path) is first and len(calls) == 1
    (tmp_path / "packages" / "hfb_energy.yaml").write_text(
        "# ha-builder: energy@1.0.1\nautomation:\n  id: hfb_energy_tariff\n"
    )
    assert errors.owned_cached(tmp_path)[("automation", "hfb_energy_tariff")][1] == "1.0.1"
    assert len(calls) == 2


def log(name, level="ERROR", message=None):
    return {
        "name": name,
        "level": level,
        "message": message or ["Error in action: boom\nmore"],
        "exception": "Traceback ... /config/secret",
        "source": ["x.py", 1],
    }


AUTO = "homeassistant.components.automation."


def test_log_entity():
    assert errors.log_entity(log(AUTO + "a")) == "automation.a"
    assert errors.log_entity(log("homeassistant.components.script.b", "CRITICAL")) == "script.b"
    assert errors.log_entity(log(AUTO + "a", "WARNING")) is None
    assert errors.log_entity(log("homeassistant.helpers.template")) is None
    assert errors.log_entity(log(AUTO + "a.b")) is None
    assert errors.log_entity("x") is None


def test_from_log(tmp_path):
    _packages(tmp_path)
    index = errors.owned(tmp_path)
    name = AUTO + "heating_plans_sweep_circuit_demo"
    entity = "automation.heating_plans_sweep_circuit_demo"
    assert errors.from_log(log(name), entity, "heating_plans_sweep_circuit_demo", index) == {
        "feature": "heating",
        "version": "1.19.0-beta.5",
        "kind": "log",
        "code": "heating_plans_sweep_circuit_demo",
        "source": name,
        "entity_id": entity,
        "message": "Error in action: boom",
    }
    r = errors.from_log(
        log("homeassistant.components.script.hfb_other", "CRITICAL", "x"),
        "script.hfb_other",
        "hfb_other",
        index,
    )
    assert (r["feature"], r["entity_id"], r["message"]) == ("unknown", "script.hfb_other", "x")
    assert "version" not in r
    # the customer's own: decided by its config id, not by the entity ID
    assert errors.from_log(log(name), entity, "1700000000000", index) is None
    assert errors.from_log(log(name), entity, None, index) is None


# --- dedup -----------------------------------------------------------------


def test_dedup_merges_a_flood():
    d = errors.Dedup()
    sent = []
    for t in range(0, 1800, 2):
        d.add({"feature": "h", "code": "c", "trace": [{"t": t}]}, at(t))
        sent += [(t, r) for r in d.due(t)]
    assert [(t, r["count"]) for t, r in sent] == [(0, 1), (600, 300), (1200, 300)]
    t, second = sent[1]
    assert second["trace"] == [{"t": 2}] and second["first_seen"] == "2026-11-23T05:30:12+00:00"
    assert second["last_seen"] == "2026-11-23T05:40:10+00:00"


def test_dedup_keys_and_cap(caplog):
    d = errors.Dedup()
    d.add({"code": "c", "entity_id": "a"}, T0)
    d.add({"code": "c", "entity_id": "b"}, T0)
    assert len(d.due(0)) == 2
    d = errors.Dedup()
    for i in range(errors.MAX_KEYS + 10):
        d.add({"code": f"k{i}"}, T0)
    assert len(d.due(1)) == errors.MAX_KEYS
    assert caplog.text.count("zahodí") == 1
    # a quiet key is forgotten after the window
    assert d.due(1 + errors.DEDUP_WINDOW) == [] and d._keys == {}


def test_dedup_unsent_does_not_count_as_sent():
    d = errors.Dedup()
    d.add({"code": "c"}, T0)
    dropped = d.due(0)
    d.unsent(dropped)
    d.add({"code": "c"}, T0)
    assert [r["count"] for r in d.due(5)] == [1]  # at once, not after the window
    d.add({"code": "c"}, T0)
    assert d.due(10) == []


# --- Forwarder -------------------------------------------------------------


class _FakeConn:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, data: dict) -> None:
        self.sent.append(data)


def event(event_type, data):
    return {"id": 1, "type": "event", "event": {"event_type": event_type, "data": data}}


def _forward(agent, events, now=0.0, fw=None):
    fw = fw or errors.Forwarder(agent)

    async def go():
        for e in events:
            await fw.handle(e, T0)
        await fw.flush(now)

    asyncio.run(go())
    return fw


def test_forwarder_needs_consent_and_connection(agent):
    conn = _FakeConn()
    agent._conn, agent.connected = conn, True
    one = [event("homecraft_error", {"kind": "domain", "code": "c", "entity_id": "climate.x"})]
    assert not _forward(agent, one).dedup._keys  # no consent: not even kept
    agent.error_reports, agent.paused = True, True
    assert not _forward(agent, one).dedup._keys
    agent.paused, agent.connected = False, False
    assert not _forward(agent, one).dedup._keys  # offline: not kept for later
    assert conn.sent == [] and audit.recent() == []

    agent.connected = True
    _forward(agent, one)
    assert conn.sent == [
        {
            "op": "errors.report",
            "reports": [
                {
                    "kind": "domain",
                    "code": "c",
                    "entity_id": "climate.x",
                    "feature": "unknown",
                    "count": 1,
                    "first_seen": "2026-11-23T05:30:10+00:00",
                    "last_seen": "2026-11-23T05:30:10+00:00",
                }
            ],
        }
    ]
    assert [(a["op"], a["target"]) for a in audit.recent()] == [("errors.report", "1× c")]


def test_forwarder_frames_of_twenty(agent):
    conn = _FakeConn()
    agent._conn, agent.connected, agent.error_reports = conn, True, True
    many = [
        event("homecraft_error", {"kind": "domain", "code": f"c{i:02}", "trace": [{"s": "x"}]})
        for i in range(45)
    ]
    _forward(agent, many)
    assert [len(f["reports"]) for f in conn.sent] == [20, 20, 5]
    assert all("id" not in f for f in conn.sent)
    # one entry per frame: the count and codes, no trace, no message
    assert [a["target"] for a in reversed(audit.recent())][2] == "5× c40, c41, c42, c43, c44"
    assert len(audit.recent()) == 3


def test_report_errors_rechecks_before_every_frame(agent):
    class PauseAfterFirst(_FakeConn):
        async def send(self, data):
            await super().send(data)
            agent.paused = True

    conn = PauseAfterFirst()
    agent._conn, agent.connected, agent.error_reports = conn, True, True
    reports = [{"code": f"c{i}"} for i in range(45)]
    assert asyncio.run(agent.report_errors(reports)) == 20
    assert len(conn.sent) == 1


def test_forwarder_dropped_reports_are_not_marked_sent(agent):
    class Closed:
        async def send(self, data):
            raise RuntimeError("closing transport")

    agent._conn, agent.connected, agent.error_reports = Closed(), True, True
    one = [event("homecraft_error", {"kind": "domain", "code": "c"})]
    fw = _forward(agent, one)
    conn = _FakeConn()
    agent._conn = conn
    _forward(agent, one, now=5, fw=fw)
    assert len(conn.sent) == 1  # not held back by the window


@contextlib.asynccontextmanager
async def fake_ha(monkeypatch, states=None, refuse_first=False):
    """HA's WebSocket (auth, subscribe_events) and REST /api/states."""
    seen: list[dict] = []
    connections: list[web.WebSocketResponse] = []

    async def websocket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        connections.append(ws)
        await ws.send_json({"type": "auth_required"})
        await ws.receive_json()
        await ws.send_json({"type": "auth_ok"})
        refuse = refuse_first and len(connections) == 1
        async for m in ws:
            cmd = m.json()
            seen.append(cmd)
            ok = not (refuse and cmd["id"] == 2)
            await ws.send_json({"id": cmd["id"], "type": "result", "success": ok})
        return ws

    async def state(request):
        entity = request.match_info["entity"]
        if entity not in (states or {}):
            return web.json_response({"message": "Entity not found."}, status=404)
        return web.json_response({"entity_id": entity, "attributes": {"id": states[entity]}})

    app = web.Application()
    app.router.add_get("/ws", websocket)
    app.router.add_get("/api/states/{entity}", state)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setattr(settings, "CORE_WS_URL", f"ws://127.0.0.1:{port}/ws")
    monkeypatch.setattr(settings, "CORE_HTTP_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(errors, "FLUSH_EVERY", 0.05)
    try:
        yield seen, connections
    finally:
        await runner.cleanup()


async def _until(check, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.02)


def test_forwarder_run_subscribes(agent, monkeypatch):
    agent.connected = True

    async def scenario():
        async with fake_ha(monkeypatch) as (seen, connections):
            task = asyncio.create_task(errors.Forwarder(agent).run())
            await asyncio.sleep(0.3)
            assert not connections  # no consent: no connection at all
            agent.error_reports = True
            await _until(lambda: len(seen) == 2)
            task.cancel()
            return seen

    seen = asyncio.run(scenario())
    assert [(m["type"], m["event_type"]) for m in seen] == [
        ("subscribe_events", "homecraft_error"),
        ("subscribe_events", "system_log_event"),
    ]


def test_forwarder_refused_subscription_reconnects_quickly(agent, monkeypatch, caplog):
    agent.connected, agent.error_reports = True, True
    monkeypatch.setattr(errors, "HA_RETRY_FIRST", 0.05)
    monkeypatch.setattr(errors, "HA_RETRY", 60)

    async def scenario():
        async with fake_ha(monkeypatch, refuse_first=True) as (seen, connections):
            fw = errors.Forwarder(agent)
            task = asyncio.create_task(fw.run())
            await _until(lambda: len(seen) == 4)
            await asyncio.sleep(0.1)
            task.cancel()
            return len(connections), fw._failures

    assert asyncio.run(scenario()) == (2, 0)
    assert "ConnectionError" in caplog.text


def test_forwarder_attributes_logs_by_config_id(agent, monkeypatch, tmp_path):
    """The customer's automation holds automation.heating_window_open, Homecraft's
    (config id heating_window_open) got _2: only Homecraft's goes out."""
    p = tmp_path / "packages"
    p.mkdir()
    (p / "hfb_heating.yaml").write_text(
        "# ha-builder: heating@1.0.0\nautomation:\n  - id: heating_window_open\n"
    )
    monkeypatch.setattr(settings, "CONFIG_ROOT", tmp_path)
    monkeypatch.setattr(errors, "_cache", None)
    conn = _FakeConn()
    agent._conn, agent.connected, agent.error_reports = conn, True, True
    states = {
        "automation.heating_window_open": "1700000000000",
        "automation.heating_window_open_2": "heating_window_open",
    }

    async def scenario():
        async with fake_ha(monkeypatch, states):
            async with aiohttp.ClientSession() as http:
                fw = errors.Forwarder(agent)
                fw._http = http
                for obj in ("heating_window_open", "heating_window_open_2", "gone"):
                    await fw.handle(event("system_log_event", log(AUTO + obj)), T0)
                await fw.flush(0)

    asyncio.run(scenario())
    reports = [r for f in conn.sent for r in f["reports"]]
    assert [(r["entity_id"], r["feature"]) for r in reports] == [
        ("automation.heating_window_open_2", "heating")
    ]
