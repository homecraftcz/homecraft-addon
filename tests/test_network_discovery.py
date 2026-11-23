"""Network device candidates from HA (#632): merge_candidates (Homecraft #464
tests, unchanged), the add-on's marking of saved devices, and the panel endpoint
against a fake HA core."""

import asyncio

from aiohttp import web

from connector import network_devices, network_discovery, settings
from connector.network_discovery import merge_candidates

ENTRIES = [
    {"entry_id": "e1", "domain": "shelly"},
    {"entry_id": "e2", "domain": "onvif"},
    {"entry_id": "e3", "domain": "synology_dsm"},
]


def _by_addr(cands):
    return {c["address"]: c for c in cands}


def test_registry_and_dhcp_join_by_mac_in_any_format():
    devices = [
        {
            "name": "Hall TRV",
            "name_by_user": "Hallway",
            "configuration_url": None,
            "connections": [["mac", "aa-bb-cc-dd-ee-ff"]],
            "manufacturer": "Shelly",
            "model": "BLU",
            "config_entries": ["e1"],
            "primary_config_entry": "e1",
        },
        {"name": "Orphan", "connections": [["mac", "11:22:33:44:55:66"]]},  # no address: dropped
    ]
    dhcp = [{"mac_address": "AABBCCDDEEFF", "hostname": "hall", "ip_address": "10.0.0.5"}]
    [c] = merge_candidates(devices, ENTRIES, dhcp, [], [])
    assert c["address"] == "10.0.0.5"
    assert c["mac"] == "AA:BB:CC:DD:EE:FF"
    assert c["name"] == "Hallway"
    assert c["hostname"] == "hall"
    assert c["integration"] == "shelly"
    assert c["sources"] == ["dhcp", "registry"]


def test_configuration_url_gives_address_and_dedup_urls():
    devices = [
        {"name": "Cam", "configuration_url": "http://10.0.0.9:80/ui", "config_entries": ["e2"]},
        {"name": "Cam", "configuration_url": "http://10.0.0.9:80/ui", "config_entries": ["e2"]},
    ]
    [c] = merge_candidates(devices, ENTRIES, [], [], [])
    assert c["address"] == "10.0.0.9"
    assert c["urls"] == ["http://10.0.0.9:80/ui"]
    assert c["kind"] == "camera"


def test_zeroconf_prefers_ipv4_and_ssdp_uses_location():
    zc = [
        {
            "name": "shellyblugw-abc._shelly._tcp.local.",
            "type": "_shelly._tcp.local.",
            "ip_addresses": ["fe80::1", "10.0.0.10"],
        }
    ]
    ssdp = [{"name": "Router", "ssdp_location": "http://10.0.0.1:1900/desc.xml"}]
    got = _by_addr(merge_candidates([], [], [], zc, ssdp))
    assert got["10.0.0.10"]["name"] == "shellyblugw-abc"
    assert got["10.0.0.10"]["kind"] == "shelly_gateway"
    assert got["10.0.0.10"]["sources"] == ["zeroconf"]
    assert got["10.0.0.1"]["name"] == "Router"
    assert got["10.0.0.1"]["kind"] == "other"


def test_kinds():
    zc = [
        {
            "name": "shelly1-x._http._tcp.local.",
            "type": "_http._tcp.local.",
            "ip_addresses": ["10.0.0.2"],
        },
        {"name": "c._rtsp._tcp.local.", "type": "_rtsp._tcp.local.", "ip_addresses": ["10.0.0.3"]},
    ]
    devices = [
        {"name": "NAS", "configuration_url": "https://10.0.0.4:5001", "config_entries": ["e3"]}
    ]
    got = _by_addr(merge_candidates(devices, ENTRIES, [], zc, []))
    assert got["10.0.0.2"]["kind"] == "shelly"
    assert got["10.0.0.3"]["kind"] == "camera"
    assert got["10.0.0.4"]["kind"] == "nas"


def test_malformed_items_ignored_and_sorted_numerically():
    dhcp = [
        "junk",
        {"ip_address": "10.0.0.20", "mac_address": "bad"},
        {"ip_address": "10.0.0.3", "hostname": "b"},
        {"mac_address": "AA:BB:CC:DD:EE:FF"},
    ]
    ssdp = [{"name": "x", "ssdp_location": "http://nas.local/d.xml"}, 5]
    got = merge_candidates(None, "nope", dhcp, [None], ssdp)
    assert [c["address"] for c in got] == ["10.0.0.3", "10.0.0.20", "nas.local"]
    assert got[1]["mac"] is None


def test_cloud_url_is_not_an_address_but_joins_by_mac():
    devices = [
        {
            "name": "Plug",
            "configuration_url": "https://control.shelly.cloud/",
            "connections": [["mac", "aa:bb:cc:dd:ee:ff"]],
        },
        {"name": "Cloud only", "configuration_url": "https://example.com/x"},
        {"name": "Local", "configuration_url": "http://printer.local/"},
    ]
    dhcp = [{"mac_address": "AA:BB:CC:DD:EE:FF", "ip_address": "10.0.0.7"}]
    got = _by_addr(merge_candidates(devices, [], dhcp, [], []))
    assert set(got) == {"10.0.0.7", "printer.local"}
    assert got["10.0.0.7"]["urls"] == ["https://control.shelly.cloud/"]
    assert got["10.0.0.7"]["name"] == "Plug"


def test_dhcp_lease_wins_over_url_address_of_a_device_with_mac():
    devices = [
        {
            "name": "Cam",
            "configuration_url": "http://10.0.0.99/",
            "connections": [["mac", "aa:bb:cc:dd:ee:ff"]],
        }
    ]
    dhcp = [{"mac_address": "AA:BB:CC:DD:EE:FF", "ip_address": "10.0.0.7"}]
    [c] = merge_candidates(devices, [], dhcp, [], [])
    assert c["address"] == "10.0.0.7"
    assert c["sources"] == ["dhcp", "registry"]


def test_non_network_configuration_url_is_not_kept():
    devices = [
        {
            "name": "Box",
            "configuration_url": "homeassistant://config/x",
            "connections": [["mac", "aa:bb:cc:dd:ee:01"]],
            "config_entries": ["e2"],
        },
    ]
    dhcp = [{"ip_address": "10.0.0.5", "mac_address": "aabbccddee01", "hostname": "box"}]
    by_addr = {c["address"]: c for c in merge_candidates(devices, ENTRIES, dhcp, [], [])}
    assert by_addr["10.0.0.5"]["urls"] == []


REGISTRY = [
    {
        "name": "shellyblugwg3-a8032ab1c2d4",
        "name_by_user": "BLU brána – půda",
        "configuration_url": "http://10.0.0.10",
        "connections": [["mac", "a8032ab1c2d4"]],
        "manufacturer": "Shelly",
        "model": "BLU Gateway Gen3",
        "config_entries": ["e1"],
    },
    {
        "name": "Kamera",
        "configuration_url": "http://admin:x@10.0.0.41/",
        "config_entries": ["e2"],
    },
]
DHCP = [
    {
        "ip_address": "10.0.0.10",
        "mac_address": "a8032ab1c2d4",
        "hostname": "shellyblugwg3-a8032ab1c2d4",
    },
    {"ip_address": "10.0.0.31", "mac_address": "c4d8d5550a2e", "hostname": "shellyplus1pm"},
    {"ip_address": "10.0.0.57", "mac_address": "e4b063f1a7c0", "hostname": "plug"},
]
ZEROCONF = [
    {
        "name": "shellyplugsg3-e4b063f1a7c0._http._tcp.local.",
        "type": "_http._tcp.local.",
        "ip_addresses": ["10.0.0.57"],
        "hostname": "shellyplugsg3-e4b063f1a7c0.local.",
    }
]


def _collected(dhcp=DHCP, zeroconf=ZEROCONF, registry_error=None):
    def slot(result=None, events=(), error=None):
        return {"result": result, "events": list(events), "error": error}

    return [
        slot(REGISTRY, error=registry_error),
        slot(ENTRIES),
        slot([], [{"add": dhcp}]),
        slot([], [{"add": zeroconf}]),
        slot(error="ssdp/subscribe_discovery failed: unknown_command"),  # optional
    ]


def test_from_ha_gives_valid_devices_marked_saved_by_ip_or_mac():
    saved = network_devices.validate(
        [
            {"name": "Brána", "address": "10.0.0.10"},  # same IP
            {"name": "Relé", "address": "boiler.lan", "mac": "C4:D8:D5:55:0A:2E"},  # same MAC
        ]
    )
    got = _by_addr(network_discovery.from_ha(_collected(), saved))
    assert set(got) == {"10.0.0.10", "10.0.0.31", "10.0.0.41", "10.0.0.57"}
    assert {a for a, c in got.items() if c["saved"]} == {"10.0.0.10", "10.0.0.31"}
    gw = got["10.0.0.10"]
    assert gw["name"] == "BLU brána – půda" and gw["kind"] == "shelly_gateway"
    assert gw["mac"] == "A8:03:2A:B1:C2:D4" and gw["urls"] == ["http://10.0.0.10"]
    assert gw["note"] == "Shelly BLU Gateway Gen3" and gw["source"] == "ha"
    assert got["10.0.0.41"]["urls"] == []  # credentials in the URL: dropped
    assert got["10.0.0.41"]["kind"] == "camera"
    assert got["10.0.0.31"]["hostname"] == ""  # a DHCP name is no .local name
    # every candidate stores as is (unknown keys dropped)
    assert network_devices.validate(list(got.values()))[0]["source"] == "ha"


def test_from_ha_needs_the_registry():
    try:
        network_discovery.from_ha(_collected(registry_error="x failed: unauthorized"), [])
    except network_discovery.policy.OpError as e:
        assert e.code == "ha_failed"
    else:
        raise AssertionError("no error")


def test_panel_reads_candidates_from_core(run_panel, monkeypatch):
    replies = {
        "config/device_registry/list": REGISTRY,
        "config_entries/get": ENTRIES,
    }
    events = {"dhcp/subscribe_discovery": DHCP, "zeroconf/subscribe_discovery": ZEROCONF}

    async def websocket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required"})
        assert (await ws.receive_json())["access_token"] == "t0k"
        await ws.send_json({"type": "auth_ok"})
        async for msg in ws:
            cmd = msg.json()
            if cmd["type"] == "ssdp/subscribe_discovery":
                error = {"code": "unknown_command", "message": "x"}
                await ws.send_json(
                    {"id": cmd["id"], "type": "result", "success": False, "error": error}
                )
                continue
            result = replies.get(cmd["type"])
            await ws.send_json(
                {"id": cmd["id"], "type": "result", "success": True, "result": result}
            )
            if cmd["type"] in events:
                add = {"add": events[cmd["type"]]}
                await ws.send_json({"id": cmd["id"], "type": "event", "event": add})
        return ws

    async def scenario(http):
        app = web.Application()
        app.router.add_get("/ws", websocket)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        monkeypatch.setattr(settings, "CORE_WS_URL", f"ws://127.0.0.1:{port}/ws")
        monkeypatch.setattr(settings, "SUPERVISOR_TOKEN", "t0k")
        try:
            ok = await http.get("/api/network-devices/ha")
            body = await ok.json()
            monkeypatch.setattr(settings, "CORE_WS_URL", "ws://127.0.0.1:1/ws")
            down = await http.get("/api/network-devices/ha")
            return ok.status, body, down.status, await down.json()
        finally:
            await asyncio.wait_for(runner.cleanup(), 5)

    monkeypatch.setattr(network_discovery, "collect", _quick(network_discovery.collect))
    status, body, down, down_body = run_panel(scenario)
    assert status == 200
    assert [c["address"] for c in body] == ["10.0.0.10", "10.0.0.31", "10.0.0.41", "10.0.0.57"]
    assert not any(c["saved"] for c in body)
    assert (down, down_body["ok"]) == (502, False)


def _quick(collect):
    async def run(http):
        return await collect(http, quiet=0.1, max_wait=2)

    return run
