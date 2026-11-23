"""The panel's switches that survive a restart: /data/state.json (#556) holds
`paused`, `error_reports` (#31) and the `system_log` status
(homecraftcz/homecraft#906); saving one keeps the others."""

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


def load_system_log() -> dict:
    """The last system_log.enable/disable/probe status; `off` before any."""
    data = _load().get("system_log")
    return data if isinstance(data, dict) and "state" in data else {"state": "off"}


def save_system_log(status: dict) -> None:
    _save(system_log=status)
