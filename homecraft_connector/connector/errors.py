"""Error reports of Homecraft's automations to Homecraft (#31), with the
customer's consent (panel switch, off by default).

The add-on subscribes in HA to `homecraft_error` (Node-RED, homecraft#857) and
`system_log_event` (only ERROR/CRITICAL of Homecraft's automations and scripts),
keeps only the allowlisted fields below, merges repeats (DEDUP_WINDOW) and sends
`{"op": "errors.report", "reports": [...]}` over the Homecraft connection. Offline,
paused or without consent the reports are dropped; nothing goes to disk or the log.
The allowlist lives only here, never in the options or from the server."""

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
import yaml

from . import policy, settings, ws_proxy

logger = logging.getLogger(__name__)

DEDUP_WINDOW = 10 * 60
FLUSH_EVERY = 5.0  # s
MAX_REPORTS = 20  # per frame
MAX_KEYS = 100  # distinct errors in memory
HA_RETRY = 30  # s after HA dropped or refused
MAX_TRACE = 50
MAX_TRACE_BYTES = 12 * 1024
FIELDS = {
    "feature": 64,
    "version": 32,
    "code": 64,
    "source": 128,
    "node_id": 64,
    "entity_id": 128,
    "area_id": 64,
    "message": 500,
}
KINDS = ("domain", "exception")
CIRCUIT_MAX = 32
LEVELS = ("ERROR", "CRITICAL")

# HA logs an automation's errors under `homeassistant.components.automation.<object_id>`
# and a script's under `homeassistant.components.script.<object_id>`.
LOGGER_RE = re.compile(r"homeassistant\.components\.(automation|script)\.([a-z0-9_]+)")
# Apply's first line of every package (homecraft feature_plan_service).
MARKER_RE = re.compile(r"^# ha-builder: ([a-z0-9_-]+)@(\S+)$")

_SCALARS = (str, int, float, bool, type(None))


def _trace(value: object) -> list[dict]:
    """Flat dicts with scalar values only, the newest MAX_TRACE, at most
    MAX_TRACE_BYTES of JSON (oldest dropped first)."""
    if not isinstance(value, list):
        return []
    trace = []
    for item in value[-MAX_TRACE:]:
        if not isinstance(item, dict):
            continue
        flat = {k: v for k, v in item.items() if isinstance(k, str) and isinstance(v, _SCALARS)}
        if flat:
            trace.append(flat)
    # Default json.dumps (ASCII escapes, spaces) is never smaller than the
    # compact UTF-8 JSON, so the limit holds on Homecraft's side too.
    while trace and len(json.dumps(trace)) > MAX_TRACE_BYTES:
        trace.pop(0)
    return trace


def clean(data: object) -> dict | None:
    """A `homecraft_error` event's data cut down to the allowlist; None = drop."""
    if not isinstance(data, dict) or data.get("kind") not in KINDS:
        return None
    report: dict = {"kind": data["kind"]}
    for name, limit in FIELDS.items():
        value = data.get(name)
        if isinstance(value, str) and value:
            report[name] = value[:limit]
    if "code" not in report:
        return None
    report.setdefault("feature", "unknown")
    circuit = data.get("circuit")
    if isinstance(circuit, int) and not isinstance(circuit, bool):
        report["circuit"] = circuit
    elif isinstance(circuit, str) and circuit:
        report["circuit"] = circuit[:CIRCUIT_MAX]
    if trace := _trace(data.get("trace")):
        report["trace"] = trace
    return report


def owned(config_root: Path) -> dict[str, tuple[str, str]]:
    """entity_id → (feature, version) of the automations and scripts in Apply's
    `packages/hfb_*.yaml` (those with the `# ha-builder:` marker)."""
    index: dict[str, tuple[str, str]] = {}
    for path in sorted((config_root / "packages").glob("hfb_*.yaml")):
        try:
            text = path.read_text(encoding="utf-8")
            marker = MARKER_RE.match(text.split("\n", 1)[0].rstrip("\r"))
            if marker is None:
                continue
            data = yaml.safe_load(text)
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            continue
        if not isinstance(data, dict):
            continue
        owner = marker.group(1), marker.group(2)
        for key, value in data.items():
            if key == "automation" or (isinstance(key, str) and key.startswith("automation ")):
                for item in value if isinstance(value, list) else [value]:
                    if isinstance(item, dict) and isinstance(item.get("id"), str):
                        index[f"automation.{item['id']}"] = owner
        scripts = data.get("script")
        if isinstance(scripts, dict):
            for key in scripts:
                if isinstance(key, str):
                    index[f"script.{key}"] = owner
    return index


def _logger_match(data: object) -> re.Match | None:
    if not isinstance(data, dict) or data.get("level") not in LEVELS:
        return None
    name = data.get("name")
    return LOGGER_RE.fullmatch(name) if isinstance(name, str) else None


def from_log(data: object, index: dict) -> dict | None:
    """A `system_log_event` of Homecraft's automation or script as a report;
    None for anything else (the customer's own automations never)."""
    m = _logger_match(data)
    if m is None:
        return None
    domain, object_id = m.groups()
    entity_id = f"{domain}.{object_id}"
    if entity_id in index:
        feature, version = index[entity_id]
    elif object_id.startswith("hfb_"):
        feature, version = "unknown", None
    else:
        return None
    report = {
        "feature": feature[: FIELDS["feature"]],
        "kind": "log",
        "code": object_id[: FIELDS["code"]] or "system_log",
        "source": data["name"][: FIELDS["source"]],
        "entity_id": entity_id[: FIELDS["entity_id"]],
    }
    if version:
        report["version"] = version[: FIELDS["version"]]
    # Only the first line: the traceback (`exception`) may carry paths and values.
    message = data.get("message")
    if isinstance(message, list):
        message = message[0] if message else None
    if isinstance(message, str) and message.strip():
        report["message"] = message.strip().splitlines()[0][: FIELDS["message"]]
    return report


def _iso(wall: datetime) -> str:
    return wall.astimezone(UTC).isoformat(timespec="seconds")


class Dedup:
    """The same error (feature, code, source, entity_id) goes out at once and
    then at most once per DEDUP_WINDOW, merged with `count`; the merged report
    keeps the first occurrence's trace and message."""

    def __init__(self) -> None:
        self._keys: dict[tuple, dict] = {}  # key → {"last_sent", "pending"}
        self._warned = False

    def add(self, report: dict, now: float, wall: datetime) -> None:
        key = tuple(report.get(f) for f in ("feature", "code", "source", "entity_id"))
        entry = self._keys.get(key)
        if entry is None:
            if len(self._keys) >= MAX_KEYS:
                if not self._warned:
                    logger.warning("Příliš mnoho různých chyb najednou, další se zahodí")
                    self._warned = True
                return
            entry = self._keys[key] = {"last_sent": None, "pending": None}
        iso = _iso(wall)
        if entry["pending"] is not None:
            entry["pending"]["count"] += 1
            entry["pending"]["last_seen"] = iso
        else:
            entry["pending"] = {**report, "count": 1, "first_seen": iso, "last_seen": iso}

    def due(self, now: float) -> list[dict]:
        out = []
        for key, entry in list(self._keys.items()):
            fresh = entry["last_sent"] is None or now - entry["last_sent"] >= DEDUP_WINDOW
            if entry["pending"] is not None and fresh:
                out.append(entry["pending"])
                entry["last_sent"], entry["pending"] = now, None
            elif entry["pending"] is None and fresh:
                del self._keys[key]
        if len(self._keys) < MAX_KEYS:
            self._warned = False
        return out


class Forwarder:
    """Subscribes to HA's events while the consent is on and Homecraft is not
    paused; `run` never ends."""

    def __init__(self, client) -> None:
        self.client = client
        self.dedup = Dedup()
        self._warned = False

    def active(self) -> bool:
        return self.client.error_reports and not self.client.paused

    async def handle(
        self, msg: dict, index_loader: Callable[[], dict], now: float, wall: datetime
    ) -> None:
        """One HA message; `index_loader` (owned packages) is read off the loop
        and only for a log event of an automation or script."""
        if not self.active() or msg.get("type") != "event":
            return
        event = msg.get("event")
        if not isinstance(event, dict):
            return
        kind, data = event.get("event_type"), event.get("data")
        if kind == "homecraft_error":
            report = clean(data)
        elif kind == "system_log_event" and _logger_match(data):
            # ponytail: reads the packages on every such error (they are rare);
            # cache by the mtime of packages/ once it shows.
            report = from_log(data, await asyncio.to_thread(index_loader))
        else:
            return
        if report is not None:
            self.dedup.add(report, now, wall)

    async def flush(self, now: float) -> None:
        if due := self.dedup.due(now):
            await self.client.report_errors(due)

    async def run(self) -> None:
        async with aiohttp.ClientSession() as http:
            while True:
                if not self.active():
                    self.dedup = Dedup()
                    await asyncio.sleep(FLUSH_EVERY)
                    continue
                try:
                    await self._listen(http)
                except (policy.OpError, aiohttp.ClientError, TimeoutError, ConnectionError) as e:
                    if not self._warned:
                        logger.warning("Odběr chyb z Home Assistantu selhal: %s", type(e).__name__)
                        self._warned = True
                    await asyncio.sleep(HA_RETRY)
                except Exception:
                    logger.exception("Přeposílání chyb selhalo")
                    await asyncio.sleep(HA_RETRY)

    async def _listen(self, http: aiohttp.ClientSession) -> None:
        ws = await ws_proxy.connect(http)
        try:
            for i, event_type in enumerate(("homecraft_error", "system_log_event"), 1):
                await ws.send_json({"id": i, "type": "subscribe_events", "event_type": event_type})
            self._warned = False
            while self.active():
                try:
                    m = await ws.receive(timeout=FLUSH_EVERY)
                except TimeoutError:
                    m = None
                if m is not None:
                    if m.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        raise ConnectionError("HA closed the socket")
                    if m.type == aiohttp.WSMsgType.TEXT:
                        try:
                            msg = json.loads(m.data)
                        except ValueError:
                            msg = None
                        if isinstance(msg, dict):
                            await self.handle(
                                msg,
                                lambda: owned(settings.CONFIG_ROOT),
                                time.monotonic(),
                                datetime.now(UTC),
                            )
                await self.flush(time.monotonic())
        finally:
            await ws.close()
