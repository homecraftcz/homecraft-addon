"""Shelly BLU TRV valves of a heating circuit made uniform (trv.sync / trv.status):
firmware updated, then the circuit's Interface and Behavior settings, which
Homecraft sends, set on every valve and read back to verify.

The valves and their gateways come from the heating library's
`sensor.heating_gateways_<circuit>` ({gateway url: {valves: {climate: BT mac}}});
on a gateway a valve is its `blutrv:<id>` component with that `addr`. Everything
goes over the gateway's local RPC (Shelly BLU Gateway Gen3, no auth: the same
calls the heating flow's gateway watchdog makes), only to a private IPv4.

trv.check reads the same without changing anything: firmware against the
gateway's latest and each wanted field against the valve's (a change made in
the gateway's web UI shows as a difference with both values); trv.sync is then
the way back. A firmware update over BLE takes minutes, so both start a job and answer
at once; trv.status reports its progress and the result per valve. One job per
circuit; valves of one gateway one after another, gateways in parallel. Jobs
live in memory: an add-on restart forgets them (Homecraft starts it again)."""

import asyncio
import copy
import ipaddress
import logging
import time
from urllib.parse import urlsplit

import aiohttp

from . import house, policy

logger = logging.getLogger(__name__)

RPC_TIMEOUT = 70.0  # BluTrv.Call: "response may be delayed up to one minute"
UPDATE_TIMEOUT = 15 * 60.0  # one valve's BLE firmware update
VERIFY_TIMEOUT = 120.0  # until the gateway's copy of the valve's config shows the change
POLL = 15.0

# What Homecraft may set (Shelly BLU TRV API): Behavior = TRV.SetConfig, Interface
# = Sys.SetConfig `ui`. Never enable, targets of the moment, schedules or names.
FLAGS = ("floor_heating", "accel", "auto_calibrate", "anticlog", "power_save", "silent_mode")


def _bool(v):
    if not isinstance(v, bool):
        raise ValueError("true or false")
    return v


def _number(low: float, high: float, *, integer=False):
    def check(v):
        ok = isinstance(v, (int, float)) and not isinstance(v, bool)
        if not ok or not low <= v <= high or (integer and v != int(v)):
            raise ValueError(f"{'an integer' if integer else 'a number'} {low:g}–{high:g}")
        return int(v) if integer else float(v)

    return check


def _units(v):
    if v not in ("C", "F"):
        raise ValueError("C or F")
    return v


TRV_FIELDS = {
    "min_valve_position": _number(0, 99, integer=True),
    "max_compensation_C": _number(0, 5),
    "default_boost_duration": _number(1, 2**31 - 1, integer=True),
    "default_override_duration": _number(1, 2**31 - 1, integer=True),
    "default_override_target_C": _number(4, 30),
    "min_target_C": _number(4, 29.9),
    "max_target_C": _number(4.1, 30),
}
UI_FIELDS = {
    "lock": _bool,
    "flip": _bool,
    "brightness": _number(1, 7, integer=True),
    "t_units": _units,
}

HASSIO_NET = ipaddress.ip_network("172.30.32.0/23")

_jobs: dict[str, dict] = {}  # instance -> the job trv.status reports
_tasks: set[asyncio.Task] = set()


def parse_config(config: object, *, empty: bool = False) -> dict:
    """trv.sync `config` checked field by field: {"trv": {...}, "ui": {...}},
    flags as {name: bool}. ValueError naming the field."""
    if not isinstance(config, dict) or not set(config) <= {"trv", "ui"}:
        raise ValueError("config: trv and/or ui")
    out: dict = {}
    for part, fields in (("trv", {**TRV_FIELDS, "flags": None}), ("ui", UI_FIELDS)):
        given = config.get(part, {})
        if not isinstance(given, dict) or not set(given) <= set(fields):
            raise ValueError(f"config.{part}: some of {', '.join(fields)}")
        checked = {}
        for key, value in given.items():
            try:
                if key == "flags":
                    if not isinstance(value, dict) or not set(value) <= set(FLAGS):
                        raise ValueError(f"some of {', '.join(FLAGS)}")
                    checked[key] = {f: _bool(b) for f, b in value.items()}
                else:
                    checked[key] = fields[key](value)
            except ValueError as e:
                raise ValueError(f"config.{part}.{key}: {e}") from None
        if checked:
            out[part] = checked
    if not out and not empty:  # trv.check may read only the firmware
        raise ValueError("config: nothing to set")
    trv = out.get("trv", {})
    if trv.get("min_target_C", 4) >= trv.get("max_target_C", 30):
        raise ValueError("config.trv: min_target_C under max_target_C")
    return out


def differences(wanted: dict, remote: dict) -> list[dict]:
    """The wanted fields the valve's config (BluTrv.GetRemoteConfig `config`)
    does not have, as {field, want, have}; flags come back as a list of the set
    ones. `have` None: the valve does not report the field."""
    trv = remote.get("trv:0") if isinstance(remote.get("trv:0"), dict) else {}
    ui = (remote.get("sys") or {}).get("ui") if isinstance(remote.get("sys"), dict) else None
    ui = ui if isinstance(ui, dict) else {}
    have = {f"ui.{k}": ui.get(k) for k in wanted.get("ui", {})}
    want = {f"ui.{k}": v for k, v in wanted.get("ui", {}).items()}
    for key, value in wanted.get("trv", {}).items():
        if key == "flags":
            flags = trv.get("flags") if isinstance(trv.get("flags"), list) else None
            for f, on in value.items():
                want[f"flags.{f}"] = on
                have[f"flags.{f}"] = None if flags is None else f in flags
        else:
            want[key], have[key] = value, trv.get(key)
    return [
        {"field": k, "want": v, "have": have[k]} for k, v in want.items() if not _same(v, have[k])
    ]


def _same(want, have) -> bool:
    """Equal; numbers within 0.05 (the valve may keep 21.0 for 21, or round)."""
    if isinstance(want, bool) or isinstance(have, bool):  # 1 is no True here
        return isinstance(want, bool) and isinstance(have, bool) and want == have
    if isinstance(want, (int, float)) and isinstance(have, (int, float)):
        return abs(want - have) < 0.05
    return want == have


def gateway_url(url: object) -> str | None:
    """http://<private IPv4>[:port] of a gateway, or None. The URL is a state
    attribute (the heating sensor copies it from the gateway device's
    configuration_url), so nothing else: no names, no public hosts, not the
    hassio network."""
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
        ip = ipaddress.IPv4Address(parts.hostname or "")
        port = parts.port or 80
    except ValueError:
        return None
    if parts.scheme != "http" or not any(ip in n for n in policy.TAILSCALE_ROUTE_NETS):
        return None
    if ip in HASSIO_NET:  # the Supervisor and add-ons, never a gateway
        return None
    return f"http://{ip}:{port}"


class RpcError(Exception):
    pass


async def rpc(http: aiohttp.ClientSession, url: str, method: str, params: dict | None = None):
    """One JSON-RPC call to a gateway; its result or RpcError."""
    body = {"id": 1, "method": method, **({"params": params} if params is not None else {})}
    try:
        async with http.post(
            f"{url}/rpc", json=body, timeout=aiohttp.ClientTimeout(total=RPC_TIMEOUT)
        ) as r:
            data = await r.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as e:
        raise RpcError(f"{method}: {type(e).__name__}") from None
    if not isinstance(data, dict):
        raise RpcError(f"{method}: not JSON-RPC")
    if "error" in data:
        err = data["error"] if isinstance(data["error"], dict) else {}
        raise RpcError(f"{method}: {err.get('code')} {str(err.get('message'))[:200]}")
    return data.get("result")


async def blutrv_ids(http: aiohttp.ClientSession, url: str) -> dict[str, int]:
    """BT mac (lower case) -> BluTrv id of the gateway's valves."""
    out, offset = {}, 0
    while True:
        page = await rpc(
            http,
            url,
            "Shelly.GetComponents",
            {"dynamic_only": True, "include": ["config"], "offset": offset},
        )
        components = page.get("components") if isinstance(page, dict) else None
        if not isinstance(components, list) or not components:
            return out
        for c in components:
            config = c.get("config") if isinstance(c, dict) else None
            key = str(c.get("key", "")) if isinstance(c, dict) else ""
            if key.startswith("blutrv:") and isinstance(config, dict):
                if isinstance(config.get("addr"), str) and isinstance(config.get("id"), int):
                    out[config["addr"].lower()] = config["id"]
        offset += len(components)
        if offset >= (page.get("total") or 0):
            return out


async def _fw(http, url: str, tid: int) -> str:
    info = await rpc(http, url, "BluTrv.GetRemoteDeviceInfo", {"id": tid})
    info = info.get("device_info") if isinstance(info, dict) else None
    fw = info.get("fw_id") if isinstance(info, dict) else None
    return fw if isinstance(fw, str) else ""


async def _valve(
    http, url: str, v: dict, config: dict, update: bool, latest: str, apply: bool
) -> None:
    """Update and set one valve, recording each step in `v` (the job's entry);
    without `apply` only read: is an update available, which fields differ."""
    tid = v["id"]
    v["fw"] = before = await _fw(http, url, tid)
    if not apply:
        if update:
            v["update"] = (
                "unknown"
                if not (latest and before)
                else ("current" if before == latest else "available")
            )
        remote = await rpc(http, url, "BluTrv.GetRemoteConfig", {"id": tid})
        remote = remote.get("config") if isinstance(remote, dict) else None
        v["diff"] = differences(config, remote if isinstance(remote, dict) else {})
        v["config"] = "mismatch" if v["diff"] else "ok"
        return
    if update and latest and before and before != latest:
        v["update"] = "running"
        await rpc(http, url, "BluTrv.UpdateFirmware", {"id": tid})
        # Done once the version changes: a valve on another build than the
        # gateway's fw_id must not hold its gateway's other valves forever.
        deadline = time.monotonic() + UPDATE_TIMEOUT
        while v["fw"] in (before, ""):
            if time.monotonic() > deadline:  # still set it up below
                v["update"] = "failed"
                v["error"] = f"update: still {before} after {UPDATE_TIMEOUT / 60:g} min"
                break
            await asyncio.sleep(POLL)
            try:
                v["fw"] = await _fw(http, url, tid)
            except RpcError:
                pass  # the valve restarts while it updates
        else:
            v["update"] = "updated"
    elif update:
        v["update"] = "current" if latest and before == latest else "unknown"
    v["config"] = "running"
    if "trv" in config:
        params = {"id": 0, "config": config["trv"]}
        await rpc(
            http, url, "BluTrv.Call", {"id": tid, "method": "TRV.SetConfig", "params": params}
        )
    if "ui" in config:
        params = {"config": {"ui": config["ui"]}}
        await rpc(
            http, url, "BluTrv.Call", {"id": tid, "method": "Sys.SetConfig", "params": params}
        )
    deadline = time.monotonic() + VERIFY_TIMEOUT
    v["diff"] = None  # nothing read back yet: never "ok"
    while True:
        try:
            remote = await rpc(http, url, "BluTrv.GetRemoteConfig", {"id": tid})
            remote = remote.get("config") if isinstance(remote, dict) else None
            v["diff"] = differences(config, remote if isinstance(remote, dict) else {})
        except RpcError:
            if time.monotonic() > deadline:
                raise
        if v["diff"] == [] or time.monotonic() > deadline:
            break
        await asyncio.sleep(POLL)
    v["config"] = "mismatch" if v["diff"] else "ok"


def _failed(valves: list[dict], error: str) -> None:
    for v in valves:
        if v["config"] in ("pending", "running"):
            v.update(config="failed", error=error)
        if v["update"] in ("pending", "running"):
            v["update"] = "failed"


async def _gateway(
    http, gw: dict, valves: list[dict], config: dict, update: bool, apply: bool
) -> None:
    """One gateway's valves, one BLE peer at a time; every error ends in `valves`."""
    url = gateway_url(gw.get("url"))
    if url is None:
        return _failed(valves, "brána nemá lokální adresu IPv4")
    try:
        ids = await blutrv_ids(http, url)
    except Exception as e:
        return _failed(
            valves, f"brána neodpověděla: {e if isinstance(e, RpcError) else type(e).__name__}"
        )
    for v in valves:
        v["id"] = ids.get(v["mac"])
        if v["id"] is None:
            _failed([v], "hlavice není na bráně spárovaná")
        elif update:
            v["update"] = "pending"
    latest = ""
    if update:
        try:
            found = await rpc(http, url, "BluTrv.CheckForUpdates")
            latest = found.get("fw_id") if isinstance(found, dict) else ""
            latest = latest if isinstance(latest, str) else ""
        except RpcError as e:
            logger.warning("Brána %s: kontrola firmwaru selhala: %s", url, e)
    for v in valves:
        if v["id"] is None:
            continue
        try:
            await _valve(http, url, v, config, update, latest, apply)
        except Exception as e:
            _failed([v], str(e) if isinstance(e, RpcError) else type(e).__name__)


def _entry(eid: str, gw: dict, mac: str) -> dict:
    return {
        "entity_id": eid,
        "gateway": str(gw.get("name") or gw.get("url")),
        "mac": mac,
        "id": None,
        "fw": "",
        "update": "skipped",
        "config": "pending",
        "diff": [],
        "error": "",
    }


async def _run(job: dict, gateways: dict, config: dict, update: bool, apply: bool) -> None:
    try:
        async with aiohttp.ClientSession() as http:
            work = []
            for gw in gateways.values():
                if isinstance(gw, dict) and isinstance(gw.get("valves"), dict):
                    valves = [_entry(e, gw, str(m).lower()) for e, m in gw["valves"].items()]
                    job["valves"] += valves
                    work.append(_gateway(http, gw, valves, config, update, apply))
            await asyncio.gather(*work)
    except Exception as e:  # never a silent end: trv.status must say it stopped
        logger.warning("Sjednocení hlavic %s selhalo: %s", job["instance"], type(e).__name__)
        job["error"] = type(e).__name__
    finally:
        job["running"] = False
        job["finished_at"] = time.time()
        ok = sum(v["config"] == "ok" for v in job["valves"])
        logger.info("Hlavice %s sjednoceny: %d z %d", job["instance"], ok, len(job["valves"]))


async def start(instance: str, config: object, update: object, apply: bool = True) -> dict:
    """trv.sync (`apply`) or trv.check (only read and compare): checks, reads
    the circuit's gateways from HA, starts the job.
    policy.OpError: bad_request (naming the field), busy, not_found, or HA's."""
    try:
        if not isinstance(instance, str) or not house.SLUG.fullmatch(instance):
            raise ValueError("instance: not an object ID")
        if not isinstance(update, bool):
            raise ValueError("update: true or false")
        checked = parse_config(config, empty=not apply)
    except ValueError as e:
        raise policy.OpError("bad_request", str(e)) from None
    if (_jobs.get(instance) or {}).get("running"):
        raise policy.OpError("busy", f"trv {instance}: a job already runs")
    job = {
        "instance": instance,
        "running": True,  # before any await: a second trv.sync is busy
        "started_at": time.time(),
        "finished_at": None,
        "mode": "sync" if apply else "check",
        "update": update,
        "config": checked,
        "error": "",
        "valves": [],
    }
    _jobs[instance] = job
    try:
        async with aiohttp.ClientSession() as http:
            states = await house._states(http)
        sensor = states.get(f"sensor.heating_gateways_{instance}")
        if sensor is None:
            raise policy.OpError("not_found", f"sensor.heating_gateways_{instance}")
    except BaseException as e:
        job.update(running=False, finished_at=time.time(), error=getattr(e, "code", "internal"))
        raise
    gateways = (sensor.get("attributes") or {}).get("gateways")
    gateways = gateways if isinstance(gateways, dict) else {}
    task = asyncio.create_task(_run(job, gateways, checked, update, apply))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"started": True, "gateways": len(gateways)}


def status(instance: object) -> dict | None:
    """trv.status: a copy of the circuit's last job (running or done), None without one."""
    if not isinstance(instance, str):
        raise ValueError("instance")
    return copy.deepcopy(_jobs.get(instance))
