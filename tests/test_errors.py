"""Error reports to Homecraft (#31): allowlist, log rule, dedup, consent."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

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
    assert errors.owned(tmp_path) == {
        "automation.heating_plans_sweep_circuit_demo": ("heating", "1.19.0-beta.5"),
        "automation.hfb_extra": ("heating", "1.19.0-beta.5"),
        "script.heating_boost_circuit_demo": ("heating", "1.19.0-beta.5"),
        "automation.hfb_energy_tariff": ("energy", "1.0.0"),
    }
    assert errors.owned(tmp_path / "missing") == {}


def log(name, level="ERROR", message=None):
    return {
        "name": name,
        "level": level,
        "message": message or ["Error in action: boom\nmore"],
        "exception": "Traceback ... /config/secret",
        "source": ["x.py", 1],
    }


def test_from_log(tmp_path):
    _packages(tmp_path)
    index = errors.owned(tmp_path)
    name = "homeassistant.components.automation.heating_plans_sweep_circuit_demo"
    assert errors.from_log(log(name), index) == {
        "feature": "heating",
        "version": "1.19.0-beta.5",
        "kind": "log",
        "code": "heating_plans_sweep_circuit_demo",
        "source": name,
        "entity_id": "automation.heating_plans_sweep_circuit_demo",
        "message": "Error in action: boom",
    }
    r = errors.from_log(log("homeassistant.components.script.hfb_other", "CRITICAL", "x"), index)
    assert (r["feature"], r["entity_id"], r["message"]) == ("unknown", "script.hfb_other", "x")
    assert "version" not in r
    assert errors.from_log(log("homeassistant.components.automation.my_lights"), index) is None
    assert errors.from_log(log(name, "WARNING"), index) is None
    assert errors.from_log(log("homeassistant.helpers.template"), index) is None


# --- dedup -----------------------------------------------------------------


def test_dedup_merges_a_flood():
    d = errors.Dedup()
    sent = []
    for t in range(0, 1800, 2):
        d.add({"feature": "h", "code": "c", "trace": [{"t": t}]}, t, at(t))
        sent += [(t, r) for r in d.due(t)]
    assert [(t, r["count"]) for t, r in sent] == [(0, 1), (600, 300), (1200, 300)]
    t, second = sent[1]
    assert second["trace"] == [{"t": 2}] and second["first_seen"] == "2026-11-23T05:30:12+00:00"
    assert second["last_seen"] == "2026-11-23T05:40:10+00:00"


def test_dedup_keys_and_cap(caplog):
    d = errors.Dedup()
    d.add({"code": "c", "entity_id": "a"}, 0, T0)
    d.add({"code": "c", "entity_id": "b"}, 0, T0)
    assert len(d.due(0)) == 2
    d = errors.Dedup()
    for i in range(errors.MAX_KEYS + 10):
        d.add({"code": f"k{i}"}, 1, T0)
    assert len(d.due(1)) == errors.MAX_KEYS
    assert caplog.text.count("zahodí") == 1
    # a quiet key is forgotten after the window
    assert d.due(1 + errors.DEDUP_WINDOW) == [] and d._keys == {}


# --- Forwarder -------------------------------------------------------------


class _FakeConn:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, data: dict) -> None:
        self.sent.append(data)


def event(event_type, data):
    return {"id": 1, "type": "event", "event": {"event_type": event_type, "data": data}}


def _forward(agent, events, now=0.0):
    fw = errors.Forwarder(agent)

    async def go():
        for e in events:
            await fw.handle(e, dict, now, T0)
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
    _forward(agent, one)  # offline: dropped, not kept for later
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
    assert [(a["op"], a["target"]) for a in audit.recent()] == [("errors.report", "c climate.x")]


def test_forwarder_frames_of_twenty(agent):
    conn = _FakeConn()
    agent._conn, agent.connected, agent.error_reports = conn, True, True
    many = [
        event("homecraft_error", {"kind": "domain", "code": f"c{i}", "trace": [{"s": "x"}]})
        for i in range(45)
    ]
    _forward(agent, many)
    assert [len(f["reports"]) for f in conn.sent] == [20, 20, 5]
    assert all("id" not in f for f in conn.sent)
    targets = {a["target"] for a in audit.recent()}
    assert len(targets) == 45 and "c0" in targets  # code only: no trace, no message


def test_forwarder_reads_packages_only_for_matching_logs(agent):
    conn = _FakeConn()
    agent._conn, agent.connected, agent.error_reports = conn, True, True
    loads = []
    fw = errors.Forwarder(agent)

    def loader():
        loads.append(1)
        return {"automation.a": ("heating", "1.0")}

    async def go():
        await fw.handle(event("system_log_event", log("homeassistant.core")), loader, 0, T0)
        await fw.handle(
            event("system_log_event", log("homeassistant.components.automation.a")), loader, 0, T0
        )
        await fw.flush(0)

    asyncio.run(go())
    assert len(loads) == 1
    assert conn.sent[0]["reports"][0]["feature"] == "heating"


def test_forwarder_run_subscribes(agent, monkeypatch):
    seen: list[dict] = []
    connections: list[int] = []

    async def scenario():
        got = asyncio.Event()

        async def websocket(request):
            connections.append(1)
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_json({"type": "auth_required"})
            await ws.receive_json()
            await ws.send_json({"type": "auth_ok"})
            for _ in range(2):
                seen.append(await ws.receive_json())
            got.set()
            await ws.receive()
            return ws

        app = web.Application()
        app.router.add_get("/ws", websocket)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        monkeypatch.setattr(settings, "CORE_WS_URL", f"ws://127.0.0.1:{port}/ws")
        monkeypatch.setattr(errors, "FLUSH_EVERY", 0.05)

        task = asyncio.create_task(errors.Forwarder(agent).run())
        await asyncio.sleep(0.3)
        assert not connections  # no consent: no connection at all
        agent.error_reports = True
        await asyncio.wait_for(got.wait(), 5)
        task.cancel()
        await runner.cleanup()

    asyncio.run(scenario())
    assert [(m["type"], m["event_type"]) for m in seen] == [
        ("subscribe_events", "homecraft_error"),
        ("subscribe_events", "system_log_event"),
    ]
