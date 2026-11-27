"""What Homecraft installed in this HA (homecraftcz/homecraft#636): /data/features.json.

Homecraft sends the summary with `features.report` after every Apply and on
connect; the panel's Features tab reads the file on each request. Read-only for
the panel: only Homecraft writes it, the whole summary at once.

Since homecraftcz/homecraft#945 a feature also carries `id`, `assignment` and
heating's `circuits` or tailscale's `settings`; the report `last_change`,
`technician` and `remote_access_requested_at` (#947). These and any key the
add-on does not know yet are kept, cut to MAX_TEXT / MAX_ITEMS / MAX_DEPTH, and
never refuse the report: a newer Homecraft must not break an older add-on."""

import json
import math
import time
from urllib.parse import urlsplit

from . import settings
from .files import write_private

STATUSES = ("running", "update", "addon_error")
MAX_FEATURES = 100
MAX_ITEMS = 200  # instances, files and devices of one feature; circuits, valves, windows
MAX_TEXT = 500  # a text in the extended fields
MAX_KEYS = 50  # keys of one object in the extended fields
MAX_DEPTH = 6
EMPTY = {
    "project_url": "",
    "features": [],
    "last_change": None,
    "technician": "",
    "remote_access_requested_at": None,
    "received_at": None,
}


def _path():
    return settings.DATA_DIR / "features.json"


def _text(obj: dict, key: str, max_len: int, *, required=False) -> str:
    value = obj.get(key) or ""
    if not isinstance(value, str) or len(value) > max_len:
        raise ValueError(f"{key}: text, at most {max_len} characters")
    if required and not value.strip():
        raise ValueError(f"{key}: missing")
    return value.strip()


def _texts(obj: dict, key: str, max_len: int) -> list[str]:
    value = obj.get(key) or []
    if not isinstance(value, list) or len(value) > MAX_ITEMS:
        raise ValueError(f"{key}: a list of at most {MAX_ITEMS}")
    if any(not isinstance(v, str) or len(v) > max_len for v in value):
        raise ValueError(f"{key}: texts of at most {max_len} characters")
    return value


def _capped(value: object, depth: int = 0) -> object:
    """JSON cut to size: long texts shortened, long lists and objects cut,
    anything deeper than MAX_DEPTH or not JSON becomes None."""
    if isinstance(value, str):
        return value[:MAX_TEXT]
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if depth >= MAX_DEPTH:
        return None
    if isinstance(value, list):
        return [_capped(v, depth + 1) for v in value[:MAX_ITEMS]]
    if isinstance(value, dict):
        items = [(k, v) for k, v in value.items() if isinstance(k, str)][:MAX_KEYS]
        return {k[:100]: _capped(v, depth + 1) for k, v in items}
    return None


def _unknown(obj: dict, known: set[str]) -> dict:
    return _capped({k: v for k, v in obj.items() if k not in known})


def _str(value: object, max_len: int = 100) -> str:
    return value[:max_len] if isinstance(value, str) else ""


def _unix(value: object) -> float | None:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    return value if ok else None


def _last_change(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    texts = {k: _str(value.get(k)) for k in ("actor", "feature", "instance")}
    return {"at": _unix(value.get("at")), **texts}


def _project_url(report: dict) -> str:
    # The panel renders it as a link: http(s) only (no javascript:, data:, ...).
    url = _text(report, "project_url", 500)
    if url:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or "@" in parts.netloc:
            raise ValueError("project_url: an http(s) URL")
    return url


def _feature(f: object) -> dict:
    if not isinstance(f, dict):
        raise ValueError("feature: an object")
    applied_at = f.get("applied_at")
    if applied_at is not None and (
        isinstance(applied_at, bool)
        or not isinstance(applied_at, (int, float))
        or not math.isfinite(applied_at)
    ):
        raise ValueError("applied_at: a unix time or null")
    status = f.get("status", "running")
    if status not in STATUSES:
        raise ValueError(f"status: one of {', '.join(STATUSES)}")
    known = {
        "name": _text(f, "name", 100, required=True),
        "version": _text(f, "version", 64),
        "available": _text(f, "available", 64),
        "instances": _texts(f, "instances", 100),
        "applied_at": applied_at,
        "status": status,
        "error": _text(f, "error", 500),
        "files": _texts(f, "files", 255),
        "devices": _texts(f, "devices", 255),  # network device addresses (#634)
        "id": _str(f.get("id")),  # the library feature: heating, tailscale, ... (#945)
        "assignment": _str(f.get("assignment"), 255),
    }
    if isinstance(f.get("circuits"), list):  # heating (#945)
        known["circuits"] = _capped(f["circuits"])
    if isinstance(f.get("settings"), dict):  # tailscale (#945)
        known["settings"] = _capped(f["settings"])
    return {**_unknown(f, set(known) | {"circuits", "settings"}), **known}


def validate(report: object) -> dict:
    """The normalized summary; ValueError on bad input. Unknown keys are kept, capped."""
    if not isinstance(report, dict):
        raise ValueError("expected an object")
    features = report.get("features")
    if not isinstance(features, list) or len(features) > MAX_FEATURES:
        raise ValueError(f"features: a list of at most {MAX_FEATURES}")
    known = {
        "project_url": _project_url(report),
        "features": [_feature(f) for f in features],
        "last_change": _last_change(report.get("last_change")),
        "technician": _str(report.get("technician")),
        "remote_access_requested_at": _unix(report.get("remote_access_requested_at")),
    }
    # received_at is ours (set on save); op and id belong to the request
    return {**_unknown(report, set(known) | {"received_at", "op", "id"}), **known}


def load() -> dict:
    try:
        stored = json.loads(_path().read_text())
        return {**validate(stored), "received_at": stored.get("received_at")}
    except (FileNotFoundError, ValueError, AttributeError):
        return dict(EMPTY)


def mark_remote_access_requested(at: float) -> None:
    """The owner's request went through (#50): kept until Homecraft's next
    report (which carries the same time), so a reload still shows it."""
    stored = {**load(), "remote_access_requested_at": at}
    write_private(_path(), json.dumps(stored, ensure_ascii=False).encode())


def save(report: object) -> dict:
    """Validate and replace the stored summary; returns what was stored."""
    stored = {**validate(report), "received_at": time.time()}
    write_private(_path(), json.dumps(stored, ensure_ascii=False).encode())
    return stored
