"""What Homecraft installed in this HA (homecraftcz/homecraft#636): /data/features.json.

Homecraft sends the summary with `features.report` after every Apply and on
connect; the panel's Features tab reads the file on each request. Read-only for
the panel: only Homecraft writes it, the whole summary at once."""

import json
import math
import time
from urllib.parse import urlsplit

from . import settings
from .files import write_private

STATUSES = ("running", "update", "addon_error")
MAX_FEATURES = 100
MAX_ITEMS = 200  # instances, files and devices of one feature
EMPTY = {"project_url": "", "features": [], "received_at": None}


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
    return {
        "name": _text(f, "name", 100, required=True),
        "version": _text(f, "version", 64),
        "available": _text(f, "available", 64),
        "instances": _texts(f, "instances", 100),
        "applied_at": applied_at,
        "status": status,
        "error": _text(f, "error", 500),
        "files": _texts(f, "files", 255),
        "devices": _texts(f, "devices", 255),  # network device addresses (#634)
    }


def validate(report: object) -> dict:
    """The normalized summary; ValueError on bad input. Unknown keys are dropped."""
    if not isinstance(report, dict):
        raise ValueError("expected an object")
    features = report.get("features")
    if not isinstance(features, list) or len(features) > MAX_FEATURES:
        raise ValueError(f"features: a list of at most {MAX_FEATURES}")
    return {"project_url": _project_url(report), "features": [_feature(f) for f in features]}


def load() -> dict:
    try:
        stored = json.loads(_path().read_text())
        return {**validate(stored), "received_at": stored.get("received_at")}
    except (FileNotFoundError, ValueError, AttributeError):
        return dict(EMPTY)


def save(report: object) -> dict:
    """Validate and replace the stored summary; returns what was stored."""
    stored = {**validate(report), "received_at": time.time()}
    write_private(_path(), json.dumps(stored, ensure_ascii=False).encode())
    return stored
