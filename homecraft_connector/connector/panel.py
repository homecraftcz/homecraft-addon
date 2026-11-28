"""The add-on panel behind HA ingress (#556): the house name (HA's location_name),
state, verification and pairing code, Access token, pause, consent to error reports (#31)
with HA's system_log (homecraftcz/homecraft#906), the audit, the network devices (#631)
with their LAN scan (#633), the features Homecraft installed (homecraftcz/homecraft#636),
the live heating on Domů (#47, read only) and Topení (#48: read, plus the
five helpers of a circuit the owner sets; #49: names and window valves saved
through Homecraft's heating.save; #58: Doporučení, Homecraft's heating.advise
and heating.advice_apply) and Vzdálený přístup (#50: Tailscale's state, a QR
code of the house address, the owner's remote_access.request).

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
    house,
    http_proxy,
    network_devices,
    network_discovery,
    network_scan,
    policy,
    remote,
    selfcheck,
    settings,
    state,
    system_log,
)
from .client import AskError, Client

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


def _circuits() -> list | None:
    """The heating circuits Homecraft reported; None without heating (or from
    an older Homecraft)."""
    heating = [f for f in features.load()["features"] if isinstance(f.get("circuits"), list)]
    return [c for f in heating for c in f["circuits"]] if heating else None


def _circuit(instance: object) -> dict | None:
    return next(
        (
            c
            for c in _circuits() or []
            if isinstance(c, dict)
            and isinstance(instance, str)
            and instance
            and c.get("instance") == instance
        ),
        None,
    )


async def _house(request: web.Request) -> web.Response:
    """Live heating for Domů (#47). Without circuits in the features report
    (an older Homecraft, no heating) HA is not asked: `heating` is None."""
    circuits = _circuits()
    if circuits is None:
        return web.json_response({"heating": None, "low_batteries": []})
    try:
        return web.json_response(await house.heating_summary(circuits))
    except policy.OpError as e:
        logger.warning("Stav domu z HA nenačten: %s %s", e.code, e.message)
        return web.json_response(
            {
                "heating": None,
                "low_batteries": [],
                "error": f"Home Assistant neodpověděl ({e.code})",
            }
        )


async def _heating(request: web.Request) -> web.Response:
    """One circuit for Topení (#48), live from HA."""
    circuit = _circuit(request.query.get("circuit"))
    if circuit is None:
        return web.json_response({"ok": False, "error": "Okruh není v souhrnu"}, status=404)
    try:
        return web.json_response(await house.heating_detail(circuit))
    except policy.OpError as e:
        logger.warning("Okruh topení z HA nenačten: %s %s", e.code, e.message)
        return web.json_response(
            {"ok": False, "error": f"Home Assistant neodpověděl ({e.code})"}, status=502
        )


async def _heating_helper(request: web.Request) -> web.Response:
    """Sets one of the five owner helpers of a reported circuit (#48): only
    house.HELPERS, range-checked; never another entity or service."""
    instance = await _json_field(request, "circuit", str)
    key = await _json_field(request, "key", str)
    value = await _json_field(request, "value", object)
    if _circuit(instance) is None:
        return web.json_response({"ok": False, "error": "Okruh není v souhrnu"}, status=400)
    # The audit names the circuit and the key, never the value (audit.py: no payloads).
    target = f"{instance} {key}"
    try:
        data = await house.set_helper(instance, key, value)
    except ValueError as e:
        return web.json_response({"ok": False, "error": f"Neplatná hodnota: {e}"}, status=400)
    except LookupError as e:
        audit.record("panel.helper", target, False, "not_found")
        logger.warning("Helper %s v HA chybí", e)
        return web.json_response(
            {
                "ok": False,
                "error": "Tohle nastavení v Home Assistantu chybí; technik ho doplní Apply",
            },
            status=409,
        )
    except policy.OpError as e:
        audit.record("panel.helper", target, False, e.code)
        logger.warning("Nastavení %s selhalo: %s %s", target, e.code, e.message)
        return web.json_response(
            {"ok": False, "error": f"Home Assistant neodpověděl ({e.code})"}, status=502
        )
    value = next(v for k, v in data.items() if k != "entity_id")
    audit.record("panel.helper", target, True)
    logger.info("V panelu nastaveno %s = %s", data["entity_id"], value)
    return web.json_response({"ok": True, "value": value})


NAME_MAX = 100  # Homecraft's limit for a circuit, valve or window name
SAVE_FIELDS = {"instance", "title", "heading", "valve_names", "windows"}


def _save_name(value: object, what: str) -> str:
    if not isinstance(value, str) or len(value.strip()) > NAME_MAX:
        raise ValueError(f"{what}: text of at most {NAME_MAX} characters")
    return value.strip()


def _heating_payload(body: object) -> dict:
    """The panel's heating.save body checked against the reported circuit: only
    SAVE_FIELDS, names as text, valves and windows of that circuit. ValueError."""
    if not isinstance(body, dict) or not set(body) <= SAVE_FIELDS:
        raise ValueError(f"an object with {', '.join(sorted(SAVE_FIELDS))}")
    circuit = _circuit(body.get("instance"))
    if circuit is None:
        raise ValueError("instance: not a reported circuit")
    valves = set(house._ids([v.get("entity_id") for v in house._items(circuit.get("valves"))]))
    windows = set(house._ids([w.get("key") for w in house._items(circuit.get("windows"))]))
    out: dict = {"instance": circuit["instance"]}
    # The circuit's name cannot be cleared (Homecraft would store it empty); the
    # panel sends `heading` only as the new name too, never to clear it.
    for key in ("title", "heading"):
        if key in body:
            out[key] = _save_name(body[key], key)
            if not out[key]:
                raise ValueError(f"{key}: must not be empty")
    if "valve_names" in body:
        names = body["valve_names"]
        if not isinstance(names, dict) or not names or not set(names) <= valves:
            raise ValueError("valve_names: valves of this circuit")
        out["valve_names"] = {eid: _save_name(n, eid) for eid, n in names.items()}
    if "windows" in body:
        changes = body["windows"]
        if not isinstance(changes, dict) or not changes or not set(changes) <= windows:
            raise ValueError("windows: windows of this circuit")
        out["windows"] = {}
        for key, change in changes.items():
            if not isinstance(change, dict) or not change or not set(change) <= {"name", "valves"}:
                raise ValueError(f"windows.{key}: name and/or valves")
            w = {}
            if "name" in change:
                w["name"] = _save_name(change["name"], f"windows.{key}.name")
            if "valves" in change:
                picked = change["valves"]
                if not isinstance(picked, list) or not all(
                    isinstance(v, str) and v in valves for v in picked
                ):
                    raise ValueError(f"windows.{key}.valves: valves of this circuit")
                w["valves"] = list(dict.fromkeys(picked))
            out["windows"][key] = w
    if len(out) == 1:
        raise ValueError("nothing to save")
    return out


# heating.save errors (homecraftcz/homecraft#946): these carry a Czech message for
# the owner, shown as is; the rest are ours or the hub's (English) and get a text here.
OWNER_MESSAGES = ("invalid", "blocked", "failed", "stale")
SAVE_ERRORS = {
    "offline": "Homecraft není připojený, změny teď nejde uložit.",
    "paused": "Homecraft je pozastavený, změny teď nejde uložit.",
    "unknown": ("Spojení se přerušilo, nevíme, jestli se změna uložila – zkontrolujte za chvíli."),
    "unknown_op": "Tahle verze Homecraftu ukládání z panelu neumí.",
    "busy": "Ukládání topení už probíhá, zkuste to za chvíli.",
    "not_paired": "Doplněk není spárovaný s Homecraftem.",
    "too_large": "Změn je najednou moc, uložte je po částech.",
    "blocked": "Technik musí nejdřív vyřešit změny v Home Assistantu.",
}
SAVE_TIMEOUT = 300.0  # Plan + Apply on a slow HA can take minutes
ASK_STATUS = {
    "offline": 409,
    "paused": 409,
    "busy": 409,
    "blocked": 409,
    "stale": 409,
    "invalid": 400,
}


def _ask_error(e: AskError, op: str, target: str, texts: dict, fallback: str) -> web.Response:
    """A failed client.ask from the panel: audited, logged, answered in Czech
    (the handler's own message for OWNER_MESSAGES codes, else `texts`)."""
    audit.record(op, target, False, e.code)
    logger.warning("%s %s z panelu selhalo: %s", op, target, e.code)
    message = (e.code in OWNER_MESSAGES and e.message) or texts.get(e.code)
    return web.json_response(
        {"ok": False, "code": e.code, "error": message or f"{fallback} ({e.code})."},
        status=ASK_STATUS.get(e.code, 502),
    )


async def _heating_save(request: web.Request) -> web.Response:
    """Topení › Uložit (#49): the owner's names and window valves go to Homecraft
    as heating.save, which saves the circuit and applies heating (or refuses).
    One of the Homecraft requests the panel makes (with remote_access.request and
    the heating advice); nothing is changed here."""
    try:
        body = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="expected JSON") from None
    try:
        payload = _heating_payload(body)
    except ValueError as e:
        return web.json_response({"ok": False, "error": f"Neplatná změna: {e}"}, status=400)
    instance = payload["instance"]
    try:
        result = await request.app[CLIENT].ask("heating.save", payload, SAVE_TIMEOUT)
    except AskError as e:
        return _ask_error(e, "heating.save", instance, SAVE_ERRORS, "Uložení se nepovedlo")
    audit.record("heating.save", instance, True)
    status = result.get("status") if isinstance(result, dict) else None
    logger.info("Topení %s uloženo z panelu (%s)", instance, status)
    return web.json_response({"ok": True, "status": status})


# Topení › Doporučení (#58): heating.advise / heating.advice_apply
# (homecraftcz/homecraft#337). Homecraft analyses the circuit and keeps the
# proposed changes (10 min); the panel shows them and sends back a change_id.
ADVISE_TIMEOUT = 150.0  # HA history through the Connector + AI; Homecraft stops at ~100 s
ADVISE_DAYS = (7, 30)
CHANGE_ID_MAX = 32
ADVISE_ERRORS = {
    "offline": "Homecraft není připojený, analýzu teď nejde spustit.",
    "paused": "Homecraft je pozastavený, analýzu teď nejde spustit.",
    "unknown": "Homecraft neodpověděl včas, zkuste analýzu za chvíli znovu.",
    "unknown_op": "Tahle verze Homecraftu doporučení neumí.",
    "not_paired": "Doplněk není spárovaný s Homecraftem.",
    "busy": "Analýza topení už probíhá, zkuste to za chvíli.",
}
APPLY_ERRORS = {
    **ADVISE_ERRORS,
    "offline": "Homecraft není připojený, návrh teď nejde použít.",
    "paused": "Homecraft je pozastavený, návrh teď nejde použít.",
    "unknown": "Spojení se přerušilo, nevíme, jestli se návrh použil – spusťte analýzu znovu.",
    "busy": "Topení se právě ukládá, zkuste to za chvíli.",
    "stale": "Návrh vypršel, spusťte analýzu znovu.",
}


def _ha_user(request: web.Request) -> str | None:
    """The HA user in the panel, from the ingress headers (only the Supervisor's
    proxy reaches us, _guard); for Homecraft's history, never for permissions."""
    h = request.headers
    return h.get("X-Remote-User-Display-Name") or h.get("X-Remote-User-Name") or None


def _advice_payload(body: object, op: str) -> dict:
    """The panel's heating.advise / heating.advice_apply body checked: a
    reported circuit, `days` 7–30 (advise, optional), a `change_id` (apply).
    ValueError."""
    if not isinstance(body, dict) or _circuit(body.get("instance")) is None:
        raise ValueError("instance: not a reported circuit")
    out = {"instance": body["instance"]}
    if op == "heating.advise" and "days" in body:
        days = body["days"]
        if type(days) is not int or not ADVISE_DAYS[0] <= days <= ADVISE_DAYS[1]:
            raise ValueError(f"days: {ADVISE_DAYS[0]}–{ADVISE_DAYS[1]}")
        out["days"] = days
    if op == "heating.advice_apply":
        change_id = body.get("change_id")
        if not isinstance(change_id, str) or not 0 < len(change_id) <= CHANGE_ID_MAX:
            raise ValueError(f"change_id: text of at most {CHANGE_ID_MAX} characters")
        out["change_id"] = change_id
    return out


async def _advice(request: web.Request, op: str, timeout: float, texts: dict, fallback: str):
    """POST body checked and asked as `op`: (instance, result) or the error response."""
    try:
        body = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="expected JSON") from None
    try:
        payload = _advice_payload(body, op)
    except ValueError as e:
        return None, web.json_response(
            {"ok": False, "error": f"Neplatný požadavek: {e}"}, status=400
        )
    if op == "heating.advice_apply" and (ha_user := _ha_user(request)):
        payload["ha_user"] = ha_user
    instance = payload["instance"]
    try:
        result = await request.app[CLIENT].ask(op, payload, timeout)
    except AskError as e:
        return None, _ask_error(e, op, instance, texts, fallback)
    audit.record(op, instance, True)
    return instance, result if isinstance(result, dict) else {}


async def _heating_advise(request: web.Request) -> web.Response:
    """Doporučení › Analyzovat: Homecraft reads the circuit's history, schedules
    and boiler profile and answers with the summary and findings, passed on as is."""
    instance, result = await _advice(
        request, "heating.advise", ADVISE_TIMEOUT, ADVISE_ERRORS, "Analýza se nepovedla"
    )
    if instance is None:
        return result
    return web.json_response({**result, "ok": True})


async def _heating_advice_apply(request: web.Request) -> web.Response:
    """Doporučení › Použít: Homecraft writes the change it proposed (change_id)
    to the circuit's schedules; nothing is written to HA here."""
    instance, result = await _advice(
        request, "heating.advice_apply", SAVE_TIMEOUT, APPLY_ERRORS, "Použití se nepovedlo"
    )
    if instance is None:
        return result
    logger.info("Doporučení pro topení %s použito z panelu", instance)
    return web.json_response(
        {"ok": True, "status": result.get("status"), "applied": result.get("applied") or []}
    )


async def _remote(request: web.Request) -> web.Response:
    """Vzdálený přístup (#50): Tailscale's state; read only. `?force=1`
    (Zkontrolovat) probes the certificate again, in the background."""
    return web.json_response(await remote.status(request.query.get("force") == "1"))


async def _remote_qr(request: web.Request) -> web.Response:
    """The house address as a QR code for `<img>` (the CSP's img-src 'self')."""
    url = await remote.url()
    if not url:
        raise web.HTTPNotFound()
    return web.Response(text=remote.qr_svg(url), content_type="image/svg+xml")


REQUEST_ERRORS = {
    "offline": "Homecraft není připojený, žádost teď nejde odeslat.",
    "paused": "Homecraft je pozastavený, žádost teď nejde odeslat.",
    "unknown": "Spojení se přerušilo, nevíme, jestli žádost dorazila – zkuste to za chvíli.",
    "unknown_op": "Tahle verze Homecraftu žádost z panelu neumí.",
    "not_paired": "Doplněk není spárovaný s Homecraftem.",
    "busy": "Homecraft je zaneprázdněný, zkuste to za chvíli.",
}


async def _remote_request(request: web.Request) -> web.Response:
    """Požádat technika o zapnutí (#50): remote_access.request to Homecraft
    (homecraftcz/homecraft#947), which records it for the technician (once in
    24 h). Nothing is switched on here or by Homecraft."""
    try:
        result = await request.app[CLIENT].ask("remote_access.request", {"note": None})
    except AskError as e:
        return _ask_error(
            e, "remote_access.request", "tailscale", REQUEST_ERRORS, "Žádost se nepovedla"
        )
    audit.record("remote_access.request", "tailscale", True)
    logger.info("Majitel požádal o vzdálený přístup z panelu")
    at = features._unix(result.get("requested_at") if isinstance(result, dict) else None)
    at = at or time.time()
    features.mark_remote_access_requested(at)  # until Homecraft's next report
    return web.json_response({"ok": True, "requested_at": at})


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
    app.router.add_get("/api/house", _house)
    app.router.add_get("/api/heating", _heating)
    app.router.add_post("/api/heating/helper", _heating_helper)
    app.router.add_post("/api/heating/save", _heating_save)
    app.router.add_post("/api/heating/advise", _heating_advise)
    app.router.add_post("/api/heating/advice/apply", _heating_advice_apply)
    app.router.add_get("/api/remote", _remote)
    app.router.add_get("/api/remote/qr.svg", _remote_qr)
    app.router.add_post("/api/remote/request", _remote_request)
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
