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
from connector.client import AskError

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


@pytest.mark.parametrize(
    ("url", "got"),
    [
        ("/hfb-features/heating", "/hfb-features/heating"),
        ("/lovelace/0?x=1#y", "/lovelace/0?x=1#y"),
        ("//evil.example/x", "/lovelace"),
        ("/\\evil.example", "/lovelace"),
        ("/\t/evil.example", "/lovelace"),
        ("/\n/evil.example", "/lovelace"),
        ("\t//evil.example", "/lovelace"),
        ("https://evil.example/", "/lovelace"),
        ("javascript:alert(1)", "/lovelace"),
        ("/a\x00b", "/lovelace"),
        ("hfb-features/heating", "/lovelace"),
        ("", "/lovelace"),
        (5, "/lovelace"),
    ],
)
def test_dashboard_url_stays_in_ha(url, got):
    states = _by_id([_state(f"binary_sensor.heating_state_{C1}", "on", url=url)])
    assert house.detail({"instance": C1}, states, {})["dashboard_url"] == got


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
        (C1, "window_reminder", 10**400),  # math.isfinite would raise OverflowError
        (C1, "weather_threshold", float("inf")),
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

    async def one_state(request):
        assert request.headers["Authorization"] == "Bearer t0k"
        state = _by_id(DETAIL_STATES).get(request.match_info["entity_id"])
        return web.json_response(
            state or {"message": "Entity not found."}, status=200 if state else 404
        )

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
    app.router.add_get("/api/states/{entity_id}", one_state)
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
    assert entries[-1]["target"] == f"{C1} window_reminder"  # never the value


def test_helper_endpoint_refuses(run_panel, monkeypatch):
    calls = []
    steps = [
        _post({"circuit": "heating_circuit_9", "key": "auto_rule", "value": "time"}),
        _post({"circuit": C1, "key": "mode", "value": "off"}),
        _post({"circuit": C1, "key": "window_reminder", "value": 7}),
        _post({"circuit": C1, "key": "weather_threshold", "value": 30}),
        _post({"circuit": C1, "key": "auto_rule"}),
        _post({"circuit": ["x"], "key": "auto_rule", "value": "time"}),
        _post({"circuit": C1, "key": "window_reminder", "value": 10**400}),
    ]
    got = run_panel(_scenario(monkeypatch, calls, steps))
    assert [s for s, _ in got] == [400] * 7
    assert calls == []
    assert audit.recent() == []


def test_helper_endpoint_missing_helper(run_panel, monkeypatch):
    """A reported circuit whose helper HA does not have: 409, no service call
    (HA would answer 200 and change nothing)."""
    calls = []
    steps = [_post({"circuit": "heating_circuit_2", "key": "window_reminder", "value": 20})]
    [(status, body)] = run_panel(_scenario(monkeypatch, calls, steps))
    assert status == 409 and "chybí" in json.loads(body)["error"]
    assert calls == []
    assert audit.recent()[0]["code"] == "not_found"


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


def test_detail_has_what_the_panel_edits():
    """#49: the reported names (not HA's fallbacks), window keys and valve IDs."""
    circuit = {
        **CIRCUITS_1[0],
        "heading": "Dole",
        "valves": [{"entity_id": "climate.bathroom_valve", "name": ""}, {"entity_id": ["x"]}],
    }
    d = house.detail(circuit, _by_id(DETAIL_STATES), {})
    assert (d["title"], d["heading"]) == ("Přízemí", "Dole")
    [v] = d["valves"]
    assert (v["name"], v["own_name"]) == ("climate.bathroom_valve", "")
    assert [(w["key"], w["own_name"], w["valve_ids"]) for w in d["windows"]] == [
        ("w1", "Okno", []),
        ("w2", "Dveře", ["climate.bathroom_valve", "climate.gone"]),
    ]


# --- heating.save (#49)


def _save_scenario(monkeypatch, agent, bodies, ask=None, nonce=True):
    """POST each body to /api/heating/save with CIRCUITS_1 reported; `ask`
    replaces Client.ask (None: the real one, the agent is not connected)."""
    features.save({"features": [{"name": "Topení", "id": "heating", "circuits": CIRCUITS_1}]})
    if ask is not None:
        monkeypatch.setattr(agent, "ask", ask)
    headers = {"X-Homecraft-Panel": panel.NONCE} if nonce else {}

    async def scenario(http):
        out = []
        for body in bodies:
            r = await http.post("/api/heating/save", json=body, headers=headers)
            text = await r.text()
            out.append(
                (r.status, json.loads(text) if r.content_type == "application/json" else text)
            )
        return out

    return scenario


def test_save_sends_heating_save(run_panel, agent, monkeypatch):
    asked = []

    async def ask(op, payload, timeout):
        asked.append((op, payload))
        assert timeout == panel.SAVE_TIMEOUT == 300
        return {"status": "success"}

    body = {
        "instance": C1,
        "title": " Přízemí ",
        "valve_names": {"climate.bathroom_valve": "Koupelna dole"},
        "windows": {
            "w1": {"name": "Okno", "valves": ["climate.bathroom_valve"] * 2},
            "w2": {"valves": []},
        },
    }
    [(status, reply)] = run_panel(_save_scenario(monkeypatch, agent, [body], ask))
    assert (status, reply) == (200, {"ok": True, "status": "success"})
    assert asked == [
        (
            "heating.save",
            {
                **body,
                "title": "Přízemí",
                "windows": {
                    "w1": {"name": "Okno", "valves": ["climate.bathroom_valve"]},
                    "w2": {"valves": []},
                },
            },
        )
    ]
    audit.flush()
    [entry] = audit.recent()
    assert (entry["op"], entry["target"], entry["ok"]) == ("heating.save", C1, True)


@pytest.mark.parametrize(
    "body",
    [
        {"instance": C1},  # nothing to save
        {"instance": C1, "valve_names": {}},
        {"instance": C1, "windows": {}},
        {"instance": C1, "title": "  "},
        {"instance": C1, "heading": ""},
        {"instance": "heating_circuit_9", "title": "X"},
        {"instance": C1, "title": "X", "parameters": {}},
        {"instance": C1, "title": "x" * 101},
        {"instance": C1, "heading": 5},
        {"instance": C1, "valve_names": {"climate.other": "X"}},
        {"instance": C1, "valve_names": ["climate.bathroom_valve"]},
        {"instance": C1, "valve_names": {"climate.bathroom_valve": None}},
        {"instance": C1, "windows": {"w9": {"name": "X"}}},
        {"instance": C1, "windows": {"w1": {}}},
        {"instance": C1, "windows": {"w1": {"name": "X", "sensors": []}}},
        {"instance": C1, "windows": {"w1": {"valves": ["climate.gone"]}}},
        {"instance": C1, "windows": {"w1": {"valves": "climate.bathroom_valve"}}},
        ["not", "an", "object"],
    ],
)
def test_save_refuses_bodies(run_panel, agent, monkeypatch, body):
    asked = []

    async def ask(op, payload, timeout):
        asked.append(payload)

    [(status, reply)] = run_panel(_save_scenario(monkeypatch, agent, [body], ask))
    assert status == 400 and reply["ok"] is False
    assert asked == [] and audit.recent() == []


def test_save_offline_is_409(run_panel, agent, monkeypatch):
    body = {"instance": C1, "title": "X"}
    [(status, reply)] = run_panel(_save_scenario(monkeypatch, agent, [body]))
    assert status == 409 and reply["code"] == "offline"
    assert "není připojený" in reply["error"]
    assert audit.recent()[0]["code"] == "offline"


@pytest.mark.parametrize(
    ("code", "message", "status", "shown"),
    [
        ("blocked", "Technik má v topení rozpracované změny.", 409, "Technik má v topení"),
        ("blocked", "", 409, "Technik musí nejdřív vyřešit změny v Home Assistantu."),
        ("failed", "Změny jsou uložené, ale nasazení se nepovedlo.", 502, "nepovedlo"),
        ("invalid", "Hlavice x v okruhu není", 400, "Hlavice x v okruhu není"),
        ("busy", "Too many requests in flight", 409, "už probíhá"),
        ("unknown_op", "Unknown operation", 502, "neumí"),
        ("unknown", "", 502, "nevíme, jestli se změna uložila"),
        ("internal", "Internal error", 502, "(internal)"),
    ],
)
def test_save_errors(run_panel, agent, monkeypatch, code, message, status, shown):
    async def ask(op, payload, timeout):
        raise AskError(code, message)

    body = {"instance": C1, "title": "X"}
    [(got, reply)] = run_panel(_save_scenario(monkeypatch, agent, [body], ask))
    assert got == status and shown in reply["error"] and reply["code"] == code


def test_save_needs_the_nonce(run_panel, agent, monkeypatch):
    asked = []

    async def ask(op, payload, timeout):
        asked.append(payload)

    body = {"instance": C1, "title": "X"}
    [(status, _)] = run_panel(_save_scenario(monkeypatch, agent, [body], ask, nonce=False))
    assert status == 403 and asked == []
