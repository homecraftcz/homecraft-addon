"""Vzdálený přístup (#50): status from features.report and the Supervisor, the QR
code, the owner's remote_access.request and the certificate probe."""

import asyncio
import ssl
import time
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from connector import audit, features, panel, remote, settings
from connector.client import AskError

TAILSCALE = {
    "name": "Tailscale",
    "id": "tailscale",
    "settings": {"tailnet": "Tail1a2b3c.ts.net", "subnet": ""},
}
ADDON = {
    "state": "started",
    "version": "0.27.1",
    "options": {"userspace_networking": False, "share_homeassistant": "serve"},
}
INFO = f"/addons/{remote.ADDON}/info"


@pytest.fixture
def supervisor(monkeypatch):
    """Fake Supervisor answers by path (missing = not installed); records the
    calls. The certificate cache answers `answers["cert"]`."""
    answers = {"/host/info": {"hostname": "homeassistant"}, "calls": []}
    answers["cert"] = {"valid": True, "expires": time.time() + 80 * 86400}

    async def fake(http, path):
        answers["calls"].append(path)
        return answers.get(path)

    def cached(host, force=False):
        answers["calls"].append(f"cert {host} {force}")
        return answers["cert"], False

    monkeypatch.setattr(remote, "_supervisor", fake)
    monkeypatch.setattr(remote, "cert", cached)
    return answers


def status(force=False):
    return asyncio.run(remote.status(force))


def test_status_without_the_feature_asks_nobody(supervisor):
    features.save({"features": [], "remote_access_requested_at": 1_700_000_000})
    s = status()
    assert s["enabled"] is False and s["url"] == "" and s["addon"] is None
    assert s["requested_at"] == 1_700_000_000
    assert supervisor["calls"] == []


def test_status_feature_without_the_addon_is_off(supervisor):
    features.save({"features": [TAILSCALE]})
    s = status()
    assert s["enabled"] is False and s["url"] == "" and s["cert"] is None
    assert supervisor["calls"] == [INFO]


def test_status_with_the_addon_and_tailnet(supervisor):
    supervisor[INFO] = ADDON
    supervisor["/host/info"] = {"hostname": "Dum_HA"}
    features.save({"features": [TAILSCALE, {"name": "go2rtc", "id": "go2rtc"}]})
    s = status(force=True)
    assert s["enabled"] is True and s["https"] is True
    assert s["url"] == "https://dum-ha.tail1a2b3c.ts.net"
    assert s["tailnet"] == "tail1a2b3c.ts.net" and s["hostname"] == "dum-ha"
    assert s["addon"] == {"state": "started", "version": "0.27.1"}
    assert s["subnet"] == "" and s["cameras"] is True
    assert s["cert"]["state"] == "ok" and s["cert_pending"] is False
    assert "cert dum-ha.tail1a2b3c.ts.net True" in supervisor["calls"]
    assert "options" not in str(s)


def test_status_without_tailnet_or_go2rtc(supervisor):
    supervisor[INFO] = {"state": "stopped", "version": "0.26.0"}
    supervisor["/host/info"] = None
    features.save({"features": [{**TAILSCALE, "settings": {"subnet": "10.0.0.0/24"}}]})
    s = status()
    assert s["enabled"] is True and s["url"] == "" and s["hostname"] == "homeassistant"
    assert s["subnet"] == "10.0.0.0/24" and s["cameras"] is None
    assert s["cert"] is None  # no address, no probe
    assert not any(c.startswith("cert") for c in supervisor["calls"])


@pytest.mark.parametrize(
    "tailnet",
    [
        "evil.example/x",
        "a b.ts.net",
        "ts.net",
        "tail.example.com",
        "x.ts.net.evil.com",
        "x.ts.net\nfoo",
        "-a.ts.net",
        "x" * 80 + ".ts.net",
    ],
)
def test_status_refuses_a_tailnet_outside_ts_net(supervisor, tailnet):
    supervisor[INFO] = ADDON
    features.save({"features": [{**TAILSCALE, "settings": {"tailnet": tailnet}}]})
    s = status()
    assert s["url"] == "" and s["tailnet"] == ""


@pytest.mark.parametrize(
    ("options", "https"),
    [
        ({"share_homeassistant": "disabled"}, False),
        ({"share_homeassistant": "funnel"}, True),
        ({"proxy": False}, False),
        ({"proxy": True}, True),
        ({}, True),
    ],
)
def test_status_https_follows_the_addon_proxy(supervisor, options, https):
    supervisor[INFO] = {**ADDON, "options": options}
    features.save({"features": [TAILSCALE]})
    s = status()
    assert s["https"] is https and bool(s["url"]) is https
    assert asyncio.run(remote.url()) == s["url"]


@pytest.mark.parametrize(
    ("name", "label"),
    [
        ("Chata", "chata"),
        ("Dům_HA 2", "dm-ha-2"),
        ("ha.local", "ha"),
        ("--x--", "x"),
        ("___", "homeassistant"),
        ("", "homeassistant"),
        (None, "homeassistant"),
        ("a" * 70, "a" * 63),
    ],
)
def test_machine_name(name, label):
    assert remote.machine_name(name) == label


def test_status_hostname_option_wins(supervisor):
    supervisor[INFO] = {**ADDON, "options": {"hostname": "Chata"}}
    features.save({"features": [TAILSCALE]})
    assert status()["url"] == "https://chata.tail1a2b3c.ts.net"


@pytest.mark.parametrize(
    ("result", "state"),
    [
        (None, None),
        ({"valid": False}, "invalid"),
        ({"valid": True, "expires": time.time() + 3 * 86400}, "soon"),
        ({"valid": True, "expires": time.time() + 30 * 86400}, "ok"),
    ],
)
def test_status_certificate_states(supervisor, result, state):
    supervisor[INFO] = ADDON
    supervisor["cert"] = result
    features.save({"features": [TAILSCALE]})
    c = status()["cert"]
    assert (c and c["state"]) == state


def test_supervisor_not_installed_and_installed(monkeypatch):
    async def go():
        async def info(request):
            if request.match_info["slug"] != remote.ADDON:
                return web.json_response({"result": "error"}, status=400)
            assert request.headers["Authorization"] == "Bearer sv-token"
            return web.json_response({"result": "ok", "data": ADDON})

        app = web.Application()
        app.router.add_get("/addons/{slug}/info", info)
        async with TestServer(app) as server:
            monkeypatch.setattr(settings, "SUPERVISOR_URL", str(server.make_url("")).rstrip("/"))
            monkeypatch.setattr(settings, "SUPERVISOR_TOKEN", "sv-token")
            async with aiohttp.ClientSession() as http:
                ok = await remote._supervisor(http, INFO)
                missing = await remote._supervisor(http, "/addons/other/info")
        return ok, missing

    ok, missing = asyncio.run(go())
    assert ok == ADDON and missing is None


def test_qr_is_svg():
    svg = remote.qr_svg("https://homeassistant.tail1a2b3c.ts.net")
    assert svg.lstrip().startswith("<svg") and svg.rstrip().endswith("</svg>")
    assert "viewBox" in svg and "<script" not in svg


def test_qr_endpoint_needs_no_certificate(run_panel, supervisor):
    supervisor[INFO] = ADDON
    features.save({"features": [TAILSCALE]})

    async def scenario(http):
        r = await http.get("/api/remote/qr.svg")
        body = await r.text()
        features.save({"features": []})
        missing = await http.get("/api/remote/qr.svg")
        return r.status, r.content_type, r.headers["Content-Security-Policy"], body, missing.status

    status_, ctype, csp, body, missing = run_panel(scenario)
    assert (status_, ctype, missing) == (200, "image/svg+xml", 404)
    assert "default-src 'none'" in csp and body.lstrip().startswith("<svg")
    assert not any(c.startswith("cert") for c in supervisor["calls"])


def test_remote_endpoint_force(run_panel, supervisor):
    supervisor[INFO] = ADDON
    features.save({"features": [TAILSCALE]})

    async def scenario(http):
        r = await http.get("/api/remote")
        forced = await http.get("/api/remote?force=1")
        return r.status, await r.json(), forced.status

    code, body, forced = run_panel(scenario)
    assert code == forced == 200 and body["url"] == "https://homeassistant.tail1a2b3c.ts.net"
    host = "homeassistant.tail1a2b3c.ts.net"
    assert [c for c in supervisor["calls"] if c.startswith("cert")] == [
        f"cert {host} False",
        f"cert {host} True",
    ]


def post_request(run_panel, nonce=True, then=None):
    headers = {"X-Homecraft-Panel": panel.NONCE} if nonce else {}

    async def scenario(http):
        r = await http.post("/api/remote/request", json={}, headers=headers)
        body = await r.json() if r.content_type == "application/json" else None
        return r.status, body, (await then(http) if then else None)

    return run_panel(scenario)


def test_request_offline_is_409(run_panel):
    code, body, _ = post_request(run_panel)  # the agent is not connected: the real ask()
    assert code == 409 and body["code"] == "offline" and "není připojený" in body["error"]
    assert audit.recent()[0]["op"] == "remote_access.request"
    assert audit.recent()[0]["code"] == "offline"
    assert features.load()["remote_access_requested_at"] is None


def test_request_needs_the_nonce(run_panel, agent, monkeypatch):
    async def ask(op, payload, timeout=None):
        raise AssertionError("must not be sent")

    monkeypatch.setattr(agent, "ask", ask)
    assert post_request(run_panel, nonce=False)[0] == 403


def test_request_sends_and_keeps_requested_at(run_panel, agent, monkeypatch, supervisor):
    asked = []

    async def ask(op, payload, timeout=None):
        asked.append((op, payload))
        return {"requested_at": 1_800_000_000.5}

    async def then(http):
        return (await (await http.get("/api/remote")).json())["requested_at"]

    features.save({"features": [TAILSCALE], "technician": "Karel"})
    received = features.load()["received_at"]
    monkeypatch.setattr(agent, "ask", ask)
    code, body, shown = post_request(run_panel, then=then)
    assert (code, body) == (200, {"ok": True, "requested_at": 1_800_000_000.5})
    assert asked == [("remote_access.request", {"note": None})]
    assert audit.recent()[0]["ok"] is True
    stored = features.load()
    assert shown == stored["remote_access_requested_at"] == 1_800_000_000.5
    assert stored["technician"] == "Karel" and stored["received_at"] == received


@pytest.mark.parametrize(
    ("err", "code"),
    [
        (AskError("unknown"), 502),
        (AskError("busy", panel.HUB_BUSY), 409),
        (AskError("not_paired"), 502),
        (AskError("internal", "boom"), 502),
    ],
)
def test_request_errors(run_panel, agent, monkeypatch, err, code):
    async def ask(op, payload, timeout=None):
        raise err

    monkeypatch.setattr(agent, "ask", ask)
    status_, body, _ = post_request(run_panel)
    assert status_ == code and body["ok"] is False and body["code"] == err.code
    assert panel.HUB_BUSY not in body["error"] and "boom" not in body["error"]  # Czech texts only


class FakeWriter:
    def __init__(self, cert):
        self.cert, self.closed = cert, False

    def get_extra_info(self, name):
        assert name == "peercert"
        return self.cert

    def close(self):
        self.closed = True


def test_cert_expiry_reads_not_after(monkeypatch):
    writer = FakeWriter({"notAfter": "Feb 24 12:00:00 2027 GMT"})

    async def connect(host, port, ssl):
        assert (host, port) == ("ha.tail1a2b3c.ts.net", 443) and ssl.verify_mode
        return None, writer

    monkeypatch.setattr(asyncio, "open_connection", connect)
    result = asyncio.run(remote.cert_expiry("ha.tail1a2b3c.ts.net"))
    assert result == {"valid": True, "expires": 1803470400.0} and writer.closed


def test_cert_expiry_refused_certificate_is_invalid(monkeypatch):
    async def connect(*args, **kwargs):
        e = ssl.SSLCertVerificationError("certificate verify failed")
        e.verify_message = "certificate has expired"
        raise e

    monkeypatch.setattr(asyncio, "open_connection", connect)
    assert asyncio.run(remote.cert_expiry("ha.tail1a2b3c.ts.net")) == {"valid": False}


def test_cert_expiry_connection_failure_is_none(monkeypatch):
    async def connect(*args, **kwargs):
        raise OSError("no route")

    monkeypatch.setattr(asyncio, "open_connection", connect)
    assert asyncio.run(remote.cert_expiry("ha.tail1a2b3c.ts.net")) is None


def test_cert_expiry_timeout_is_none(monkeypatch):
    async def slow(*args, **kwargs):
        await asyncio.sleep(10)

    monkeypatch.setattr(remote, "CERT_TIMEOUT", 0.05)
    monkeypatch.setattr(asyncio, "open_connection", slow)
    started = time.monotonic()
    assert asyncio.run(remote.cert_expiry("ha.tail1a2b3c.ts.net")) is None
    assert time.monotonic() - started < 1


def test_cert_cache_probes_in_the_background_once(monkeypatch):
    """cert() answers at once; one probe per host at a time; failures are
    retried after CERT_RETRY, a valid result kept CERT_TTL; force probes again."""
    monkeypatch.setattr(remote, "_certs", {})
    monkeypatch.setattr(remote, "_cert_tasks", {})
    probes = []
    answer = {"value": None}

    async def probe(host):
        probes.append(host)
        await asyncio.sleep(0.05)
        return answer["value"]

    monkeypatch.setattr(remote, "cert_expiry", probe)
    clock = {"now": 1000.0}
    # remote's clock only: the event loop keeps the real one
    monkeypatch.setattr(
        remote, "time", SimpleNamespace(monotonic=lambda: clock["now"], time=time.time)
    )

    async def go():
        h = "ha.tail1a2b3c.ts.net"
        assert remote.cert(h) == (None, True)
        assert remote.cert(h) == (None, True)  # in flight: not started twice
        await asyncio.sleep(0.1)
        assert remote.cert(h) == (None, False) and len(probes) == 1
        clock["now"] += remote.CERT_RETRY + 1  # a failure is retried after 5 min
        answer["value"] = {"valid": True, "expires": 2e9}
        assert remote.cert(h)[1] is True
        await asyncio.sleep(0.1)
        assert remote.cert(h) == ({"valid": True, "expires": 2e9}, False)
        clock["now"] += remote.CERT_RETRY + 1  # a valid one is kept an hour
        assert remote.cert(h)[1] is False and len(probes) == 2
        assert remote.cert(h, force=True)[1] is True  # Zkontrolovat
        await asyncio.sleep(0.1)
        return len(probes)

    assert asyncio.run(go()) == 3
