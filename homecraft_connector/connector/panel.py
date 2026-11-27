"""The add-on panel behind HA ingress (#556): the house name (HA's location_name),
state, verification and pairing code, Access token, pause, consent to error reports (#31)
with HA's system_log (homecraftcz/homecraft#906), the audit, the network devices (#631)
with their LAN scan (#633) and the features Homecraft installed (homecraftcz/homecraft#636).

Only the Supervisor's ingress proxy may talk to it (HA add-on docs: allow
172.30.32.2, deny everyone else); CONNECTOR_DEV=1 drops that check for the
dev run without a Supervisor. The Access token and the HA admin token are
write-only: no response carries them, `GET /api/status` says only
`token_set` / `ha_token_set`.

State-changing requests carry a per-start nonce that only the panel page
holds. Limit: a script on the same origin (anything else served through HA)
can fetch the page and read the nonce; HA's own frontend can do far more
anyway, so the nonce is against other origins and blind requests."""

import asyncio
import logging
import secrets
import time
from pathlib import Path

import aiohttp
from aiohttp import web

from . import (
    access_token,
    audit,
    features,
    fingerprint,
    ha_token,
    http_proxy,
    network_devices,
    network_discovery,
    network_scan,
    policy,
    selfcheck,
    settings,
    state,
    system_log,
)
from .client import Client

logger = logging.getLogger(__name__)

INGRESS_PROXY = "172.30.32.2"
PORT = 8099
# State-changing requests must carry NONCE in it: a cross-site form cannot set
# a header, a cross-site fetch with one needs a CORS preflight we never answer,
# and nobody but the page knows the value.
CSRF_HEADER = "X-Homecraft-Panel"
NONCE = secrets.token_urlsafe(32)
PAGE = (
    (Path(__file__).parent / "panel.html").read_text(encoding="utf-8").replace("{{NONCE}}", NONCE)
)
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    # Inline script and style only; HA shows the panel in its own iframe.
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self'; frame-ancestors 'self'; base-uri 'none'; "
        "form-action 'none'"
    ),
}

CLIENT = web.AppKey("client", Client)
PAIRING_CODE = web.AppKey("pairing_code", str)
VERIFICATION_CODE = web.AppKey("verification_code", str)


@web.middleware
async def _guard(request: web.Request, handler):
    if not settings.DEV and request.remote != INGRESS_PROXY:
        raise web.HTTPForbidden()
    if request.method not in ("GET", "HEAD") and (
        not secrets.compare_digest(request.headers.get(CSRF_HEADER, "").encode(), NONCE.encode())
        or request.headers.get("Sec-Fetch-Site", "same-origin") != "same-origin"
    ):
        raise web.HTTPForbidden()
    return await handler(request)


async def _security_headers(request: web.Request, response: web.StreamResponse) -> None:
    """On every response, errors (HTTPException) included."""
    response.headers.update(SECURITY_HEADERS)


async def _page(request: web.Request) -> web.Response:
    return web.Response(text=PAGE, content_type="text/html")


# The panel header's house name (HA's location_name): looked up again at most
# every HA_NAME_TTL seconds, so a rename shows without an add-on restart.
HA_NAME_TTL = 600.0
HA_NAME_RETRY = 60.0  # after a failed lookup
_house_cache: dict = {"value": None, "at": None, "task": None}


async def _lookup_house_name() -> None:
    """Records the time once the result is in: a failure is retried after HA_NAME_RETRY."""
    try:
        async with aiohttp.ClientSession(timeout=selfcheck.TIMEOUT) as http:
            name = await asyncio.wait_for(fingerprint._ha_name(http), fingerprint.LOOKUP_TIMEOUT)
        _house_cache["value"] = name or _house_cache["value"]
        _house_cache["at"] = time.monotonic()
    except Exception as e:
        _house_cache["at"] = time.monotonic() - HA_NAME_TTL + HA_NAME_RETRY
        logger.debug("Název domu zatím nezjištěn: %s", type(e).__name__)


def _house_name() -> str | None:
    """The cached name at once (None until the first lookup ends); a stale one is
    refreshed in the background, so the status never waits for HA."""
    at, task = _house_cache["at"], _house_cache["task"]
    stale = at is None or time.monotonic() - at > HA_NAME_TTL
    if stale and (task is None or task.done()):
        _house_cache["task"] = asyncio.create_task(_lookup_house_name())
    return _house_cache["value"]


async def _status(request: web.Request) -> web.Response:
    client = request.app[CLIENT]
    return web.json_response(
        {
            "ha_name": _house_name(),
            "connected": client.connected,
            "server_url": client.server_url,
            "version": client.version,
            "last_error": client.last_error,
            "last_connected": client.last_connected,
            "pairing_code": request.app[PAIRING_CODE],
            "verification_code": request.app[VERIFICATION_CODE],
            "pending_approval": client.pending_approval,
            "token_set": access_token.active() is not None,
            "token_pending": access_token.pending(),  # pushed by Homecraft, not yet through Access
            "token_updated_at": access_token.updated_by_homecraft(),
            "ha_token_set": ha_token.load() is not None,
            "paused": client.paused,
            "error_reports": client.error_reports,
            "system_log": state.load_system_log(),
            "audit_24h": audit.summary(time.time() - 86400),
        }
    )


async def _audit(request: web.Request) -> web.Response:
    return web.json_response(audit.recent())


async def _json_field(request: web.Request, name: str, kind: type) -> object:
    try:
        value = (await request.json())[name]
    except (ValueError, KeyError, TypeError):
        raise web.HTTPBadRequest(text=f"expected JSON with {name}") from None
    if not isinstance(value, kind):
        raise web.HTTPBadRequest(text=f"{name} must be {kind.__name__}")
    return value


async def _save_token(request: web.Request) -> web.Response:
    token = await _json_field(request, "token", str)
    try:
        access_token.save(token)
    except ValueError:
        # Never echo the input: it is (part of) the secret.
        return web.json_response(
            {"ok": False, "error": "Token má tvar <client_id>:<client_secret>"}, status=400
        )
    logger.info("Přístupový token uložen v panelu")
    request.app[CLIENT].reconnect_now()
    return web.json_response({"ok": True})


async def _clear_token(request: web.Request) -> web.Response:
    access_token.clear()
    logger.info("Přístupový token smazán v panelu")
    request.app[CLIENT].reconnect_now()  # drop the connection made with it
    return web.json_response({"ok": True})


async def _save_ha_token(request: web.Request) -> web.Response:
    token = await _json_field(request, "token", str)
    try:
        ha_token.save(token)
    except ValueError:
        return web.json_response({"ok": False, "error": "Tohle není token HA"}, status=400)
    logger.info("Token správce HA uložen v panelu")
    return web.json_response({"ok": True})


async def _clear_ha_token(request: web.Request) -> web.Response:
    ha_token.clear()
    logger.info("Token správce HA smazán v panelu")
    return web.json_response({"ok": True})


async def _pause(request: web.Request) -> web.Response:
    paused = await _json_field(request, "paused", bool)
    state.save_paused(paused)
    await request.app[CLIENT].set_paused(paused)
    logger.info("Homecraft %s v panelu", "pozastaven" if paused else "znovu povolen")
    return web.json_response({"ok": True, "paused": paused})


async def _error_reports(request: web.Request) -> web.Response:
    """Consent to send error reports; errors.Forwarder follows it within FLUSH_EVERY."""
    enabled = await _json_field(request, "enabled", bool)
    state.save_error_reports(enabled)
    request.app[CLIENT].error_reports = enabled
    logger.info("Chybová hlášení Homecraftu %s v panelu", "zapnuta" if enabled else "vypnuta")
    status = await system_log.switch(enabled)
    return web.json_response({"ok": True, "error_reports": enabled, "system_log": status})


# The restart and the wait for HA after it; one at a time.
_RESTART_LOCK = asyncio.Lock()
_restart_tasks: set[asyncio.Task] = set()
RESTART_DOWN_WAIT = 120.0  # seconds HA may take to go down after the restart call
RESTART_MAX_WAIT = 600.0
RESTART_POLL = 1.0  # short: the down phase of a restart lasts seconds
RESTART_ERRORS = {
    "conflict": "Kontrola konfigurace HA neprošla (konflikt se system_log)",
    "ha_config_invalid": (
        "Konfigurace Home Assistantu má chybu, která nesouvisí s doplňkem; "
        "Home Assistant by restart odmítl"
    ),
    "restart_failed": "Restart Home Assistantu selhal",
}


async def _ha_up(http: http_proxy.HttpProxy) -> bool:
    try:
        reply = await asyncio.wait_for(http.core("GET", "/api/"), 10)
    except (policy.OpError, TimeoutError):
        return False
    return reply["status"] == 200


async def _wait(http: http_proxy.HttpProxy, up: bool, seconds: float) -> bool:
    """Until HA's API is up (or down); False after `seconds`."""
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while await _ha_up(http) != up:
        if loop.time() > end:
            return False
        await asyncio.sleep(RESTART_POLL)
    return True


async def _after_restart(client: Client) -> None:
    """Waits for HA to go down and come back, then rechecks (the consent read
    again under system_log.lock). HA never down: the restart did not happen."""
    try:
        async with aiohttp.ClientSession() as session:
            http = http_proxy.HttpProxy(session)
            if not await _wait(http, False, RESTART_DOWN_WAIT):
                logger.warning("Home Assistant se po restartu nevypnul")
                await system_log.note("restart_not_done")
                return
            if not await _wait(http, True, RESTART_MAX_WAIT):
                logger.warning("Home Assistant po restartu neodpovídá")
                return
            await system_log.recheck(session, lambda: client.error_reports)
    except Exception:
        logger.exception("Kontrola po restartu HA selhala")
    finally:
        _RESTART_LOCK.release()


async def _system_log_restart(request: web.Request) -> web.Response:
    """Restarts HA (only after a clean check_config) and probes once it is back."""
    if _RESTART_LOCK.locked():
        return web.json_response({"ok": False, "error": "Restart už běží"}, status=409)
    await _RESTART_LOCK.acquire()
    try:
        async with aiohttp.ClientSession() as session:
            problem = await system_log.restart(http_proxy.HttpProxy(session))
    except BaseException as e:
        _RESTART_LOCK.release()
        if isinstance(e, policy.OpError):
            return web.json_response(
                {"ok": False, "error": f"Home Assistant neodpověděl ({e.code})"}, status=502
            )
        raise
    if problem:
        _RESTART_LOCK.release()
        reason, detail = problem
        return web.json_response(
            {"ok": False, "error": f"{RESTART_ERRORS[reason]}: {detail}"}, status=409
        )
    logger.info("Restart Home Assistantu z panelu")
    task = asyncio.create_task(_after_restart(request.app[CLIENT]))
    _restart_tasks.add(task)
    task.add_done_callback(_restart_tasks.discard)
    return web.json_response({"ok": True})


async def _system_log_check(request: web.Request) -> web.Response:
    """**Zkontrolovat znovu**: system_log.recheck (HA restarted another way, the
    customer fixed the config, a failed switch)."""
    client = request.app[CLIENT]
    async with aiohttp.ClientSession() as session:
        status = await system_log.recheck(session, lambda: client.error_reports)
    return web.json_response({"ok": True, "system_log": status})


async def _reconnect(request: web.Request) -> web.Response:
    request.app[CLIENT].reconnect_now()
    return web.json_response({"ok": True})


async def _updates(request: web.Request) -> web.Response:
    client = request.app[CLIENT]
    return web.json_response(
        {"connected": client.connected, "paused": client.paused, "items": client.updates}
    )


UPDATE_ERRORS = {
    "offline": "Connector není připojený",
    "paused": "Homecraft je pozastavený",
}


async def _run_update(request: web.Request) -> web.Response:
    kind = await _json_field(request, "kind", str)
    item = await _json_field(request, "item", str)
    try:
        await request.app[CLIENT].run_update(kind, item)
    except RuntimeError as e:
        return web.json_response(
            {"ok": False, "error": UPDATE_ERRORS.get(str(e), UPDATE_ERRORS["offline"])}, status=409
        )
    except ValueError:
        return web.json_response({"ok": False, "error": "Položka není v seznamu"}, status=400)
    return web.json_response({"ok": True})


async def _features(request: web.Request) -> web.Response:
    """Read on each request: a new summary shows without a restart."""
    return web.json_response(features.load())


async def _network_devices(request: web.Request) -> web.Response:
    return web.json_response(network_devices.load())


async def _save_network_devices(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="expected JSON") from None
    try:
        stored = network_devices.save(body)
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    logger.info("Síťová zařízení uložena v panelu (%d)", len(stored))
    return web.json_response(stored)


async def _network_devices_seen(request: web.Request) -> web.Response:
    """Confirm new scan finds (the banner's "Vše v pořádku")."""
    addresses = await _json_field(request, "addresses", object)
    if addresses != "all" and not (
        isinstance(addresses, list) and all(isinstance(a, str) for a in addresses)
    ):
        raise web.HTTPBadRequest(text='addresses: a list of addresses or "all"')
    return web.json_response(network_devices.mark_seen(addresses))


async def _ha_candidates(request: web.Request) -> web.Response:
    """Network device candidates from HA (#632); read-only, saves nothing."""
    try:
        async with aiohttp.ClientSession() as http:
            r = await network_discovery.collect(http)
        return web.json_response(network_discovery.from_ha(r, network_devices.load()))
    except policy.OpError as e:
        logger.warning("Načtení zařízení z HA selhalo: %s %s", e.code, e.message)
        return web.json_response(
            {"ok": False, "error": f"Home Assistant neodpověděl ({e.code})"}, status=502
        )


_SCAN_LOCK = asyncio.Lock()


async def _scan(request: web.Request) -> web.Response:
    """The recommended LAN scan (#633): only a person in the panel starts it
    (POST, so the nonce guard applies); Homecraft has no operation for it.
    Saves only hostnames missing on saved devices; new ones go through PUT."""
    profiles = await _json_field(request, "profiles", list)
    if not profiles or not all(isinstance(p, str) and p in network_scan.PROFILES for p in profiles):
        raise web.HTTPBadRequest(text=f"profiles: some of {', '.join(network_scan.PROFILES)}")
    if _SCAN_LOCK.locked():
        return web.json_response({"ok": False, "error": "Sken už běží"}, status=409)
    async with _SCAN_LOCK:
        try:
            async with aiohttp.ClientSession() as http:
                result = await network_scan.scan(http, set(profiles))
        except network_scan.ScanError as e:
            logger.warning("Sken sítě odmítnut: %s", e)
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        found, saved = network_scan.mark(result.pop("found"), network_devices.load())
        if any(d["filled"] for d in found):
            network_devices.save(saved)
    logger.info(
        "Sken sítě %s: %d adres, nalezeno %d, %.1f s",
        ", ".join(result["subnets"]),
        result["addresses"],
        len(found),
        result["seconds"],
    )
    return web.json_response({**result, "devices": found})


def make_app(client: Client, pairing_code: str, verification_code: str) -> web.Application:
    # The network device list is the largest body (200 devices with URLs and notes).
    app = web.Application(middlewares=[_guard], client_max_size=1024 * 1024)
    app.on_response_prepare.append(_security_headers)
    app[CLIENT] = client
    app[PAIRING_CODE] = pairing_code
    app[VERIFICATION_CODE] = verification_code
    app.router.add_get("/", _page)
    app.router.add_get("/api/status", _status)
    app.router.add_get("/api/audit", _audit)
    app.router.add_post("/api/access-token", _save_token)
    app.router.add_delete("/api/access-token", _clear_token)
    app.router.add_post("/api/ha-token", _save_ha_token)
    app.router.add_delete("/api/ha-token", _clear_ha_token)
    app.router.add_post("/api/pause", _pause)
    app.router.add_post("/api/error-reports", _error_reports)
    app.router.add_post("/api/system-log/restart", _system_log_restart)
    app.router.add_post("/api/system-log/check", _system_log_check)
    app.router.add_post("/api/reconnect", _reconnect)
    app.router.add_get("/api/updates", _updates)
    app.router.add_post("/api/updates/run", _run_update)
    app.router.add_get("/api/features", _features)
    app.router.add_get("/api/network-devices", _network_devices)
    app.router.add_put("/api/network-devices", _save_network_devices)
    app.router.add_post("/api/network-devices/seen", _network_devices_seen)
    app.router.add_get("/api/network-devices/ha", _ha_candidates)
    app.router.add_post("/api/network-devices/scan", _scan)
    return app


async def start(client: Client, pairing_code: str, verification_code: str) -> web.AppRunner:
    # All interfaces: the ingress proxy reaches the add-on on the hassio network
    # (no host port is published); _guard turns away anyone but the proxy.
    runner = web.AppRunner(make_app(client, pairing_code, verification_code), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    return runner
