"""Recommended LAN scan (#633): /shelly parser, subnet limits, the connection
bound against fake hosts, probes against local servers and matching with the
saved list. No real network: hosts are fakes or 127.0.0.1."""

import asyncio
import time

import aiohttp
import pytest
from aiohttp import web

from connector import network_devices, network_scan, settings

GEN1 = {
    "type": "SHSW-1",
    "mac": "A4CF12F3A1B2",
    "auth": False,
    "fw": "20230913-112003/v1.14.0-gcb84623",
    "num_outputs": 1,
}
GEN3_GW = {
    "name": None,
    "id": "shellyblugwg3-a8032ab1c2d4",
    "mac": "A8032AB1C2D4",
    "model": "S3GW-1DBT001",
    "gen": 3,
    "ver": "1.4.4",
    "app": "BluGwG3",
    "auth_en": False,
}


def _info(*addresses):
    return {
        "interfaces": [
            {"interface": "eth0", "ipv4": {"address": list(addresses)}},
            {"interface": "wlan0", "ipv4": None},
        ]
    }


def test_parse_shelly_gen2_plus():
    d = network_scan.parse_shelly(GEN3_GW)
    assert d == {
        "name": "Shelly BluGwG3",
        "kind": "shelly_gateway",
        "hostname": "shellyblugwg3-a8032ab1c2d4.local",
        "mac": "A8032AB1C2D4",
        "note": "Gen3 · FW 1.4.4 · S3GW-1DBT001",
    }
    named = network_scan.parse_shelly(
        {**GEN3_GW, "id": "shellyplus1pm-c4d8d5550a2e", "app": "Plus1PM", "name": "Kotel"}
    )
    assert named["name"] == "Kotel" and named["kind"] == "shelly"


def test_parse_shelly_gen1_has_no_hostname():
    d = network_scan.parse_shelly(GEN1)
    assert d["name"] == "Shelly SHSW-1" and d["kind"] == "shelly"
    assert d["hostname"] == "" and d["note"].startswith("Gen1 · FW 20230913")


def test_parse_shelly_rejects_other_shapes():
    for data in (None, [], {"mac": 1}, {"mac": "A4CF12F3A1B2"}, {"id": "x", "gen": 2}):
        assert network_scan.parse_shelly(data) is None
    bad_id = network_scan.parse_shelly({**GEN3_GW, "id": "bad id!"})
    assert bad_id["hostname"] == ""


def test_subnets_private_and_small_only():
    assert [str(n) for n in network_scan.subnets(_info("10.0.0.5/24", "10.0.0.6/24"))] == [
        "10.0.0.0/24"
    ]
    assert [str(n) for n in network_scan.subnets(_info("192.168.4.2/22"))] == ["192.168.4.0/22"]
    # a public or too big subnet next to a good one is skipped
    assert [str(n) for n in network_scan.subnets(_info("8.8.8.8/24", "172.16.1.1/24"))] == [
        "172.16.1.0/24"
    ]
    for addr, why in (
        ("8.8.8.8/24", "není soukromá"),
        ("100.64.0.1/24", "není soukromá"),  # CGNAT / Tailscale
        ("10.0.0.5/21", "větší než /22"),
        ("192.168.0.2/16", "větší než /22"),
    ):
        with pytest.raises(network_scan.ScanError, match=why):
            network_scan.subnets(_info(addr))
    with pytest.raises(network_scan.ScanError, match="žádnou IPv4"):
        network_scan.subnets({"interfaces": []})


def test_connection_bound_and_slow_hosts_do_not_block(monkeypatch):
    """254 hosts that never answer plus one open: at most 64 connections at
    once, each cut at TIMEOUT, so the whole sweep takes ~4 timeouts."""
    monkeypatch.setattr(network_scan, "TIMEOUT", 0.05)
    now = {"open": 0, "max": 0}

    class Writer:
        def close(self):
            pass

    async def fake_open(ip, port):
        now["open"] += 1
        now["max"] = max(now["max"], now["open"])
        try:
            if ip == "10.0.0.200":
                return None, Writer()
            await asyncio.sleep(60)  # a slow host: only the timeout ends it
        finally:
            now["open"] -= 1

    monkeypatch.setattr(network_scan, "open_connection", fake_open)
    hosts = [f"10.0.0.{i}" for i in range(1, 255)]

    async def go():
        async with aiohttp.ClientSession() as http:
            p = network_scan.Prober(http)
            start = time.monotonic()
            got = await asyncio.gather(*(p.is_open(ip, 445) for ip in hosts))
            return got, time.monotonic() - start

    got, elapsed = asyncio.run(go())
    assert [ip for ip, ok in zip(hosts, got, strict=True) if ok] == ["10.0.0.200"]
    assert now["max"] == network_scan.MAX_CONNECTIONS
    assert elapsed < 1.5  # ceil(254 / 64) * 0.05 s plus slack, not 254 * 0.05


def _serve(routes, scenario):
    async def go():
        app = web.Application()
        for method, path, handler in routes:
            app.router.add_route(method, path, handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as http:
                return await scenario(network_scan.Prober(http), port)
        finally:
            await runner.cleanup()

    return asyncio.run(go())


def test_probes_against_local_servers():
    async def shelly(request):
        return web.json_response(GEN3_GW)

    async def onvif(request):
        assert "GetDeviceInformation" in await request.text()
        return web.Response(
            text="<env:Envelope><env:Body><tds:GetDeviceInformationResponse>"
            "<tds:Manufacturer>HIKVISION</tds:Manufacturer><tds:Model>DS-2CD2143G2</tds:Model>"
            "</tds:GetDeviceInformationResponse></env:Body></env:Envelope>"
        )

    async def syno(request):
        return web.json_response({"data": {}, "success": True})

    async def hang(request):
        await asyncio.sleep(1)

    routes = [
        ("GET", "/shelly", shelly),
        ("POST", "/onvif/device_service", onvif),
        ("GET", "/webapi/query.cgi", syno),
        ("GET", "/slow", hang),
    ]

    async def scenario(p, port):
        start = time.monotonic()
        slow = await p._http("GET", f"http://127.0.0.1:{port}/slow")
        slow_s = time.monotonic() - start
        return (
            await p.shelly("127.0.0.1", port),
            await p.onvif("127.0.0.1", port),
            await p.synology("127.0.0.1", port),
            slow,
            slow_s,
        )

    sh, cam, nas, slow, slow_s = _serve(routes, scenario)
    assert sh["hostname"] == "shellyblugwg3-a8032ab1c2d4.local"
    assert cam == {"Manufacturer": "HIKVISION", "Model": "DS-2CD2143G2", "FirmwareVersion": None}
    assert nas is True
    assert slow is None and slow_s < 2


def test_rtsp_options(monkeypatch):
    async def go():
        async def handle(reader, writer):
            line = await reader.readline()
            assert line.startswith(b"OPTIONS rtsp://")
            writer.write(b"RTSP/1.0 200 OK\r\nCSeq: 1\r\n\r\n")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server, aiohttp.ClientSession() as http:
            return await network_scan.Prober(http).rtsp("127.0.0.1", port)

    assert asyncio.run(go()) is True


def test_mark_matches_saved_by_ip_mac_or_name_and_fills_hostname():
    saved = network_devices.validate(
        [
            {"name": "Brána", "address": "10.0.0.10"},  # same IP, no hostname
            {"name": "Relé", "address": "boiler.lan", "mac": "c4:d8:d5:55:0a:2e"},  # same MAC
            {"name": "Kamera", "address": "10.0.0.41", "hostname": "cam.local"},
        ]
    )
    found = [
        {"address": "10.0.0.10", **network_scan.parse_shelly(GEN3_GW)},
        {
            "address": "10.0.0.31",
            **network_scan.parse_shelly(
                {**GEN3_GW, "id": "shellyplus1pm-c4d8d5550a2e", "mac": "C4D8D5550A2E"}
            ),
        },
        {"address": "10.0.0.41", "name": "Kamera, výrobce neznámý", "kind": "camera"},
        {"address": "10.0.0.57", "name": "Sdílení SMB", "kind": "nas", "note": "SMB 445"},
    ]
    out, filled = network_scan.mark(found, saved)
    got = {d["address"]: d for d in out}
    assert [d["saved"] for d in out] == [True, True, True, False]
    assert [d["filled"] for d in out] == [True, True, False, False]
    assert got["10.0.0.57"]["source"] == "scan"
    assert [d["hostname"] for d in filled] == [
        "shellyblugwg3-a8032ab1c2d4.local",
        "shellyplus1pm-c4d8d5550a2e.local",
        "cam.local",
    ]
    assert saved[0]["hostname"] == ""  # the input is not changed
    network_devices.validate(out)  # each one stores as is


def test_panel_scan(run_panel, monkeypatch):
    """The endpoint needs the nonce (POST), refuses a public subnet and fills
    a saved device's hostname; the scan itself is faked."""
    network_devices.save([{"name": "Brána", "address": "10.0.0.10"}])

    async def fake_scan(http, profiles):
        assert profiles == {"shelly"}
        return {
            "subnets": ["10.0.0.0/24"],
            "addresses": 253,
            "seconds": 0.1,
            "found": [{"address": "10.0.0.10", **network_scan.parse_shelly(GEN3_GW)}],
        }

    monkeypatch.setattr(network_scan, "scan", fake_scan)
    from connector import panel

    async def scenario(http):
        headers = {panel.CSRF_HEADER: panel.NONCE}
        no_nonce = await http.post("/api/network-devices/scan", json={"profiles": ["shelly"]})
        bad = await http.post(
            "/api/network-devices/scan", json={"profiles": ["ssh"]}, headers=headers
        )
        ok = await http.post(
            "/api/network-devices/scan", json={"profiles": ["shelly"]}, headers=headers
        )
        return no_nonce.status, bad.status, ok.status, await ok.json()

    no_nonce, bad, status, body = run_panel(scenario)
    assert (no_nonce, bad, status) == (403, 400, 200)
    assert body["devices"][0]["filled"] is True
    assert network_devices.load()[0]["hostname"] == "shellyblugwg3-a8032ab1c2d4.local"


def test_scan_refuses_without_supervisor(monkeypatch):
    monkeypatch.setattr(settings, "SUPERVISOR_URL", "")

    async def go():
        async with aiohttp.ClientSession() as http:
            await network_scan.scan(http, {"shelly"})

    with pytest.raises(network_scan.ScanError):
        asyncio.run(go())


def test_probe_host_picks_one_device_per_address():
    class Stub:
        def __init__(self, up, shelly=None, onvif=None, rtsp=False, syno=False):
            self.up, self._shelly, self._onvif, self._rtsp, self._syno = (
                up,
                shelly,
                onvif,
                rtsp,
                syno,
            )

        async def is_open(self, ip, port):
            return port in self.up

        async def shelly(self, ip):
            return self._shelly

        async def onvif(self, ip, port):
            return self._onvif

        async def rtsp(self, ip):
            return self._rtsp

        async def synology(self, ip, port):
            return self._syno

    every = set(network_scan.PROFILES)

    def run(stub, profiles=every):
        return asyncio.run(network_scan.probe_host(stub, "10.0.0.5", profiles))

    assert run(Stub(set())) is None
    shelly = run(Stub({80}, shelly=network_scan.parse_shelly(GEN3_GW)))
    assert shelly["kind"] == "shelly_gateway" and shelly["urls"] == ["http://10.0.0.5"]
    assert run(Stub({554}, rtsp=True))["name"] == "Kamera, výrobce neznámý"
    onvif = {"Manufacturer": "Reolink", "Model": "RLC-510A", "FirmwareVersion": None}
    assert run(Stub({8000, 554}, onvif=onvif))["name"] == "Kamera Reolink"
    assert run(Stub({5001}, syno=True))["urls"] == ["https://10.0.0.5:5001"]
    assert run(Stub({445}))["name"] == "Sdílení SMB"
    assert run(Stub({80}))["kind"] == "other"  # web
    assert run(Stub({80}), {"shelly", "camera", "nas"}) is None  # web is opt-in
