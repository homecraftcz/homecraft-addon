"""Domů's live heating (#47): summarize() over fake states and registry, and
/api/house against a fake HA core (REST states + WS entity registry)."""

import asyncio

from aiohttp import web

from connector import features, house, network_discovery, policy, settings

CIRCUITS = [
    {
        "instance": "heating_circuit_1",
        "title": "Přízemí",
        "heading": "",
        "valves": [
            {"entity_id": "climate.bathroom", "name": "Koupelna"},
            {"entity_id": "climate.kitchen", "name": ""},
        ],
        "windows": [
            {"key": "w1", "name": "Okno koupelna", "sensors": ["binary_sensor.w1"], "valves": []}
        ],
    },
    {"instance": "heating_circuit_2", "title": "Patro", "valves": [], "windows": [], "x": 1},
]


def _state(eid, state, **attrs):
    return {"entity_id": eid, "state": state, "attributes": attrs}


STATES = [
    _state(
        "climate.bathroom",
        "heat",
        current_temperature=21.5,
        temperature=22,
        hvac_action="heating",
    ),
    _state("climate.kitchen", "heat", friendly_name="Kuchyň TRV", hvac_action="idle"),
    _state("sensor.bathroom_battery", "12", device_class="battery"),
    _state("sensor.kitchen_battery", "80", device_class="battery"),
    _state("sensor.w1_battery", "25", device_class="battery"),
    _state("binary_sensor.w1", "on"),
    _state("binary_sensor.heating_state_heating_circuit_1", "on"),
    _state("weather.forecast_domov", "cloudy", temperature=6.2),
]
REGISTRY = [
    {"entity_id": "climate.bathroom", "device_id": "d1"},
    {"entity_id": "sensor.bathroom_battery", "device_id": "d1"},
    {"entity_id": "climate.kitchen", "device_id": "d2"},
    {"entity_id": "sensor.kitchen_battery", "device_id": "d2"},
    {"entity_id": "binary_sensor.w1", "device_id": "d3"},
    {"entity_id": "sensor.w1_battery", "device_id": "d3"},
    {"entity_id": "sensor.orphan", "device_id": None},
]


def _by_id(states):
    return {s["entity_id"]: s for s in states}


def _devices():
    return {e["entity_id"]: e["device_id"] for e in REGISTRY if e["device_id"]}


def test_summary_counts_valves_windows_and_low_batteries():
    got = house.summarize(CIRCUITS, _by_id(STATES), _devices())
    h = got["heating"]
    assert (h["valves"], h["valves_heating"], h["windows"], h["windows_open"]) == (2, 1, 1, 1)
    assert h["outdoor"] == 6.2
    assert [(c["name"], c["heating"]) for c in h["circuits"]] == [
        ("Přízemí", True),
        ("Patro", False),
    ]
    bath, kitchen = h["circuits"][0]["valves"]
    assert (bath["temperature"], bath["target"], bath["battery"]) == (21.5, 22.0, 12.0)
    assert kitchen["name"] == "Kuchyň TRV"
    assert [(b["name"], b["kind"], b["battery"]) for b in got["low_batteries"]] == [
        ("Koupelna", "valve", 12.0),
        ("Okno koupelna", "window", 25.0),
    ]


def test_threshold_helper_and_outdoor_source():
    states = _by_id(
        [
            *STATES,
            _state(house.LOW_BATTERY_HELPER, "20.0"),
            _state(house.OUTDOOR_HELPER, "sensor.outside"),
            _state("sensor.outside", "-3.5"),
        ]
    )
    got = house.summarize(CIRCUITS, states, _devices())
    assert [b["name"] for b in got["low_batteries"]] == ["Koupelna"]
    assert got["heating"]["outdoor"] == -3.5


def test_missing_and_malformed_data_does_not_break():
    circuits = [
        {"instance": "c1", "valves": "x"},
        "junk",
        {"instance": "c2", "windows": [{"sensors": [1]}]},
        {"instance": {"x": 1}, "valves": [{"entity_id": "climate.bathroom"}]},  # skipped
        {"instance": "c3", "heading": {"x": 1}, "valves": [{"entity_id": "climate.a", "name": 5}]},
    ]
    got = house.summarize(circuits, {}, {})
    h = got["heating"]
    assert [c["name"] for c in h["circuits"]] == ["c1", "c2", "c3"]
    assert h["circuits"][2]["valves"][0]["name"] == "climate.a"
    assert h["circuits"][1]["windows"][0]["name"] == ""
    assert (h["valves"], h["circuits_heating"]) == (1, 0)
    assert h["outdoor"] is None
    assert got["low_batteries"] == []


def test_several_battery_sensors_on_one_device_keep_the_lowest():
    states = _by_id([*STATES, _state("sensor.bathroom_battery_2", "60", device_class="battery")])
    devices = {**_devices(), "sensor.bathroom_battery_2": "d1"}
    got = house.summarize(CIRCUITS, states, devices)
    assert got["low_batteries"][0]["battery"] == 12.0
    states = _by_id([*STATES, _state("sensor.kitchen_battery_2", "5", device_class="battery")])
    devices = {**_devices(), "sensor.kitchen_battery_2": "d2"}
    got = house.summarize(CIRCUITS, states, devices)
    assert got["low_batteries"][0]["name"] == "Kuchyň TRV"


def test_registry_failure_keeps_the_summary(monkeypatch):
    async def states(http):
        return _by_id(STATES)

    async def collect(http, commands, **kw):
        raise policy.OpError("ha_unreachable", "x")

    monkeypatch.setattr(house, "_registry", {"at": None, "devices": {}})
    monkeypatch.setattr(house, "_states", states)
    monkeypatch.setattr(network_discovery, "collect", collect)
    got = asyncio.run(house.heating_summary(CIRCUITS))
    assert got["heating"]["valves"] == 2
    assert got["low_batteries"] == []


def _fake_core(monkeypatch):
    async def states(request):
        assert request.headers["Authorization"] == "Bearer t0k"
        return web.json_response(STATES)

    async def websocket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required"})
        await ws.receive_json()
        await ws.send_json({"type": "auth_ok"})
        async for msg in ws:
            cmd = msg.json()
            assert cmd["type"] == "config/entity_registry/list"
            reply = {"id": cmd["id"], "type": "result", "success": True, "result": REGISTRY}
            await ws.send_json(reply)
        return ws

    app = web.Application()
    app.router.add_get("/api/states", states)
    app.router.add_get("/ws", websocket)
    return app


def test_panel_house(run_panel, monkeypatch):
    monkeypatch.setattr(house, "_registry", {"at": None, "devices": {}})
    monkeypatch.setattr(settings, "SUPERVISOR_TOKEN", "t0k")

    async def scenario(http):
        before = await (await http.get("/api/house")).json()
        heating = {"name": "Topení", "id": "heating", "circuits": CIRCUITS}
        features.save({"project_url": "", "features": [heating]})
        runner = web.AppRunner(_fake_core(monkeypatch))
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        monkeypatch.setattr(settings, "CORE_HTTP_URL", f"http://127.0.0.1:{port}")
        monkeypatch.setattr(settings, "CORE_WS_URL", f"ws://127.0.0.1:{port}/ws")
        try:
            ok = await (await http.get("/api/house")).json()
        finally:
            await asyncio.wait_for(runner.cleanup(), 5)
        monkeypatch.setattr(settings, "CORE_HTTP_URL", "http://127.0.0.1:1")
        down = await (await http.get("/api/house")).json()
        return before, ok, down

    before, ok, down = run_panel(scenario)
    assert before == {"heating": None, "low_batteries": []}
    assert ok["heating"]["valves_heating"] == 1
    assert ok["low_batteries"][0]["name"] == "Koupelna"
    assert down["heating"] is None and down["error"]


def test_registry_is_cached(monkeypatch):
    calls = []

    async def collect(http, commands, **kw):
        calls.append(commands)
        return [{"result": REGISTRY, "events": [], "error": None}]

    monkeypatch.setattr(house, "_registry", {"at": None, "devices": {}})
    monkeypatch.setattr(network_discovery, "collect", collect)
    for _ in range(2):
        assert asyncio.run(house._entity_devices(None))["climate.kitchen"] == "d2"
    assert len(calls) == 1
