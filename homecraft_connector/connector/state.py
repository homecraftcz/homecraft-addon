"""The panel's switches that survive a restart: /data/state.json (#556) holds
`paused` and `error_reports` (#31); saving one keeps the other."""

import json

from . import settings
from .files import write_private


def _path():
    return settings.DATA_DIR / "state.json"


def _load() -> dict:
    try:
        data = json.loads(_path().read_text())
    except (FileNotFoundError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(**changes: object) -> None:
    write_private(_path(), json.dumps({**_load(), **changes}).encode())


def load_paused() -> bool:
    return _load().get("paused") is True


def save_paused(paused: bool) -> None:
    _save(paused=bool(paused))


def load_error_reports() -> bool:
    """Consent to send error reports to Homecraft; off unless switched on."""
    return _load().get("error_reports") is True


def save_error_reports(enabled: bool) -> None:
    _save(error_reports=bool(enabled))
