"""Live house state for the panel's Domů tab (#47) and Topení tab (#48):
heating from HA's states, read only, plus the five helpers of a circuit the
owner sets (set_helper, #48). The circuits (valves, windows) come from Homecraft's features.report
(homecraftcz/homecraft#945); everything live is read from HA as the add-on:
`GET /api/states` once per request, the entity registry (for the battery
entity on a valve's or window sensor's device) over WS, cached ENTITY_TTL s."""

import logging
import math
import re
import time
from datetime import datetime

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
                "instance": instance,
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


async def _live() -> tuple[dict[str, dict], dict[str, str]]:
    """HA's states and the entity registry; policy.OpError when HA does not
    answer. Without the registry only the batteries are missing."""
    async with aiohttp.ClientSession() as http:
        states = await _states(http)
        try:
            devices = await _entity_devices(http)
        except policy.OpError as e:
            logger.warning("Registr entit HA nenačten, baterie chybí: %s %s", e.code, e.message)
            devices = {}
    return states, devices


async def heating_summary(circuits: list) -> dict:
    """summarize() over HA's live states."""
    return summarize(circuits, *await _live())


# The owner's helpers of a circuit (#48): key -> (entity prefix, service, data
# field, check). Ranges and options as the heating library defines the helpers.
TIME = re.compile(r"([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?")
SLUG = re.compile(r"[a-z0-9_]{1,100}")


def _in_range(low: float, high: float, step: float):
    def check(value: object) -> float:
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        if not ok or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"a number {low:g}–{high:g}")
        if abs((value - low) / step - round((value - low) / step)) > 1e-9:
            raise ValueError(f"a step of {step:g}")
        return float(value)

    return check


def _rule(value: object) -> str:
    if value not in ("weather", "time"):
        raise ValueError("weather or time")
    return value


def _time(value: object) -> str:
    if not isinstance(value, str) or not TIME.fullmatch(value):
        raise ValueError("a time HH:MM")
    return value[:5] + ":00"


HELPERS = {
    "window_reminder": (
        "input_number.heating_window_reminder_",
        "input_number/set_value",
        "value",
        _in_range(0, 120, 5),
    ),
    "weather_threshold": (
        "input_number.heating_weather_threshold_",
        "input_number/set_value",
        "value",
        _in_range(5, 25, 0.5),
    ),
    "auto_rule": (
        "input_select.heating_auto_rule_",
        "input_select/select_option",
        "option",
        _rule,
    ),
    "time_from": (
        "input_datetime.heating_time_from_",
        "input_datetime/set_datetime",
        "time",
        _time,
    ),
    "time_to": ("input_datetime.heating_time_to_", "input_datetime/set_datetime", "time", _time),
}
PHASES = ("fast", "run", "overrun", "closed")  # heating.jinja heating_valves(); else normal


def helper_call(instance: str, key: str, value: object) -> tuple[str, dict]:
    """(service path, service data) for one helper; ValueError on a key outside
    HELPERS or a value outside its range."""
    if key not in HELPERS:
        raise ValueError(f"key: one of {', '.join(HELPERS)}")
    # An object ID only: HA reads "a, b" in entity_id as two entities.
    if not SLUG.fullmatch(instance):
        raise ValueError("circuit: not an object ID")
    prefix, service, field, check = HELPERS[key]
    return service, {"entity_id": prefix + instance, field: check(value)}


async def set_helper(instance: str, key: str, value: object) -> dict:
    """Calls the helper's service over HA's REST API as the add-on; returns the
    service data. ValueError on bad input, policy.OpError when HA refuses or does
    not answer. The caller checks that `instance` is a reported circuit."""
    service, data = helper_call(instance, key, value)
    try:
        async with (
            aiohttp.ClientSession() as http,
            http.post(
                f"{settings.CORE_HTTP_URL}/api/services/{service}",
                json=data,
                headers={"Authorization": f"Bearer {settings.SUPERVISOR_TOKEN}"},
                timeout=selfcheck.TIMEOUT,
            ) as r,
        ):
            if r.status != 200:
                raise policy.OpError("ha_failed", f"{service}: HTTP {r.status}")
    except (aiohttp.ClientError, TimeoutError) as e:
        raise policy.OpError("ha_unreachable", type(e).__name__) from None
    return data


def _since(state: dict) -> float | None:
    try:
        return datetime.fromisoformat(str(state.get("last_changed"))).timestamp()
    except ValueError:
        return None


def _helper_value(key: str, state: dict) -> object:
    value = state.get("state")
    if HELPERS[key][2] == "value":
        return _num(value)
    if key == "auto_rule":
        return value if value in ("weather", "time") else None
    return value[:5] if isinstance(value, str) and TIME.fullmatch(value) else None


def detail(circuit: dict, states: dict[str, dict], devices: dict[str, str]) -> dict:
    """One circuit for Topení: summarize()'s circuit plus each valve's room
    (<room> of climate.<room>_valve) and phase, each window's open-since and
    the valves it closes, the owner's helpers, the mode and the boiler."""
    instance = circuit["instance"]
    [c] = summarize([circuit], states, devices)["heating"]["circuits"]
    attrs = (states.get(f"sensor.heating_valves_{instance}") or {}).get("attributes")
    phases = attrs.get("phases") if isinstance(attrs, dict) else None
    phases = phases if isinstance(phases, dict) else {}
    names = {}
    for v in c["valves"]:
        eid = v["entity_id"]
        room = eid.split(".", 1)[-1]
        v["room"] = room.removesuffix("_valve") if room.endswith("_valve") else ""
        phase = phases.get(eid)
        phase = phase[0] if isinstance(phase, list) and phase else None
        v["phase"] = phase if phase in PHASES else "normal"
        names[eid] = v["name"]
    # summarize() keeps every window in order, so they pair with the report's.
    for w, reported in zip(c["windows"], _items(circuit.get("windows")), strict=True):
        sensors = _ids(reported.get("sensors"))
        opened = [_since(states[e]) for e in sensors if (states.get(e) or {}).get("state") == "on"]
        w["open_since"] = min((t for t in opened if t is not None), default=None)
        w["valves"] = [names.get(e, e) for e in _ids(reported.get("valves"))]
    attrs = (states.get(f"binary_sensor.heating_state_{instance}") or {}).get("attributes")
    url = attrs.get("url") if isinstance(attrs, dict) else None
    # A path in HA (the circuit's dashboard view), never another origin.
    url = url if isinstance(url, str) and url.startswith("/") and not url.startswith("//") else ""
    weather_ok = (states.get(f"binary_sensor.heating_weather_ok_{instance}") or {}).get("state")
    return {
        **c,
        "dashboard_url": url or "/lovelace",
        "helpers": {
            key: _helper_value(key, states.get(prefix + instance) or {})
            for key, (prefix, *_) in HELPERS.items()
        },
        "mode": (states.get(f"input_select.heating_mode_{instance}") or {}).get("state"),
        "weather_ok": {"on": True, "off": False}.get(weather_ok),
        "outdoor": _outdoor(states),
        "boiler": {
            "relay": (states.get(f"switch.boiler_{instance}") or {}).get("state"),
            "overrun": _num(
                (states.get(f"input_number.heating_bypass_overrun_{instance}") or {}).get("state")
            ),
        },
    }


async def heating_detail(circuit: dict) -> dict:
    """detail() over HA's live states; policy.OpError when HA does not answer."""
    return detail(circuit, *await _live())
