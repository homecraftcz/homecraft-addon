"""`system_log: fire_event: true` for the error reports (homecraftcz/homecraft#906).

HA fires `system_log_event` only with it, and reads it only at start. With the
consent on, the add-on writes its own package `packages/homecraft_system_log.yaml`
(merged with the customer's `system_log:` by HA) and removes it with the consent
off; HA restarts only from the panel. Where the customer configures
`fire_event` themselves (configuration.yaml, `!include`, another package), the
add-on writes nothing and the panel says what to add. HA tells nothing about
the `system_log` it runs with: `probe` writes one warning and waits for its event.

The status `{"state", "hint", "message"}` is kept in /data/state.json:
`off`, `restart_needed`, `active`, `own_on`, `own_config`, `no_packages`,
`conflict`, `error`."""

import asyncio
import base64
import json
import logging
import os

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
PROBE_LOGGER = "homecraft.connector"  # not errors.LOGGER_RE: never forwarded
PROBE_TIMEOUT = 5.0

HINT_OWN = (
    "V configuration.yaml máš vlastní `system_log:`. Doplň do něj `fire_event: true` "
    "a restartuj Home Assistant, jinak chyby automatizací Homecraftu z logu HA nedorazí."
)
HINT_TAGGED = (
    "`system_log:` je načtený z jiného souboru (`!include`). Doplň do něj `fire_event: true` "
    "a restartuj Home Assistant, jinak chyby automatizací Homecraftu z logu HA nedorazí."
)
HINT_NO_PACKAGES = (
    "Konfigurace nenačítá složku packages/. Doplň do configuration.yaml `system_log:` "
    "s `fire_event: true` a restartuj Home Assistant."
)
HINT_CONFLICT = (
    "Home Assistant ohlásil konflikt s jiným nastavením `system_log` (text z HA níže). "
    "Soubor jsme nezapsali; nastav `fire_event: true` tam, kde `system_log` máš."
)
HINT_ERROR = (
    "Nastavení se nepodařilo, soubor jsme nezapsali. Zkus Zkontrolovat znovu "
    "nebo přepni souhlas znovu."
)


def status(state: str, hint: str | None = None, message: str | None = None) -> dict:
    return {"state": state, "hint": hint, "message": message}


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


def _path():
    return settings.CONFIG_ROOT / FILE


def ours(path) -> bool:
    """Our file: never a symlink, the first line is MARK."""
    if path.is_symlink() or not path.is_file():
        return False
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.readline().rstrip("\r\n") == MARK


def _write() -> None:
    """Atomic, 0644, like core_config._write."""
    path = _path()
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_name(path.name + files.TMP_SUFFIX)
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    os.fchmod(fd, 0o644)  # past the umask
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(CONTENT)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _remove() -> bool:
    """Removes the file only when it is ours."""
    path = _path()
    if not ours(path):
        return False
    path.unlink()
    return True


async def check(http) -> str | None:
    """None when HA's check_config is clean, else what it reported: like
    core_config._check, but a duplicate `system_log` key in a package HA
    reports only in `warnings` (`result` stays valid)."""
    reply = await http.core("POST", "/api/config/core/check_config")
    try:
        result = json.loads(base64.b64decode(reply["body_b64"]) or b"null")
    except ValueError:
        result = None
    if not isinstance(result, dict):
        return f"HTTP {reply['status']}"
    warnings = result.get("warnings")
    if warnings and "system_log" in str(warnings):
        return str(warnings)[:500]
    if reply["status"] == 200 and result.get("result") == "valid":
        return None
    if result.get("errors"):
        return str(result["errors"])[:500]
    return f"HTTP {reply['status']}"


_lock = asyncio.Lock()


async def enable(http, previous: dict | None = None) -> dict:
    """Writes the package when the customer's config leaves room for it;
    `http.core(method, path)` calls HA's REST API. `previous`: the saved status,
    kept when the file is already there."""
    async with _lock:
        text = await asyncio.to_thread(core_config._read)
        own = own_config(text)
        if own == "on":
            return status("own_on")
        if own == "off":
            return status("own_config", HINT_OWN)
        if own == "tagged":
            return status("own_config", HINT_TAGGED)
        if not packages_included(text):
            return status("no_packages", HINT_NO_PACKAGES)
        path = _path()
        existed = ours(path)
        if not existed and (path.exists() or path.is_symlink()):
            return status("conflict", HINT_CONFLICT, f"{FILE} už existuje a není náš")
        await asyncio.to_thread(_write)
        try:
            problem = await check(http)
        except policy.OpError as e:
            await asyncio.to_thread(_remove)
            logger.warning("Kontrola konfigurace HA selhala: %s %s", e.code, e.message)
            return status("error", HINT_ERROR, f"Home Assistant neodpověděl ({e.code})")
        except BaseException:
            await asyncio.to_thread(_remove)
            raise
        if problem:
            await asyncio.to_thread(_remove)
            return status("conflict", HINT_CONFLICT, problem)
        if existed and previous and previous.get("state") == "active":
            return previous
        return status("restart_needed")


async def disable() -> dict:
    """Removes our file; HA keeps firing the events until it restarts, the
    add-on just stops subscribing."""
    async with _lock:
        removed = await asyncio.to_thread(_remove)
    return status("restart_needed" if removed else "off")


async def switch(enabled: bool) -> dict:
    """enable/disable for the consent switch, saved; a failure is the status
    `error`, never an exception (the consent and homecraft_error work without it)."""
    try:
        if enabled:
            async with aiohttp.ClientSession() as session:
                result = await enable(http_proxy.HttpProxy(session), state.load_system_log())
        else:
            result = await disable()
    except Exception as e:
        logger.exception("Nastavení system_log selhalo")
        result = status("error", HINT_ERROR, getattr(e, "code", type(e).__name__))
    state.save_system_log(result)
    return result


async def probe(session: aiohttp.ClientSession) -> bool:
    """Does HA fire `system_log_event` now? Writes one warning to HA's log."""
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
                    "message": "Homecraft Connector: test of error reports",
                    "level": "warning",
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


async def restart(http) -> str | None:
    """Restarts HA only after a clean check_config; the problem otherwise."""
    if problem := await check(http):
        return problem
    try:
        reply = await http.core("POST", "/api/services/homeassistant/restart")
    except policy.OpError as e:
        if e.code == "ha_unreachable":
            return None  # HA dropped the connection: the restart is running
        raise
    return None if reply["status"] < 400 else f"HTTP {reply['status']}"
