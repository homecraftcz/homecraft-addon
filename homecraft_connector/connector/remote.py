"""Vzdálený přístup in the panel (#50): what the house has of Tailscale, read
without the Tailscale API and without its keys. The tailnet and subnet come
from Homecraft's features.report (homecraftcz/homecraft#945); the add-on's
state, version and options from the Supervisor; the certificate's expiry from
a TLS handshake with the house address."""

import asyncio
import io
import logging
import re
import ssl
import time

import aiohttp
import segno

from . import features, settings

logger = logging.getLogger(__name__)

ADDON = "a0d7b954_tailscale"
DEFAULT_HOSTNAME = "homeassistant"
CERT_TIMEOUT = 3.0
CERT_TTL = 3600.0
SUPERVISOR_TIMEOUT = aiohttp.ClientTimeout(total=5)
# The address goes into a QR code and the owner's phone: DNS names only.
LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
HOSTNAME = re.compile(LABEL)
TAILNET = re.compile(rf"{LABEL}(?:\.{LABEL})+")

_certs: dict[str, tuple[float, float | None]] = {}  # host -> (checked at, expiry)


def _feature(report: dict, fid: str) -> dict | None:
    """A feature by its `id`; an older report without ids by its name."""
    for f in report["features"]:
        if f.get("id") == fid or (not f.get("id") and fid in f.get("name", "").lower()):
            return f
    return None


async def _supervisor(http: aiohttp.ClientSession, path: str) -> dict | None:
    """The Supervisor's `data` for GET `path`; None when it is missing, refuses
    (an add-on that is not installed) or does not answer."""
    if not settings.SUPERVISOR_URL:  # dev without a Supervisor
        return None
    try:
        async with http.get(
            f"{settings.SUPERVISOR_URL}{path}",
            headers={"Authorization": f"Bearer {settings.SUPERVISOR_TOKEN}"},
            timeout=SUPERVISOR_TIMEOUT,
        ) as r:
            if r.status != 200:
                return None
            data = (await r.json()).get("data")
    except (aiohttp.ClientError, TimeoutError, ValueError, AttributeError) as e:
        logger.debug("Supervisor %s neodpověděl: %s", path, type(e).__name__)
        return None
    return data if isinstance(data, dict) else None


async def cert_expiry(host: str) -> float | None:
    """When the house address's HTTPS certificate expires (unix time); None when
    the handshake fails or takes over CERT_TIMEOUT. Cached CERT_TTL, failures too."""
    cached = _certs.get(host)
    if cached and time.monotonic() - cached[0] < CERT_TTL:
        return cached[1]
    expiry = None
    writer = None
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, 443, ssl=ssl.create_default_context()), CERT_TIMEOUT
        )
        expiry = float(ssl.cert_time_to_seconds(writer.get_extra_info("peercert")["notAfter"]))
    except Exception as e:  # DNS, routing, an untrusted certificate: the row hides
        logger.debug("Certifikát %s nezjištěn: %s", host, type(e).__name__)
    finally:
        if writer is not None:
            writer.close()
    _certs[host] = (time.monotonic(), expiry)
    return expiry


async def status() -> dict:
    """The tab's data. `enabled` = the project has the tailscale feature and the
    Tailscale add-on is installed; `url` is "" until the tailnet is known."""
    report = features.load()
    feature = _feature(report, "tailscale")
    ts = feature.get("settings") if feature else None
    ts = ts if isinstance(ts, dict) else {}
    host = None
    async with aiohttp.ClientSession() as http:
        addon = await _supervisor(http, f"/addons/{ADDON}/info") if feature else None
        options = addon.get("options") if addon else None
        options = options if isinstance(options, dict) else {}
        if addon:  # the add-on names the device after the host unless an option says otherwise
            host = options.get("hostname") or (await _supervisor(http, "/host/info") or {}).get(
                "hostname"
            )
    host = host.lower() if isinstance(host, str) else ""
    hostname = host if HOSTNAME.fullmatch(host) else DEFAULT_HOSTNAME
    tailnet = str(ts.get("tailnet") or "").strip().lower().rstrip(".")
    tailnet = tailnet if TAILNET.fullmatch(tailnet) else ""
    fqdn = f"{hostname}.{tailnet}" if tailnet else ""
    enabled = addon is not None
    subnet = ts.get("subnet")
    return {
        "enabled": enabled,
        "requested_at": report["remote_access_requested_at"],
        "url": f"https://{fqdn}" if enabled and fqdn else "",
        "tailnet": tailnet,
        "hostname": hostname,
        "addon": {
            "state": str(addon.get("state") or ""),
            "version": str(addon.get("version") or ""),
        }
        if enabled
        else None,
        "cert_expires": await cert_expiry(fqdn) if enabled and fqdn else None,
        "subnet": subnet.strip() if isinstance(subnet, str) else None,
        # go2rtc's WebRTC needs the add-on's userspace networking off
        "cameras": (options.get("userspace_networking") is False)
        if enabled and _feature(report, "go2rtc")
        else None,
    }


def qr_svg(url: str) -> str:
    """The URL as a QR code, dark on white whatever the panel's theme."""
    out = io.BytesIO()
    segno.make(url, error="m").save(
        out, kind="svg", scale=1, border=4, dark="#000", light="#fff", omitsize=True, xmldecl=False
    )
    return out.getvalue().decode()
