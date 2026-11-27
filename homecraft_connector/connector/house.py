"""Live house state for the panel's Domů tab (#47): heating from HA's states,
read only. The circuits (valves, windows) come from Homecraft's features.report
(homecraftcz/homecraft#945); everything live is read from HA as the add-on:
`GET /api/states` once per request, the entity registry (for the battery
entity on a valve's or window sensor's device) over WS, cached ENTITY_TTL s."""

import logging
import math
import time

import aiohttp

from . import network_discovery, policy, selfcheck, settings

logger = logging.getLogger(__name__)

ENTITY_TTL = 60.0
LOW_BATTERY_HELPER = "input_number.heating_low_battery_threshold"
LOW_BATTERY_DEFAULT = 30.0  # heating.jinja's default
# heating.jinja heating_outdoor_temperature(): this helper names the source entity
OUTDOOR_HELPER = "input_text.heating_outdoor_temperature_source"
OUTDOOR_DEFAULT = "weather.forecast_domov"

_registry: dict = {"at": None, "devices": {}}  # entity_id -> device_id


def _num(value) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def _items(value) -> list[dict]:
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def _ids(value) -> list[str]:
    return [x for x in value if isinstance(x, str)] if isinstance(value, list) else []


async def _states(http: aiohttp.ClientSession) -> dict[str, dict]:
    try:
        async with http.get(
            f"{settings.CORE_HTTP_URL}/api/states",
            headers={"Authorization": f"Bearer {settings.SUPERVISOR_TOKEN}"},
            timeout=selfcheck.TIMEOUT,
        ) as r:
            if r.status != 200:
                raise policy.OpError("ha_failed", f"/api/states: HTTP {r.status}")
            states = await r.json()
    except (aiohttp.ClientError, TimeoutError, ValueError) as e:
        raise policy.OpError("ha_unreachable", type(e).__name__) from None
    return {s["entity_id"]: s for s in _items(states) if isinstance(s.get("entity_id"), str)}


async def _entity_devices(http: aiohttp.ClientSession) -> dict[str, str]:
    """entity_id -> device_id from the entity registry, cached ENTITY_TTL s."""
    at = _registry["at"]
    if at is not None and time.monotonic() - at < ENTITY_TTL:
        return _registry["devices"]
    [slot] = await network_discovery.collect(
        http, ("config/entity_registry/list",), quiet=0.05, max_wait=10
    )
    if slot["error"]:
        raise policy.OpError("ha_failed", slot["error"])
    devices = {
        e["entity_id"]: e["device_id"]
        for e in _items(slot["result"])
        if isinstance(e.get("entity_id"), str) and isinstance(e.get("device_id"), str)
    }
    _registry.update(at=time.monotonic(), devices=devices)
    return devices


def _batteries(states: dict[str, dict], devices: dict[str, str]) -> dict[str, float]:
    """device_id -> battery % of its battery sensor."""
    out = {}
    for eid, s in states.items():
        attrs = s.get("attributes") if isinstance(s.get("attributes"), dict) else {}
        level = _num(s.get("state"))
        if attrs.get("device_class") == "battery" and level is not None and eid in devices:
            device = devices[eid]
            out[device] = min(level, out.get(device, level))  # several sensors: the lowest
    return out


def _name(*values) -> str:
    """The first non-empty text; a non-text (bad report) never shows."""
    return next((v for v in values if isinstance(v, str) and v), "")


def _outdoor(states: dict[str, dict]) -> float | None:
    source = str((states.get(OUTDOOR_HELPER) or {}).get("state") or "").strip()
    s = states.get(source if "." in source else OUTDOOR_DEFAULT) or {}
    if source.startswith("weather.") or "." not in source:
        attrs = s.get("attributes") if isinstance(s.get("attributes"), dict) else {}
        return _num(attrs.get("temperature"))
    return _num(s.get("state"))


def summarize(circuits: list, states: dict[str, dict], devices: dict[str, str]) -> dict:
    """The heating summary and the low batteries (valves and window sensors
    under the heating's threshold, lowest first)."""
    batteries = _batteries(states, devices)
    threshold = _num((states.get(LOW_BATTERY_HELPER) or {}).get("state"))
    threshold = LOW_BATTERY_DEFAULT if threshold is None else threshold
    low: dict[str, dict] = {}

    def battery(eid: str, name: str, kind: str, circuit: str) -> float | None:
        level = batteries.get(devices.get(eid, ""))
        if level is not None and level < threshold:
            low[eid] = dict(entity_id=eid, name=name, kind=kind, circuit=circuit, battery=level)
        return level

    out = []
    for c in _items(circuits):
        instance = c.get("instance")
        if not isinstance(instance, str) or not instance:
            continue
        name = _name(c.get("heading"), c.get("title"), instance)
        valves = []
        for v in _items(c.get("valves")):
            eid = v.get("entity_id")
            if not isinstance(eid, str):
                continue
            s = states.get(eid) or {}
            attrs = s.get("attributes") if isinstance(s.get("attributes"), dict) else {}
            vname = _name(v.get("name"), attrs.get("friendly_name"), eid)
            valves.append(
                {
                    "entity_id": eid,
                    "name": vname,
                    "state": s.get("state"),
                    "temperature": _num(attrs.get("current_temperature")),
                    "target": _num(attrs.get("temperature")),
                    "hvac_action": attrs.get("hvac_action"),
                    "battery": battery(eid, vname, "valve", name),
                }
            )
        windows = []
        for w in _items(c.get("windows")):
            sensors = _ids(w.get("sensors"))
            wname = _name(w.get("name"), w.get("key"))
            for eid in sensors:
                battery(eid, wname, "window", name)
            windows.append(
                {
                    "name": wname,
                    "open": any((states.get(e) or {}).get("state") == "on" for e in sensors),
                }
            )
        demand = (states.get(f"binary_sensor.heating_state_{instance}") or {}).get("state")
        heating = any(v["hvac_action"] == "heating" for v in valves)
        out.append(
            {
                "name": name,
                "heating": demand == "on" if demand in ("on", "off") else heating,
                "valves": valves,
                "windows": windows,
            }
        )
    every = [v for c in out for v in c["valves"]]
    return {
        "heating": {
            "circuits": out,
            "circuits_heating": sum(c["heating"] for c in out),
            "valves": len(every),
            "valves_heating": sum(v["hvac_action"] == "heating" for v in every),
            "windows": sum(len(c["windows"]) for c in out),
            "windows_open": sum(w["open"] for c in out for w in c["windows"]),
            "outdoor": _outdoor(states),
            "low_battery_threshold": threshold,
        },
        "low_batteries": sorted(low.values(), key=lambda b: b["battery"]),
    }


async def heating_summary(circuits: list) -> dict:
    """summarize() over HA's live states; policy.OpError when HA does not answer.
    Without the entity registry only the batteries are missing."""
    async with aiohttp.ClientSession() as http:
        states = await _states(http)
        try:
            devices = await _entity_devices(http)
        except policy.OpError as e:
            logger.warning("Registr entit HA nenačten, baterie chybí: %s %s", e.code, e.message)
            devices = {}
    return summarize(circuits, states, devices)
