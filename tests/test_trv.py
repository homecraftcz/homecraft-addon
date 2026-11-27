"""trv.sync / trv.status (Shelly BLU TRVs of a circuit) against a fake BLU gateway."""

import asyncio

import pytest
from aiohttp import web

from connector import client, house, policy, trv

PROFILE = {
    "trv": {"min_valve_position": 5, "flags": {"anticlog": True, "silent_mode": False}},
    "ui": {"lock": True, "brightness": 3},
}


def test_parse_config():
    assert trv.parse_config(PROFILE) == {
        "trv": {"min_valve_position": 5, "flags": {"anticlog": True, "silent_mode": False}},
        "ui": {"lock": True, "brightness": 3},
    }
    for bad in (
        {},
        {"trv": {"enable": False}},  # not a profile field
        {"trv": {"min_valve_position": 100}},
        {"trv": {"flags": {"boost": True}}},
        {"ui": {"brightness": 2.5}},
        {"ui": {"lock": 1}},
        {"sys": {}},
        {"trv": {"min_target_C": 25, "max_target_C": 20}},
    ):
        with pytest.raises(ValueError):
            trv.parse_config(bad)


def test_check_takes_an_empty_profile():
    assert trv.parse_config({}, empty=True) == {}
    assert trv.differences({}, {}) == []


def test_differences_tolerates_rounding():
    remote = {"trv:0": {"max_compensation_C": 1.0}, "sys": {"ui": {"lock": 1}}}
    assert trv.differences({"trv": {"max_compensation_C": 1.02}, "ui": {"lock": True}}, remote) == [
        {"field": "ui.lock", "want": True, "have": 1}
    ]


def test_differences():
    remote = {
        "trv:0": {"min_valve_position": 5, "flags": ["anticlog", "silent_mode"]},
        "sys": {"ui": {"lock": True, "brightness": 7}},
    }
    assert trv.differences(PROFILE, remote) == [
        {"field": "ui.brightness", "want": 3, "have": 7},
        {"field": "flags.silent_mode", "want": False, "have": True},
    ]


def test_gateway_url():
    assert trv.gateway_url("http://192.168.1.20") == "http://192.168.1.20:80"
    for bad in (
        "https://192.168.1.20",
        "http://172.30.32.1:8123",
        "http://8.8.8.8",
        "http://gw.local",
        None,
        "http://[::1]",
    ):
        assert trv.gateway_url(bad) is None


def test_sync_updates_sets_and_verifies(monkeypatch):
    """Two valves on one gateway: the old one updates, both get the profile;
    the third (not paired on the gateway) fails without stopping the rest."""
    fw = {200: "old", 201: "new"}
    remote = {200: {"trv:0": {}, "sys": {"ui": {}}}, 201: {"trv:0": {}, "sys": {"ui": {}}}}
    calls = []

    async def rpc(request):
        body = await request.json()
        method, p = body["method"], body.get("params") or {}
        calls.append(method)
        result = None
        if method == "Shelly.GetComponents":
            comps = [
                {"key": "blutrv:200", "config": {"id": 200, "addr": "aa:aa:aa:aa:aa:01"}},
                {"key": "blutrv:201", "config": {"id": 201, "addr": "aa:aa:aa:aa:aa:02"}},
                {"key": "bthomesensor:200", "config": {"id": 200}},
            ]
            result = {"components": comps[p["offset"] : p["offset"] + 2], "total": 3}
        elif method == "BluTrv.CheckForUpdates":
            result = {"fw_id": "new"}
        elif method == "BluTrv.GetRemoteDeviceInfo":
            result = {"device_info": {"fw_id": fw[p["id"]]}}
        elif method == "BluTrv.UpdateFirmware":
            fw[p["id"]] = "new"
        elif method == "BluTrv.Call" and p["method"] == "TRV.SetConfig":
            cfg = dict(p["params"]["config"])
            flags = cfg.pop("flags")
            remote[p["id"]]["trv:0"].update(cfg, flags=[f for f, on in flags.items() if on])
        elif method == "BluTrv.Call" and p["method"] == "Sys.SetConfig":
            remote[p["id"]]["sys"]["ui"].update(p["params"]["config"]["ui"])
        elif method == "BluTrv.GetRemoteConfig":
            result = {"config": remote[p["id"]]}
        return web.json_response({"id": 1, "result": result})

    async def scenario():
        app = web.Application()
        app.router.add_post("/rpc", rpc)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        valves = {
            "climate.a_valve": "AA:AA:AA:AA:AA:01",
            "climate.b_valve": "AA:AA:AA:AA:AA:02",
            "climate.c_valve": "AA:AA:AA:AA:AA:03",
        }
        states = {
            "sensor.heating_gateways_c1": {
                "attributes": {"gateways": {url: {"name": "Brána", "url": url, "valves": valves}}}
            }
        }

        async def fake_states(http):
            return states

        monkeypatch.setattr(house, "_states", fake_states)
        monkeypatch.setattr(trv, "gateway_url", lambda u: u)
        monkeypatch.setattr(trv, "POLL", 0)
        conn = client._Conn(None, None, lambda: None, None)
        started = await client._dispatch(
            "trv.sync", {"instance": "c1", "config": PROFILE, "update": True}, conn
        )
        assert started == {"started": True, "gateways": 1}
        with pytest.raises(policy.OpError) as busy:  # one job per circuit
            await trv.start("c1", PROFILE, True)
        assert busy.value.code == "busy"
        with pytest.raises(policy.OpError) as bad:
            await trv.start("c2", {"ui": {"brightness": 9}}, True)
        assert bad.value.code == "bad_request" and "ui.brightness" in bad.value.message
        while trv.status("c1")["running"]:
            await asyncio.sleep(0.01)
        synced = await client._dispatch("trv.status", {"instance": "c1"}, conn)
        # Changed by hand in the gateway's web UI: trv.check shows it, writes nothing.
        remote[201]["sys"]["ui"]["brightness"] = 7
        fw[200] = "older"
        writes = len(calls)
        await client._dispatch("trv.check", {"instance": "c1", "config": PROFILE}, conn)
        while trv.status("c1")["running"]:
            await asyncio.sleep(0.01)
        checked = await client._dispatch("trv.status", {"instance": "c1"}, conn)
        assert not {"BluTrv.Call", "BluTrv.UpdateFirmware"} & set(calls[writes:])
        await runner.cleanup()
        return synced, checked

    job, checked = asyncio.run(scenario())
    assert checked["mode"] == "check"
    by = {v["entity_id"]: v for v in checked["valves"]}
    assert (
        by["climate.a_valve"]["update"] == "available" and by["climate.a_valve"]["config"] == "ok"
    )
    assert by["climate.b_valve"]["config"] == "mismatch"
    assert by["climate.b_valve"]["diff"] == [{"field": "ui.brightness", "want": 3, "have": 7}]
    by = {v["entity_id"]: v for v in job["valves"]}
    assert by["climate.a_valve"]["update"] == "updated" and by["climate.a_valve"]["fw"] == "new"
    assert by["climate.b_valve"]["update"] == "current"
    assert [by[e]["config"] for e in ("climate.a_valve", "climate.b_valve")] == ["ok", "ok"]
    assert by["climate.c_valve"]["config"] == "failed" and by["climate.c_valve"]["error"]
    assert calls.count("BluTrv.UpdateFirmware") == 1
    assert client._audit_target({"op": "trv.sync", "instance": "c1"}) == "Hlavice c1"
