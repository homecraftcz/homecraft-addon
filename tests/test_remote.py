"""Vzdálený přístup (#50): status from features.report and the Supervisor, the QR
code, the owner's remote_access.request and the certificate probe."""

import asyncio
import time

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
ADDON = {"state": "started", "version": "0.27.1", "options": {"userspace_networking": False}}


@pytest.fixture
def supervisor(monkeypatch):
    """Fake Supervisor answers by path (missing = not installed); records the calls."""
    answers = {"/host/info": {"hostname": "homeassistant"}}
    calls = []

    async def fake(http, path):
        calls.append(path)
        return answers.get(path)

    async def no_cert(host):
        calls.append(f"cert {host}")
        return 1_800_000_000.0

    monkeypatch.setattr(remote, "_supervisor", fake)
    monkeypatch.setattr(remote, "cert_expiry", no_cert)
    answers["calls"] = calls
    return answers


def status():
    return asyncio.run(remote.status())


def test_status_without_the_feature_asks_nobody(supervisor):
    features.save({"features": [], "remote_access_requested_at": 1_700_000_000})
    s = status()
    assert s["enabled"] is False and s["url"] == "" and s["addon"] is None
    assert s["requested_at"] == 1_700_000_000
    assert supervisor["calls"] == []


def test_status_feature_without_the_addon_is_off(supervisor):
    features.save({"features": [TAILSCALE]})
    s = status()
    assert s["enabled"] is False and s["url"] == "" and s["cert_expires"] is None
    assert supervisor["calls"] == [f"/addons/{remote.ADDON}/info"]


def test_status_with_the_addon_and_tailnet(supervisor):
    supervisor[f"/addons/{remote.ADDON}/info"] = ADDON
    supervisor["/host/info"] = {"hostname": "Dum-HA"}
    features.save({"features": [TAILSCALE, {"name": "go2rtc", "id": "go2rtc"}]})
    s = status()
    assert s["enabled"] is True
    assert s["url"] == "https://dum-ha.tail1a2b3c.ts.net"
    assert s["tailnet"] == "tail1a2b3c.ts.net" and s["hostname"] == "dum-ha"
    assert s["addon"] == {"state": "started", "version": "0.27.1"}
    assert s["subnet"] == "" and s["cameras"] is True
    assert s["cert_expires"] == 1_800_000_000.0
    assert "options" not in str(s)


def test_status_without_tailnet_or_go2rtc(supervisor):
    supervisor[f"/addons/{remote.ADDON}/info"] = {"state": "stopped", "version": "0.26.0"}
    supervisor["/host/info"] = None
    features.save({"features": [{**TAILSCALE, "settings": {"subnet": "10.0.0.0/24"}}]})
    s = status()
    assert s["enabled"] is True and s["url"] == "" and s["hostname"] == "homeassistant"
    assert s["subnet"] == "10.0.0.0/24" and s["cameras"] is None
    assert s["cert_expires"] is None  # no address, no probe
    assert not any(c.startswith("cert") for c in supervisor["calls"])


@pytest.mark.parametrize(
    "tailnet",
    ["evil.example/x", "a b.ts.net", "ts", "x.ts.net\nfoo", "-a.ts.net", "x" * 80 + ".net"],
)
def test_status_refuses_a_tailnet_that_is_not_a_dns_name(supervisor, tailnet):
    supervisor[f"/addons/{remote.ADDON}/info"] = ADDON
    features.save({"features": [{**TAILSCALE, "settings": {"tailnet": tailnet}}]})
    assert status()["url"] == ""


def test_status_hostname_option_wins(supervisor):
    supervisor[f"/addons/{remote.ADDON}/info"] = {**ADDON, "options": {"hostname": "Chata"}}
    features.save({"features": [TAILSCALE]})
    assert status()["url"] == "https://chata.tail1a2b3c.ts.net"


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
                ok = await remote._supervisor(http, f"/addons/{remote.ADDON}/info")
                missing = await remote._supervisor(http, "/addons/other/info")
        return ok, missing

    ok, missing = asyncio.run(go())
    assert ok == ADDON and missing is None


def test_qr_is_svg():
    svg = remote.qr_svg("https://homeassistant.tail1a2b3c.ts.net")
    assert svg.lstrip().startswith("<svg") and svg.rstrip().endswith("</svg>")
    assert "viewBox" in svg and "<script" not in svg


def test_qr_endpoint(run_panel, supervisor):
    supervisor[f"/addons/{remote.ADDON}/info"] = ADDON
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


def test_remote_endpoint(run_panel, supervisor):
    supervisor[f"/addons/{remote.ADDON}/info"] = ADDON
    features.save({"features": [TAILSCALE]})

    async def scenario(http):
        r = await http.get("/api/remote")
        return r.status, await r.json()

    code, body = run_panel(scenario)
    assert code == 200 and body["url"] == "https://homeassistant.tail1a2b3c.ts.net"


def post_request(run_panel, nonce=True):
    headers = {"X-Homecraft-Panel": panel.NONCE} if nonce else {}

    async def scenario(http):
        r = await http.post("/api/remote/request", json={}, headers=headers)
        return r.status, (await r.json() if r.content_type == "application/json" else None)

    return run_panel(scenario)


def test_request_offline_is_409(run_panel):
    code, body = post_request(run_panel)  # the agent is not connected: the real ask()
    assert code == 409 and body["code"] == "offline" and "není připojený" in body["error"]
    assert audit.recent()[0]["op"] == "remote_access.request"
    assert audit.recent()[0]["code"] == "offline"


def test_request_needs_the_nonce(run_panel, agent, monkeypatch):
    async def ask(op, payload, timeout=None):
        raise AssertionError("must not be sent")

    monkeypatch.setattr(agent, "ask", ask)
    assert post_request(run_panel, nonce=False)[0] == 403


def test_request_sends_remote_access_request(run_panel, agent, monkeypatch):
    asked = []

    async def ask(op, payload, timeout=None):
        asked.append((op, payload))
        return {"requested_at": 1_800_000_000.5}

    monkeypatch.setattr(agent, "ask", ask)
    code, body = post_request(run_panel)
    assert (code, body) == (200, {"ok": True, "requested_at": 1_800_000_000.5})
    assert asked == [("remote_access.request", {"note": None})]
    assert audit.recent()[0]["ok"] is True


@pytest.mark.parametrize(
    ("err", "code"), [(AskError("unknown"), 502), (AskError("busy", "Server busy"), 502)]
)
def test_request_errors(run_panel, agent, monkeypatch, err, code):
    async def ask(op, payload, timeout=None):
        raise err

    monkeypatch.setattr(agent, "ask", ask)
    status_, body = post_request(run_panel)
    assert status_ == code and body["ok"] is False and body["code"] == err.code


def test_cert_expiry_timeout_is_none_and_cached(monkeypatch):
    calls = []

    async def slow(*args, **kwargs):
        calls.append(args)
        await asyncio.sleep(10)

    monkeypatch.setattr(remote, "_certs", {})
    monkeypatch.setattr(remote, "CERT_TIMEOUT", 0.05)
    monkeypatch.setattr(asyncio, "open_connection", slow)
    started = time.monotonic()
    assert asyncio.run(remote.cert_expiry("homeassistant.tail1a2b3c.ts.net")) is None
    assert time.monotonic() - started < 1
    assert asyncio.run(remote.cert_expiry("homeassistant.tail1a2b3c.ts.net")) is None
    assert len(calls) == 1


def test_cert_expiry_failure_is_none(monkeypatch):
    monkeypatch.setattr(remote, "_certs", {})
    # .invalid never resolves (RFC 6761)
    assert asyncio.run(remote.cert_expiry("homeassistant.hfb.invalid")) is None
