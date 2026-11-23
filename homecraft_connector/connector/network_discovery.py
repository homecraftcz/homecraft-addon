"""Network device candidates from HA (#632): the device registry plus HA's DHCP,
zeroconf and SSDP discovery merged into one list per address. merge_candidates
is Homecraft's domain/network_discovery.py (#464/#466) unchanged; the add-on
reads the WS payloads itself, over core as the add-on."""

import asyncio
import ipaddress
import re
from urllib.parse import urlparse

import aiohttp

from . import network_devices, policy, ws_proxy

_CAMERA_INTEGRATIONS = {
    "onvif",
    "reolink",
    "tapo_control",
    "amcrest",
    "generic",
    "hikvision",
    "dahua",
}


def _items(value) -> list[dict]:
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def _str(value) -> str | None:
    return value if isinstance(value, str) and value else None


def _mac(value) -> str | None:
    hexa = re.sub(r"[:\-]", "", value) if isinstance(value, str) else ""
    if not re.fullmatch(r"[0-9a-fA-F]{12}", hexa):
        return None
    return ":".join(hexa[i : i + 2] for i in range(0, 12, 2)).upper()


def _host(url) -> str | None:
    try:
        parsed = urlparse(url) if isinstance(url, str) else None
        # homeassistant://config/backup and the like are not network addresses
        return parsed.hostname if parsed and parsed.scheme in ("http", "https") else None
    except ValueError:
        return None


def _is_lan_host(host: str | None) -> bool:
    """An IP literal, a `.local` name or a dotless one -- not a vendor cloud."""
    return bool(host) and (_ipv4(host) is not None or host.endswith(".local") or "." not in host)


def _ipv4(value) -> str | None:
    try:
        return str(ipaddress.IPv4Address(value))
    except ValueError:
        return None


def merge_candidates(
    devices: list, config_entries: list, dhcp: list, zeroconf: list, ssdp: list
) -> list[dict]:
    domains = {e["entry_id"]: e.get("domain") for e in _items(config_entries) if "entry_id" in e}
    cands: dict[str, dict] = {}
    by_mac: dict[str, str] = {}  # mac -> address of its candidate
    names: dict[str, dict[str, str]] = {"zeroconf": {}, "ssdp": {}}
    ztype: dict[str, str] = {}

    def cand(address: str, source: str, **fields) -> dict:
        c = cands.setdefault(
            address,
            {
                "address": address,
                "mac": None,
                "name": None,
                "hostname": None,
                "kind": "other",
                "urls": [],
                "manufacturer": None,
                "model": None,
                "integration": None,
                "sources": set(),
            },
        )
        c["sources"].add(source)
        for k, v in fields.items():
            if v and not c[k]:
                c[k] = v
        if c["mac"]:
            by_mac[c["mac"]] = address
        return c

    registry = []
    for d in _items(devices):
        url = _str(d.get("configuration_url"))
        mac = next(
            (
                _mac(c[1])
                for c in d.get("connections") or []
                if isinstance(c, list) and len(c) == 2 and c[0] == "mac" and _mac(c[1])
            ),
            None,
        )
        entries = [e for e in d.get("config_entries") or [] if isinstance(e, str)]
        entry = _str(d.get("primary_config_entry")) or (entries[0] if entries else None)
        registry.append(
            (
                _host(url),
                url,
                {
                    "mac": mac,
                    "name": _str(d.get("name_by_user")) or _str(d.get("name")),
                    "manufacturer": _str(d.get("manufacturer")),
                    "model": _str(d.get("model")),
                    "integration": domains.get(entry),
                },
            )
        )

    def add_registry(address: str, url: str | None, fields: dict) -> None:
        c = cand(address, "registry", **fields)
        # the DTO accepts only network schemes; homeassistant://… would fail the import
        if url and _host(url) is not None:  # http(s) only: _host drops the rest
            c["urls"].append(url)

    for d in _items(dhcp):
        if address := _str(d.get("ip_address")):
            cand(address, "dhcp", mac=_mac(d.get("mac_address")), hostname=_str(d.get("hostname")))
    for address, url, fields in registry:
        # The DHCP lease of the device's MAC wins; else its LAN-looking URL host.
        address = by_mac.get(fields["mac"]) or (address if _is_lan_host(address) else None)
        if address:
            add_registry(address, url, fields)
    for d in _items(zeroconf):
        address = next((ip for a in d.get("ip_addresses") or [] if (ip := _ipv4(a))), None)
        if address:
            cand(address, "zeroconf")
            if name := _str(d.get("name")):
                names["zeroconf"].setdefault(address, name.split(".")[0])
            if t := _str(d.get("type")):
                ztype.setdefault(address, t)
    for d in _items(ssdp):
        if address := _host(d.get("ssdp_location")):
            cand(address, "ssdp")
            if name := _str(d.get("name")):
                names["ssdp"].setdefault(address, name)

    out = []
    for address, c in cands.items():
        c["name"] = (
            c["name"]
            or names["zeroconf"].get(address)
            or names["ssdp"].get(address)
            or c["hostname"]
            or address
        )
        c["kind"] = _kind(c, ztype.get(address))
        c["urls"] = list(dict.fromkeys(c["urls"]))
        c["sources"] = sorted(c["sources"])
        out.append(c)
    return sorted(out, key=_sort_key)


def _kind(c: dict, zeroconf_type: str | None) -> str:
    text = " ".join(
        x.lower() for x in (c["name"], c["hostname"], zeroconf_type, c["integration"]) if x
    )
    if "blugw" in text:
        return "shelly_gateway"
    if "shelly" in text:
        return "shelly"
    if c["integration"] in _CAMERA_INTEGRATIONS or (zeroconf_type or "").startswith("_rtsp."):
        return "camera"
    if c["integration"] == "synology_dsm" or "synology" in text or "diskstation" in text:
        return "nas"
    return "other"


def _sort_key(c: dict):
    ip = _ipv4(c["address"])
    return (0, int(ipaddress.IPv4Address(ip)), "") if ip else (1, 0, c["name"])


COMMANDS = (
    "config/device_registry/list",
    "config_entries/get",
    "dhcp/subscribe_discovery",
    "zeroconf/subscribe_discovery",
    "ssdp/subscribe_discovery",
)


async def collect(
    http: aiohttp.ClientSession, commands=COMMANDS, quiet: float = 1.0, max_wait: float = 5.0
) -> list[dict]:
    """Per command `{"result", "events", "error"}`: read until every command
    has its result and nothing came for `quiet` s, or `max_wait` s in total
    (Homecraft's ha_registry_ws_client.collect). Closing the socket ends the
    subscriptions."""
    ws = await ws_proxy.connect(http)
    try:
        for i, cmd in enumerate(commands, 1):
            await ws.send_json({"id": i, "type": cmd})
        out = [{"result": None, "events": [], "error": None} for _ in commands]
        answered: set[int] = set()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_wait
        while (remaining := deadline - loop.time()) > 0:
            wait = min(quiet, remaining) if len(answered) == len(commands) else remaining
            try:
                reply = await ws.receive_json(timeout=wait)
            except TimeoutError:
                break
            i = reply.get("id") if isinstance(reply, dict) else None
            if not isinstance(i, int) or not 1 <= i <= len(commands):
                continue
            slot = out[i - 1]
            if reply.get("type") == "event":
                slot["events"].append(reply.get("event"))
            elif not reply.get("success"):
                error = reply.get("error") if isinstance(reply.get("error"), dict) else {}
                slot["error"] = f"{commands[i - 1]} failed: {error.get('code', 'unknown_error')}"
                answered.add(i)
            else:
                slot["result"] = reply.get("result")
                answered.add(i)
        for i, cmd in enumerate(commands, 1):
            if i not in answered:
                out[i - 1]["error"] = f"{cmd} timed out"
        return out
    except (aiohttp.ClientError, TypeError, ValueError) as e:  # TypeError: socket closed
        raise policy.OpError("ha_unreachable", type(e).__name__) from None
    finally:
        await ws.close()


def _valid(check, value) -> str | None:
    try:
        return check(value)
    except ValueError:
        return None


def from_ha(r: list[dict], saved: list[dict]) -> list[dict]:
    """Candidates from collect()'s output, each a valid stored device (source
    `ha`) plus `manufacturer`, `model`, `integration`, `sources` and `saved`
    (a saved device has its address or MAC). The registry reads are required;
    a discovery command this HA lacks counts as empty."""
    for slot in r[:2]:
        if slot["error"]:
            raise policy.OpError("ha_failed", slot["error"])

    def added(i: int) -> list:
        return [a for e in r[i]["events"] if isinstance(e, dict) for a in e.get("add") or []]

    addresses = {d["address"].lower() for d in saved}
    macs = {d["mac"] for d in saved if d["mac"]}
    out = []
    for c in merge_candidates(r[0]["result"], r[1]["result"], added(2), added(3), added(4)):
        hostname = _valid(
            network_devices._hostname, {"hostname": (c["hostname"] or "").rstrip(".")}
        )
        note = " ".join(x for x in (c["manufacturer"], c["model"]) if x)
        try:
            [device] = network_devices.validate(
                [
                    {
                        "name": c["name"][:100],
                        "kind": c["kind"],
                        "address": c["address"],
                        "hostname": hostname or "",  # a DHCP hostname has no .local: dropped
                        "urls": [u for u in c["urls"] if _valid(network_devices._url, u)],
                        "mac": c["mac"],
                        "note": note[:1000],
                        "source": "ha",
                    }
                ]
            )
        except ValueError:
            continue  # an address the list would not take
        saved_ = device["address"].lower() in addresses or device["mac"] in macs
        out.append(
            {
                **device,
                **{k: c[k] for k in ("manufacturer", "model", "integration", "sources")},
                "saved": saved_,
            }
        )
    return out
