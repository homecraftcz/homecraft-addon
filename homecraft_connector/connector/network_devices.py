"""LAN devices of this HA, kept by the add-on (#631): /data/network_devices.json.

The same model and checks as Homecraft's project network devices (#463/#467):
addresses only, never credentials. Added here: `hostname` (an mDNS `*.local`
name) and `source` (who added it: manual, ha, scan)."""

import json
import re
from urllib.parse import urlsplit

from . import settings
from .files import write_private

KINDS = ("shelly_gateway", "shelly", "camera", "nas", "router", "other")
SOURCES = ("manual", "ha", "scan")
MAX_DEVICES = 200
MAX_URLS = 20
_MAX_RAW_URLS = 100  # raw input cap, before empty entries are dropped
# The panel renders device URLs as links, so only these schemes (no javascript:, data:, ...).
_URL_SCHEMES = {"http", "https", "rtsp", "rtsps"}
# Six hex pairs with one consistent separator: ':', '-' or none.
_MAC_RE = re.compile(r"[0-9A-Fa-f]{2}([:-]?)[0-9A-Fa-f]{2}(?:\1[0-9A-Fa-f]{2}){4}")
_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def _path():
    return settings.DATA_DIR / "network_devices.json"


def _text(device: dict, key: str, max_len: int, *, required=False, default="") -> str:
    value = device.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"{key}: musí být text")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{key}: chybí")
    if len(value) > max_len:
        raise ValueError(f"{key}: nejvýš {max_len} znaků")
    return value


def _choice(device: dict, key: str, allowed: tuple, default: str) -> str:
    value = device.get(key, default)
    if value not in allowed:
        raise ValueError(f"{key}: jedna z hodnot {', '.join(allowed)}")
    return value


def _url(url: object) -> str:
    if not isinstance(url, str):
        raise ValueError("urls: musí být text")
    if len(url) > 500:
        raise ValueError("urls: nejvýš 500 znaků")
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises on a bad port
    except ValueError:
        raise ValueError("neplatná URL") from None
    # Errors never echo the URL: it may hold a password.
    if "@" in parts.netloc:
        raise ValueError("URL nesmí obsahovat přihlašovací údaje (jméno:heslo@)")
    if parts.scheme.lower() not in _URL_SCHEMES or not parts.hostname:
        schemes = ", ".join(sorted(_URL_SCHEMES))
        raise ValueError(f"URL potřebuje adresu a jedno ze schémat {schemes}")
    return url


def _hostname(device: dict) -> str:
    value = _text(device, "hostname", 255)
    labels = value.split(".")
    if value and (
        labels[-1].lower() != "local"
        or len(labels) < 2
        or not all(_LABEL_RE.fullmatch(label) for label in labels)
    ):
        raise ValueError(f"hostname: jméno v síti tvaru <jméno>.local, ne {value!r}")
    return value


def _mac(device: dict) -> str | None:
    value = device.get("mac")
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("mac: musí být text")
    value = value.strip()
    if not value:
        return None
    if not _MAC_RE.fullmatch(value):
        raise ValueError(f"neplatná MAC adresa {value!r}")
    digits = re.sub(r"[:-]", "", value).upper()
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


def _device(device: object) -> dict:
    if not isinstance(device, dict):
        raise ValueError("zařízení musí být objekt")
    address = _text(device, "address", 255, required=True)
    if any(c.isspace() or not c.isprintable() for c in address):
        raise ValueError("address: bez mezer a řídicích znaků")
    urls = device.get("urls", [])
    if not isinstance(urls, list) or len(urls) > _MAX_RAW_URLS:
        raise ValueError(f"urls: seznam, nejvýš {MAX_URLS} adres")
    urls = [u.strip() if isinstance(u, str) else u for u in urls]
    urls = [_url(u) for u in urls if u != ""]
    if len(urls) > MAX_URLS:
        raise ValueError(f"urls: nejvýš {MAX_URLS} adres")
    return {
        "name": _text(device, "name", 100, required=True),
        "kind": _choice(device, "kind", KINDS, "other"),
        "address": address,
        "hostname": _hostname(device),
        "urls": urls,
        "mac": _mac(device),
        "note": _text(device, "note", 1000),
        "source": _choice(device, "source", SOURCES, "manual"),
    }


def validate(devices: object) -> list[dict]:
    """The normalized list, in its order; ValueError (Czech, for the panel) on bad input.
    Unknown keys are dropped."""
    if not isinstance(devices, list):
        raise ValueError("očekáván seznam zařízení")
    if len(devices) > MAX_DEVICES:
        raise ValueError(f"nejvýš {MAX_DEVICES} zařízení")
    out = []
    for i, device in enumerate(devices, 1):
        try:
            out.append(_device(device))
        except ValueError as e:
            raise ValueError(f"Zařízení {i}: {e}") from None
    return out


def load() -> list[dict]:
    try:
        return validate(json.loads(_path().read_text()))
    except (FileNotFoundError, ValueError):
        return []


def save(devices: object) -> list[dict]:
    """Validate and replace the whole list; returns what was stored."""
    stored = validate(devices)
    write_private(_path(), json.dumps(stored, ensure_ascii=False).encode())
    return stored


def import_devices(devices: object) -> dict:
    """Merge Homecraft's old project list into the stored one (#635), by address or MAC.

    Stored entries win; only their empty fields are filled. New ones get source
    "manual". Invalid entries are dropped. Idempotent. Returns the counts."""
    if not isinstance(devices, list):
        raise TypeError("devices")
    stored = load()
    counts = {"added": 0, "updated": 0, "unchanged": 0, "invalid": 0, "over_limit": 0}
    for raw in devices:
        try:
            new = _device({**raw, "source": "manual"} if isinstance(raw, dict) else raw)
        except ValueError:
            counts["invalid"] += 1
            continue
        match = next(
            (
                d
                for d in stored
                if d["address"].lower() == new["address"].lower()
                or (new["mac"] and d["mac"] == new["mac"])
            ),
            None,
        )
        if match is None:
            if len(stored) >= MAX_DEVICES:
                counts["over_limit"] += 1
                continue
            stored.append(new)
            counts["added"] += 1
            continue
        missing = {k: v for k, v in new.items() if v and not match.get(k)}
        match.update(missing)
        counts["updated" if missing else "unchanged"] += 1
    if counts["added"] or counts["updated"]:
        save(stored)
    return counts
