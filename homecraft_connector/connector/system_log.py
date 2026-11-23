"""`system_log: fire_event: true` for the error reports (homecraftcz/homecraft#906).

HA fires `system_log_event` only with it, and reads it only at start. With the
consent on, the add-on writes its own package `packages/homecraft_system_log.yaml`
(merged with the customer's `system_log:` by HA) and removes it with the consent
off; HA restarts only from the panel. Where the customer configures
`fire_event` themselves (configuration.yaml, `!include`, another package), the
add-on writes nothing and the panel says what to add. HA tells nothing about
the `system_log` it runs with: `probe` writes one error line and waits for its event.

The status `{"state", "reason", "detail"}` is kept in /data/state.json; the
panel turns state and reason into text, `detail` is HA's own message. States:
`off`, `restart_needed` (reason `probe_failed`, `restart_not_done`), `active`,
`own_on`, `own_config` (reason `own_off`, `tagged`), `no_packages`, `conflict`,
`ha_config_invalid`, `error` (reason: the error code)."""

import asyncio
import json
import logging
from collections.abc import Callable

import aiohttp
import yaml

from . import core_config, files, http_proxy, policy, settings, state, ws_proxy

logger = logging.getLogger(__name__)

FILE = "packages/homecraft_system_log.yaml"
MARK = "# Homecraft Connector: system_log_event for error reports"
CONTENT = (
    f"{MARK}\n# Managed by the add-on: switch the consent off in its panel to remove it.\n"
    "system_log:\n  fire_event: true\n"
)
# Not errors.LOGGER_RE: the Forwarder never sends it. Level error: a
# `logger: default: error` would drop a warning before system_log sees it.
PROBE_LOGGER = "homecraft.connector"
PROBE_TIMEOUT = 5.0

# enable/disable/probe and their saved status: one at a time.
lock = asyncio.Lock()


def status(state: str, reason: str | None = None, detail: str | None = None) -> dict:
    return {"state": state, "reason": reason, "detail": detail}


def own_config(text: str) -> str | None:
    """How configuration.yaml sets `system_log`: `tagged` (behind a tag, or we
    cannot tell), `on` (fire_event true), `off` (fire_event anything else),
    None (no system_log or no fire_event)."""
    try:
        data = yaml.load(text, Loader=core_config._Loader)  # noqa: S506 - safe loader subclass
    except yaml.YAMLError:
        return "tagged"
    if data is None:
        return None
    if not isinstance(data, dict):
        return "tagged"
    block = data.get("system_log")
    if block is None:
        return None
    if not isinstance(block, dict):
        return "tagged"
    if "fire_event" not in block:
        return None
    return "on" if block["fire_event"] is True else "off"


def packages_included(text: str) -> bool:
    return bool(core_config.PACKAGES_RE.search(text))


def ours(path) -> bool:
    """Our file: never a symlink, the first line is MARK."""
    if path.is_symlink() or not path.is_file():
        return False
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.readline().rstrip("\r\n") == MARK


def _remove() -> bool:
    """Removes the file only when it is ours."""
    if not ours(settings.CONFIG_ROOT / FILE):
        return False
    files.delete(FILE)
    return True


async def check(http) -> tuple[str, str] | None:
    """None when HA's check_config is clean, else (`conflict` | `ha_config_invalid`,
    HA's message): only a problem naming system_log/fire_event is ours."""
    problem = await core_config._check(http, warnings_about="system_log")
    if problem is None:
        return None
    ours_ = "system_log" in problem or "fire_event" in problem
    return ("conflict" if ours_ else "ha_config_invalid", problem)


async def enable(http, previous: dict | None = None) -> dict:
    """Writes the package when the customer's config leaves room for it
    (callers hold `lock`); `http.core(method, path)` calls HA's REST API.
    `previous`: the saved status, kept when the file is already there."""
    text = await asyncio.to_thread(core_config._read)
    own = own_config(text)
    if own == "on":
        return status("own_on")
    if own is not None:
        return status("own_config", "own_off" if own == "off" else "tagged")
    if not packages_included(text):
        return status("no_packages")
    path = settings.CONFIG_ROOT / FILE
    existed = ours(path)
    if not existed:
        if path.exists() or path.is_symlink():
            return status("conflict", detail=f"{FILE} exists and is not the add-on's")
        await asyncio.to_thread(files.write, FILE, CONTENT)
    try:
        problem = await check(http)
    except BaseException as e:
        # Unchecked: a new file goes, one HA may already run with stays.
        if not existed:
            await asyncio.to_thread(_remove)
        if isinstance(e, policy.OpError):
            logger.warning("Kontrola konfigurace HA selhala: %s %s", e.code, e.message)
            return status("error", e.code)
        raise
    if problem and problem[0] == "conflict":
        await asyncio.to_thread(_remove)
        return status("conflict", detail=problem[1])
    if problem:  # HA's own config is broken, not by our file: it stays
        return status("ha_config_invalid", detail=problem[1])
    if existed and previous and previous.get("state") in ("active", "restart_needed"):
        return previous
    return status("restart_needed")


async def disable() -> dict:
    """Removes our file; HA keeps firing the events until it restarts, the
    add-on just stops subscribing."""
    removed = await asyncio.to_thread(_remove)
    return status("restart_needed" if removed else "off")


async def switch(enabled: bool) -> dict:
    """enable/disable for the consent switch, saved; a failure is the status
    `error`, never an exception (the consent and homecraft_error work without it)."""
    async with lock:
        try:
            if enabled:
                async with aiohttp.ClientSession() as session:
                    result = await enable(http_proxy.HttpProxy(session), state.load_system_log())
            else:
                result = await disable()
        except Exception as e:
            logger.exception("Nastavení system_log selhalo")
            result = status("error", getattr(e, "code", type(e).__name__))
        state.save_system_log(result)
        return result


async def recheck(session: aiohttp.ClientSession, consent_on: Callable[[], bool]) -> dict:
    """After a restart, on add-on start and from **Zkontrolovat znovu**: a waiting
    state (`restart_needed`, `own_on`) is probed, any other is enabled again (the
    customer may have fixed the config), with the consent off only a pending
    restart is probed. Saved; the consent is read under the lock."""
    async with lock:
        saved = state.load_system_log()
        if not consent_on():
            if saved["state"] == "restart_needed" and not await probe(session):
                saved = status("off")
        elif saved["state"] in ("restart_needed", "own_on"):
            saved = (
                status("active")
                if await probe(session)
                else {**saved, "reason": "probe_failed", "detail": None}
            )
        elif saved["state"] != "active":
            try:
                saved = await enable(http_proxy.HttpProxy(session), saved)
            except Exception as e:
                logger.exception("Nastavení system_log selhalo")
                saved = status("error", getattr(e, "code", type(e).__name__))
            if saved["state"] == "own_on" and await probe(session):
                saved = status("active")
        state.save_system_log(saved)
        return saved


async def note(reason: str) -> None:
    """Keeps the state, records why it is still waiting."""
    async with lock:
        state.save_system_log({**state.load_system_log(), "reason": reason})


async def startup(consent_on: Callable[[], bool]) -> None:
    """Once on add-on start, next to the connection: the consent on since 0.23.0
    adds the file, a restart done elsewhere is recognised (one probe line)."""
    if not consent_on() or state.load_system_log()["state"] == "active":
        return
    try:
        async with aiohttp.ClientSession() as session:
            await recheck(session, consent_on)
    except Exception:
        logger.exception("system_log se nepodařilo nastavit")


async def probe(session: aiohttp.ClientSession) -> bool:
    """Does HA fire `system_log_event` now? Writes one error line to HA's log."""
    try:
        ws = await ws_proxy.connect(session)
    except policy.OpError:
        return False
    try:
        await ws.send_json({"id": 1, "type": "subscribe_events", "event_type": "system_log_event"})
        await ws.send_json(
            {
                "id": 2,
                "type": "call_service",
                "domain": "system_log",
                "service": "write",
                "service_data": {
                    "message": "Homecraft Connector: test of error reports (not an error)",
                    "level": "error",
                    "logger": PROBE_LOGGER,
                },
            }
        )
        async with asyncio.timeout(PROBE_TIMEOUT):
            async for m in ws:
                if m.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    msg = json.loads(m.data)
                except ValueError:
                    continue
                data = (msg.get("event") or {}).get("data") if isinstance(msg, dict) else None
                if isinstance(data, dict) and data.get("name") == PROBE_LOGGER:
                    return True
    except (TimeoutError, aiohttp.ClientError, OSError, AttributeError):
        pass
    finally:
        await ws.close()
    return False


async def restart(http) -> tuple[str, str] | None:
    """Restarts HA only after a clean check_config; else (`conflict` |
    `ha_config_invalid` | `restart_failed`, detail)."""
    if problem := await check(http):
        return problem
    try:
        reply = await http.core("POST", "/api/services/homeassistant/restart")
    except policy.OpError as e:
        if e.code == "ha_unreachable":
            return None  # HA dropped the connection: the restart is running
        raise
    return None if reply["status"] < 400 else ("restart_failed", f"HTTP {reply['status']}")
