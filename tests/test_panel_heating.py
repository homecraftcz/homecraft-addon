"""Topení (#48): house.detail() over fake states, and /api/heating +
/api/heating/helper against a fake HA core: only the five helpers of a
reported circuit, range-checked, through the right service."""

import asyncio
import json
from datetime import UTC, datetime

import pytest
from aiohttp import web
from test_house import CIRCUITS, REGISTRY, STATES, _by_id, _state

from connector import audit, features, house, panel, settings

C1 = "heating_circuit_1"
CIRCUITS_1 = [
    {
        **CIRCUITS[0],
        "valves": [{"entity_id": "climate.bathroom_valve", "name": "Koupelna"}],
        "windows": [
            {"key": "w1", "name": "Okno", "sensors": ["binary_sensor.w1"], "valves": []},
            {
                "key": "w2",
                "name": "Dveře",
                "sensors": ["binary_sensor.w2"],
                "valves": ["climate.bathroom_valve", "climate.gone"],
            },
        ],
    },
    CIRCUITS[1],
]
DETAIL_STATES = [
    *STATES,
    _state("climate.bathroom_valve", "heat", current_temperature=20.0, temperature=21),
    {
        **_state("binary_sensor.w2", "on"),
        "last_changed": "2026-11-27T10:00:00+00:00",
    },
    _state(
        f"sensor.heating_valves_{C1}",
        "1",
        phases={"climate.bathroom_valve": ["fast", "2026-11-27T09:00:00+00:00", 21.0]},
    ),
    _state(f"binary_sensor.heating_state_{C1}", "on", url="/hfb-features/heating"),
    _state(f"binary_sensor.heating_weather_ok_{C1}", "on"),
    _state(f"input_number.heating_window_reminder_{C1}", "15.0"),
    _state(f"input_number.heating_weather_threshold_{C1}", "15.5"),
    _state(f"input_select.heating_auto_rule_{C1}", "time"),
    _state(f"input_datetime.heating_time_from_{C1}", "06:30:00"),
    _state(f"input_datetime.heating_time_to_{C1}", "bad"),
    _state(f"input_select.heating_mode_{C1}", "by_weather"),
    _state(f"switch.boiler_{C1}", "unavailable"),
    _state(f"input_number.heating_bypass_overrun_{C1}", "10.0"),
]


def test_detail():
    d = house.detail(CIRCUITS_1[0], _by_id(DETAIL_STATES), {})
    [v] = d["valves"]
    assert (v["room"], v["phase"], v["temperature"]) == ("bathroom", "fast", 20.0)
    okno, dvere = d["windows"]
    assert (okno["open"], okno["open_since"], okno["valves"]) == (True, None, [])
    assert dvere["open_since"] == datetime(2026, 11, 27, 10, tzinfo=UTC).timestamp()
    assert dvere["valves"] == ["Koupelna", "climate.gone"]
    assert d["helpers"] == {
        "window_reminder": 15.0,
        "weather_threshold": 15.5,
        "auto_rule": "time",
        "time_from": "06:30",
        "time_to": None,
    }
    assert (d["dashboard_url"], d["mode"], d["weather_ok"]) == (
        "/hfb-features/heating",
        "by_weather",
        True,
    )
    assert d["boiler"] == {"relay": "unavailable", "overrun": 10.0}


def test_detail_without_helpers_and_with_a_foreign_url():
    states = _by_id([_state(f"binary_sensor.heating_state_{C1}", "on", url="//evil.example/x")])
    d = house.detail({"instance": C1, "valves": [{"entity_id": "climate.x"}]}, states, {})
    assert d["dashboard_url"] == "/lovelace"
    assert set(d["helpers"].values()) == {None}
    assert d["valves"][0]["room"] == "" and d["valves"][0]["phase"] == "normal"
    assert d["boiler"] == {"relay": None, "overrun": None}


@pytest.mark.parametrize(
    ("key", "value", "service", "data"),
    [
        ("window_reminder", 20, "input_number/set_value", {"value": 20.0}),
        ("window_reminder", 0, "input_number/set_value", {"value": 0.0}),
        ("window_reminder", 120, "input_number/set_value", {"value": 120.0}),
        ("weather_threshold", 15.5, "input_number/set_value", {"value": 15.5}),
        ("weather_threshold", 5, "input_number/set_value", {"value": 5.0}),
        ("auto_rule", "time", "input_select/select_option", {"option": "time"}),
        ("auto_rule", "weather", "input_select/select_option", {"option": "weather"}),
        ("time_from", "06:30", "input_datetime/set_datetime", {"time": "06:30:00"}),
        ("time_to", "23:59:59", "input_datetime/set_datetime", {"time": "23:59:00"}),
    ],
)
def test_allowed_values(key, value, service, data):
    prefix = house.HELPERS[key][0]
    assert house.helper_call(C1, key, value) == (service, {"entity_id": prefix + C1, **data})


@pytest.mark.parametrize(
    ("instance", "key", "value"),
    [
        (C1, "window_reminder", 7),  # step 5
        (C1, "window_reminder", 125),
        (C1, "window_reminder", -5),
        (C1, "window_reminder", True),
        (C1, "window_reminder", "20"),
        (C1, "window_reminder", float("nan")),
        (C1, "weather_threshold", 15.2),  # step 0.5
        (C1, "weather_threshold", 4.5),
        (C1, "weather_threshold", 25.5),
        (C1, "auto_rule", "off"),
        (C1, "auto_rule", None),
        (C1, "time_from", "24:00"),
        (C1, "time_from", "6:30"),
        (C1, "time_to", 630),
        (C1, "mode", "on"),  # not one of the five
        (C1, "low_battery_threshold", 20),
        ("a, input_boolean.x", "auto_rule", "time"),  # two entities for HA
        ("Přízemí", "auto_rule", "time"),
    ],
)
def test_refused_values(instance, key, value):
    with pytest.raises(ValueError):
        house.helper_call(instance, key, value)


def _fake_core(calls, status=200):
    async def states(request):
        return web.json_response(DETAIL_STATES)

    async def service(request):
        assert request.headers["Authorization"] == "Bearer t0k"
        path = f"{request.match_info['domain']}/{request.match_info['service']}"
        calls.append((path, await request.json()))
        return web.json_response([], status=status)

    async def websocket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required"})
        await ws.receive_json()
        await ws.send_json({"type": "auth_ok"})
        async for msg in ws:
            cmd = msg.json()
            reply = {"id": cmd["id"], "type": "result", "success": True, "result": REGISTRY}
            await ws.send_json(reply)
        return ws

    app = web.Application()
    app.router.add_get("/api/states", states)
    app.router.add_post("/api/services/{domain}/{service}", service)
    app.router.add_get("/ws", websocket)
    return app


def _scenario(monkeypatch, calls, steps, status=200):
    monkeypatch.setattr(house, "_registry", {"at": None, "devices": {}})
    monkeypatch.setattr(settings, "SUPERVISOR_TOKEN", "t0k")
    features.save({"features": [{"name": "Topení", "id": "heating", "circuits": CIRCUITS_1}]})

    async def scenario(http):
        runner = web.AppRunner(_fake_core(calls, status))
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        monkeypatch.setattr(settings, "CORE_HTTP_URL", f"http://127.0.0.1:{port}")
        monkeypatch.setattr(settings, "CORE_WS_URL", f"ws://127.0.0.1:{port}/ws")
        try:
            return [await step(http) for step in steps]
        finally:
            await asyncio.wait_for(runner.cleanup(), 5)

    return scenario


def _post(body):
    async def step(http):
        r = await http.post(
            "/api/heating/helper", json=body, headers={"X-Homecraft-Panel": panel.NONCE}
        )
        return r.status, await r.text()

    return step


def test_helper_endpoint_calls_the_service(run_panel, monkeypatch):
    calls = []
    steps = [
        _post({"circuit": C1, "key": "window_reminder", "value": 20}),
        _post({"circuit": C1, "key": "auto_rule", "value": "time"}),
        _post({"circuit": C1, "key": "time_from", "value": "05:45"}),
    ]
    got = run_panel(_scenario(monkeypatch, calls, steps))
    assert [s for s, _ in got] == [200, 200, 200]
    assert json.loads(got[0][1]) == {"ok": True, "value": 20.0}
    assert calls == [
        (
            "input_number/set_value",
            {"entity_id": f"input_number.heating_window_reminder_{C1}", "value": 20.0},
        ),
        (
            "input_select/select_option",
            {"entity_id": f"input_select.heating_auto_rule_{C1}", "option": "time"},
        ),
        (
            "input_datetime/set_datetime",
            {"entity_id": f"input_datetime.heating_time_from_{C1}", "time": "05:45:00"},
        ),
    ]
    audit.flush()
    entries = audit.recent()
    assert entries[-1]["op"] == "panel.helper" and entries[-1]["ok"]
    assert entries[-1]["target"] == f"input_number.heating_window_reminder_{C1} = 20.0"


def test_helper_endpoint_refuses(run_panel, monkeypatch):
    calls = []
    steps = [
        _post({"circuit": "heating_circuit_9", "key": "auto_rule", "value": "time"}),
        _post({"circuit": C1, "key": "mode", "value": "off"}),
        _post({"circuit": C1, "key": "window_reminder", "value": 7}),
        _post({"circuit": C1, "key": "weather_threshold", "value": 30}),
        _post({"circuit": C1, "key": "auto_rule"}),
        _post({"circuit": ["x"], "key": "auto_rule", "value": "time"}),
    ]
    got = run_panel(_scenario(monkeypatch, calls, steps))
    assert [s for s, _ in got] == [400] * 6
    assert calls == []
    assert audit.recent() == []


def test_helper_endpoint_ha_refuses(run_panel, monkeypatch):
    calls = []
    steps = [_post({"circuit": C1, "key": "auto_rule", "value": "time"})]
    [(status, body)] = run_panel(_scenario(monkeypatch, calls, steps, status=400))
    assert status == 502 and "ha_failed" in json.loads(body)["error"]
    assert audit.recent()[0]["ok"] is False


def test_helper_endpoint_needs_the_nonce(run_panel, monkeypatch):
    calls = []

    async def step(http):
        body = {"circuit": C1, "key": "auto_rule", "value": "time"}
        return (await http.post("/api/heating/helper", json=body)).status

    assert run_panel(_scenario(monkeypatch, calls, [step])) == [403]
    assert calls == []


def test_heating_detail_endpoint(run_panel, monkeypatch):
    async def detail(http):
        return await (await http.get(f"/api/heating?circuit={C1}")).json()

    async def unknown(http):
        return (await http.get("/api/heating?circuit=nope")).status

    d, missing = run_panel(_scenario(monkeypatch, [], [detail, unknown]))
    assert d["helpers"]["auto_rule"] == "time" and d["valves"][0]["phase"] == "fast"
    assert missing == 404
