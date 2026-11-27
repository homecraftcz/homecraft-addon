"""Vzdálený přístup in the panel (#50): what the house has of Tailscale, read
without the Tailscale API and without its keys. The tailnet and subnet come
from Homecraft's features.report (homecraftcz/homecraft#945); the add-on's
state, version and options from the Supervisor; the certificate's expiry from
a TLS handshake with the house address, in the background."""

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
CERT_RETRY = 300.0  # after a failed or refused handshake: the technician may be fixing it
CERT_SOON = 14 * 86400  # a valid certificate closer to its end than this is a warning
SUPERVISOR_TIMEOUT = aiohttp.ClientTimeout(total=5)
# The address goes into a QR code and the owner's phone: Tailscale DNS names only.
LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
TAILNET = re.compile(rf"(?:{LABEL}\.)+ts\.net")

# host -> (checked at, result); host -> the probe in flight
_certs: dict[str, tuple[float, dict | None]] = {}
_cert_tasks: dict[str, asyncio.Task] = {}


def _feature(report: dict, fid: str) -> dict | None:
    """A feature by its `id`; an older report without ids by its name."""
    for f in report["features"]:
        if f.get("id") == fid or (not f.get("id") and fid in f.get("name", "").lower()):
            return f
    return None


def machine_name(name: object) -> str:
    """The device name Tailscale makes of a hostname (like its SanitizeHostname):
    the first label, lowercase, `_` and spaces as `-`, other characters dropped."""
    label = name.split(".")[0].strip().lower() if isinstance(name, str) else ""
    label = re.sub(r"[^a-z0-9-]", "", re.sub(r"[ _]", "-", label)).strip("-")
    return label[:63].strip("-") or DEFAULT_HOSTNAME


def _https_on(options: dict) -> bool:
    """Whether the add-on serves HA over HTTPS: `share_homeassistant` (add-on
    0.26+) or the older `proxy`. Neither in the options: on (Homecraft sets it)."""
    if "share_homeassistant" in options:
        return options["share_homeassistant"] in ("serve", "funnel")
    return options.get("proxy") is not False


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


async def cert_expiry(host: str) -> dict | None:
    """One verified TLS handshake with the house address: `{"valid": True,
    "expires": <unix time>}`; `{"valid": False}` when the certificate is refused
    (expired, untrusted, another name); None when no handshake happens within
    CERT_TIMEOUT (DNS, routing, timeout)."""
    writer = None
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, 443, ssl=ssl.create_default_context()), CERT_TIMEOUT
        )
        expires = ssl.cert_time_to_seconds(writer.get_extra_info("peercert")["notAfter"])
        return {"valid": True, "expires": float(expires)}
    except ssl.SSLCertVerificationError as e:
        logger.info("Certifikát %s neplatí: %s", host, e.verify_message)
        return {"valid": False}
    except Exception as e:
        logger.debug("Certifikát %s nezjištěn: %s", host, type(e).__name__)
        return None
    finally:
        if writer is not None:
            writer.close()


async def _probe(host: str) -> None:
    try:
        result = await cert_expiry(host)
    except Exception:  # a bug must not end in an unretrieved task exception
        logger.exception("Kontrola certifikátu %s selhala", host)
        result = None
    _certs[host] = (time.monotonic(), result)


def cert(host: str, force: bool = False) -> tuple[dict | None, bool]:
    """(the cached result at once, whether a probe runs). A stale result
    (CERT_TTL; after a failure or a refused certificate CERT_RETRY) or `force`
    starts a probe in the background; never two for one host at once."""
    at, result = _certs.get(host, (None, None))
    ttl = CERT_TTL if result and result["valid"] else CERT_RETRY
    task = _cert_tasks.get(host)
    if (force or at is None or time.monotonic() - at > ttl) and (task is None or task.done()):
        task = _cert_tasks[host] = asyncio.create_task(_probe(host))
    return result, task is not None and not task.done()


async def address() -> dict:
    """The house address and what it rests on. `enabled` = the project has the
    tailscale feature and the Tailscale add-on is installed; `fqdn` is "" until
    the tailnet is known and while the add-on does not serve HTTPS."""
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
    hostname = machine_name(host)
    tailnet = str(ts.get("tailnet") or "").strip().lower().rstrip(".")
    tailnet = tailnet if TAILNET.fullmatch(tailnet) else ""
    enabled = addon is not None
    https = enabled and _https_on(options)
    return {
        "report": report,
        "enabled": enabled,
        "https": https,
        "fqdn": f"{hostname}.{tailnet}" if tailnet and https else "",
        "tailnet": tailnet,
        "hostname": hostname,
        "addon": addon,
        "options": options,
        "settings": ts,
    }


async def url() -> str:
    """The house address alone (the QR code): no certificate probe."""
    fqdn = (await address())["fqdn"]
    return f"https://{fqdn}" if fqdn else ""


def _certificate(result: dict | None) -> dict | None:
    if result is None:
        return None
    if not result["valid"]:
        return {"state": "invalid", "expires": None}
    soon = result["expires"] - time.time() < CERT_SOON
    return {"state": "soon" if soon else "ok", "expires": result["expires"]}


async def status(force: bool = False) -> dict:
    """The tab's data. The certificate is the cached one; `cert_pending` says a
    probe runs (`force`: Zkontrolovat starts one whatever the cache)."""
    a = await address()
    report, addon, fqdn, enabled = a["report"], a["addon"], a["fqdn"], a["enabled"]
    result, pending = cert(fqdn, force) if fqdn else (None, False)
    subnet = a["settings"].get("subnet")
    return {
        "enabled": enabled,
        "requested_at": report["remote_access_requested_at"],
        "url": f"https://{fqdn}" if fqdn else "",
        "https": a["https"],
        "tailnet": a["tailnet"],
        "hostname": a["hostname"],
        "addon": {
            "state": str(addon.get("state") or ""),
            "version": str(addon.get("version") or ""),
        }
        if enabled
        else None,
        "cert": _certificate(result),
        "cert_pending": pending,
        "subnet": subnet.strip() if isinstance(subnet, str) else None,
        # go2rtc's WebRTC needs the add-on's userspace networking off
        "cameras": (a["options"].get("userspace_networking") is False)
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
