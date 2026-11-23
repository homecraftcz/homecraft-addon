"""Recommended LAN scan (#633), started only by a person from the panel.

The add-on stays on the hassio network (no host_network): no mDNS or ARP, but
unicast TCP to the LAN works. The subnet comes from the Supervisor's
/network/info; only private IPv4 subnets of at most /22 are scanned. At most
MAX_CONNECTIONS connections at once, each bounded by TIMEOUT. Nothing leaves
the LAN and no credentials are sent: only anonymous reads (`/shelly`, RTSP
OPTIONS, ONVIF GetDeviceInformation, Synology's API info)."""

import asyncio
import ipaddress
import json
import re
import time

import aiohttp

from . import network_devices, policy, settings

MAX_CONNECTIONS = 64
TIMEOUT = 0.5  # s per connection, connect and reply together
MIN_PREFIX = 22  # at most 1022 addresses per subnet
PROFILES = {
    "shelly": (80,),
    "camera": (554, 80, 8000),
    "nas": (5000, 5001, 445),
    "web": (80, 443),
}
open_connection = asyncio.open_connection  # tests swap in fake hosts


class ScanError(Exception):
    """A refusal for the panel, in Czech."""


def subnets(info: dict) -> list[ipaddress.IPv4Network]:
    """The host's IPv4 subnets from /network/info `data`; ScanError when none
    is private and small enough (the first refusal says why)."""
    nets, refusals = [], []
    for net in (a.network for a in _addresses(info)):
        if not any(net.subnet_of(p) for p in policy.TAILSCALE_ROUTE_NETS):
            refusals.append(f"Podsíť {net} není soukromá, sken odmítnut.")
        elif net.prefixlen < MIN_PREFIX:
            refusals.append(f"Podsíť {net} je větší než /{MIN_PREFIX}, sken odmítnut.")
        elif net not in nets:
            nets.append(net)
    if not nets:
        raise ScanError(refusals[0] if refusals else "Home Assistant nemá žádnou IPv4 síť.")
    return nets


def _addresses(info: dict) -> list[ipaddress.IPv4Interface]:
    out = []
    for iface in info.get("interfaces") or []:
        for addr in ((iface or {}).get("ipv4") or {}).get("address") or []:
            try:
                out.append(ipaddress.IPv4Interface(addr))
            except ValueError:
                pass
    return out


async def network_info(http: aiohttp.ClientSession) -> dict:
    if not settings.SUPERVISOR_URL:
        raise ScanError("Bez Supervisoru nelze zjistit podsíť.")
    try:
        async with http.get(
            f"{settings.SUPERVISOR_URL}/network/info",
            headers={"Authorization": f"Bearer {settings.SUPERVISOR_TOKEN}"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                raise ScanError(f"Supervisor /network/info: HTTP {resp.status}")
            return (await resp.json()).get("data") or {}
    except (aiohttp.ClientError, TimeoutError, ValueError) as e:
        raise ScanError(f"Supervisor /network/info: {type(e).__name__}") from None


def parse_shelly(data: object) -> dict | None:
    """`/shelly` of Gen1 (`type`, `mac`, `fw`) or Gen2+ (`id`, `model`, `gen`,
    `mac`, `ver`). Gen2+ announce themselves over mDNS as `<id>.local`; Gen1
    has no id here, so no hostname."""
    if not isinstance(data, dict) or not isinstance(data.get("mac"), str):
        return None
    gen = data.get("gen")
    if isinstance(gen, int) and isinstance(data.get("id"), str):
        model, fw, app = data.get("model"), data.get("ver"), data.get("app")
        name = data.get("name") or f"Shelly {app or model}"
        hostname = f"{data['id'].lower()}.local"
        try:
            network_devices._hostname({"hostname": hostname})
        except ValueError:
            hostname = ""
    elif isinstance(data.get("type"), str):
        gen, model, fw, hostname = 1, data["type"], data.get("fw"), ""
        name = f"Shelly {model}"
    else:
        return None
    blugw = "blugw" in (hostname + str(model) + str(data.get("app"))).lower()
    return {
        "name": str(name),
        "kind": "shelly_gateway" if blugw else "shelly",
        "hostname": hostname,
        "mac": data["mac"],
        "note": " · ".join(x for x in (f"Gen{gen}", fw and f"FW {fw}", model) if x),
    }


def _xml_field(text: str, tag: str) -> str | None:
    m = re.search(rf"<(?:\w+:)?{tag}>\s*([^<]{{1,100}}?)\s*</", text)
    return m.group(1) if m else None


ONVIF_BODY = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
    '<s:Body><GetDeviceInformation xmlns="http://www.onvif.org/ver10/device/wsdl"/>'
    "</s:Body></s:Envelope>"
)


class Prober:
    def __init__(self, http: aiohttp.ClientSession):
        self.http = http
        self.sem = asyncio.Semaphore(MAX_CONNECTIONS)

    async def is_open(self, ip: str, port: int) -> bool:
        async with self.sem:
            try:
                async with asyncio.timeout(TIMEOUT):
                    _, writer = await open_connection(ip, port)
            except (OSError, TimeoutError):
                return False
            writer.close()
            return True

    async def _http(self, method: str, url: str, **kw) -> tuple[int, str] | None:
        async with self.sem:
            try:
                async with self.http.request(
                    method,
                    url,
                    ssl=False,  # LAN devices have self-signed certificates
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=TIMEOUT),
                    **kw,
                ) as resp:
                    body = b""  # at most 64 KiB; read(n) may return less than n
                    while len(body) < 65536 and (chunk := await resp.content.read(65536)):
                        body += chunk
                    return resp.status, body.decode(errors="replace")
            except (aiohttp.ClientError, TimeoutError, ValueError):
                return None

    async def shelly(self, ip: str, port: int = 80) -> dict | None:
        r = await self._http("GET", f"http://{ip}:{port}/shelly")
        try:
            return parse_shelly(json.loads(r[1])) if r and r[0] == 200 else None
        except ValueError:
            return None

    async def onvif(self, ip: str, port: int) -> dict | None:
        r = await self._http(
            "POST",
            f"http://{ip}:{port}/onvif/device_service",
            data=ONVIF_BODY,
            headers={"Content-Type": "application/soap+xml; charset=utf-8"},
        )
        if not r or r[0] != 200 or "GetDeviceInformationResponse" not in r[1]:
            return None
        return {k: _xml_field(r[1], k) for k in ("Manufacturer", "Model", "FirmwareVersion")}

    async def rtsp(self, ip: str, port: int = 554) -> bool:
        async with self.sem:
            try:
                async with asyncio.timeout(TIMEOUT):
                    reader, writer = await open_connection(ip, port)
                    try:
                        writer.write(
                            f"OPTIONS rtsp://{ip}:{port}/ RTSP/1.0\r\nCSeq: 1\r\n\r\n".encode()
                        )
                        return (await reader.readline()).startswith(b"RTSP/1.0")
                    finally:
                        writer.close()
            except (OSError, TimeoutError):
                return False

    async def synology(self, ip: str, port: int) -> bool:
        scheme = "https" if port == 5001 else "http"
        r = await self._http(
            "GET",
            f"{scheme}://{ip}:{port}/webapi/query.cgi",
            params={"api": "SYNO.API.Info", "version": "1", "method": "query", "query": "all"},
        )
        return bool(r and r[0] == 200 and '"success":true' in r[1].replace(" ", ""))


async def probe_host(p: Prober, ip: str, profiles: set[str]) -> dict | None:
    """One device from the open ports, or None. Shelly wins, then a camera,
    a NAS, plain web."""
    ports = sorted({port for name in profiles for port in PROFILES[name]})
    up = [
        port
        for port, ok in zip(
            ports, await asyncio.gather(*(p.is_open(ip, x) for x in ports)), strict=True
        )
        if ok
    ]
    if not up:
        return None
    if "shelly" in profiles and 80 in up and (found := await p.shelly(ip)):
        return {**found, "urls": [f"http://{ip}"]}
    if "camera" in profiles:
        for port in (x for x in (80, 8000) if x in up):
            if info := await p.onvif(ip, port):
                vendor = info["Manufacturer"]
                return {
                    "name": f"Kamera {vendor}" if vendor else "Kamera, výrobce neznámý",
                    "kind": "camera",
                    "note": " · ".join(x for x in ("ONVIF", info["Model"]) if x),
                }
        if 554 in up and await p.rtsp(ip):
            return {"name": "Kamera, výrobce neznámý", "kind": "camera", "note": "RTSP 554"}
    if "nas" in profiles:
        for port in (x for x in (5000, 5001) if x in up):
            if await p.synology(ip, port):
                scheme = "https" if port == 5001 else "http"
                return {
                    "name": "Synology NAS",
                    "kind": "nas",
                    "note": f"DSM {port}",
                    "urls": [f"{scheme}://{ip}:{port}"],
                }
        if 445 in up:
            return {"name": "Sdílení SMB", "kind": "nas", "note": "SMB 445"}
    if "web" in profiles and (web := [x for x in (80, 443) if x in up]):
        scheme = "https" if web[0] == 443 else "http"
        return {
            "name": f"Web {ip}",
            "kind": "other",
            "note": f"HTTP {web[0]}",
            "urls": [f"{scheme}://{ip}"],
        }
    return None


def mark(found: list[dict], saved: list[dict]) -> tuple[list[dict], list[dict]]:
    """Valid scan devices (source `scan`) with `saved` (same IP, MAC or .local
    name as a saved device) and `filled` (that saved device had no hostname and
    gets this one), and the saved list with those hostnames filled in."""
    saved = [dict(d) for d in saved]
    out = []
    for f in found:
        try:
            [device] = network_devices.validate([{**f, "source": "scan"}])
        except ValueError:
            continue
        keys = {device["address"].lower(), device["hostname"].lower()} - {""}
        match = next(
            (
                d
                for d in saved
                if keys & {d["address"].lower(), d["hostname"].lower()}
                or (device["mac"] and device["mac"] == d["mac"])
            ),
            None,
        )
        filled = bool(match and device["hostname"] and not match["hostname"])
        if filled:
            match["hostname"] = device["hostname"]
        out.append({**device, "saved": match is not None, "filled": filled})
    return out, saved


async def scan(http: aiohttp.ClientSession, profiles: set[str]) -> dict:
    info = await network_info(http)
    nets = subnets(info)
    own = {str(a.ip) for a in _addresses(info)}
    hosts = [str(ip) for net in nets for ip in net.hosts() if str(ip) not in own]
    start = time.monotonic()
    p = Prober(http)
    found = await asyncio.gather(*(probe_host(p, ip, profiles) for ip in hosts))
    return {
        "subnets": [str(n) for n in nets],
        "addresses": len(hosts),
        "seconds": round(time.monotonic() - start, 1),
        "found": [{"address": ip, **f} for ip, f in zip(hosts, found, strict=True) if f],
    }
