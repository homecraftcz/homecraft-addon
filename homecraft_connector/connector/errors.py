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
HA_RETRY_FIRST = 3  # s after the first failed HA connection
HA_RETRY = 30  # s after repeated failures
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
SUBSCRIBE = ("homecraft_error", "system_log_event")

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
    # Default json.dumps (ASCII escapes, ", " separators) is never smaller than
    # the compact UTF-8 JSON, so the limit holds on Homecraft's side too.
    # Size of json.dumps(trace): brackets, the entries and a ", " between them.
    sizes = [len(json.dumps(e)) for e in trace]
    total = 2 + sum(sizes) + 2 * max(len(sizes) - 1, 0)
    drop = 0
    while drop < len(sizes) and total > MAX_TRACE_BYTES:
        total -= sizes[drop] + (2 if drop < len(sizes) - 1 else 0)
        drop += 1
    return trace[drop:]


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


def _labelled(key: object, domain: str) -> bool:
    """`automation`/`script` or a labelled `automation <label>` key of a package."""
    return key == domain or (isinstance(key, str) and key.startswith(domain + " "))


def owned(config_root: Path) -> dict[tuple[str, str], tuple[str, str]]:
    """(domain, config id) → (feature, version) of the automations (their `id`)
    and scripts (their key) in Apply's `packages/hfb_*.yaml` (those with the
    `# ha-builder:` marker)."""
    index: dict[tuple[str, str], tuple[str, str]] = {}
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
            if _labelled(key, "automation"):
                for item in value if isinstance(value, list) else [value]:
                    if isinstance(item, dict) and isinstance(item.get("id"), str):
                        index[("automation", item["id"])] = owner
            elif _labelled(key, "script") and isinstance(value, dict):
                for name in value:
                    if isinstance(name, str):
                        index[("script", name)] = owner
    return index


_cache: tuple[tuple, dict] | None = None


def owned_cached(config_root: Path) -> dict[tuple[str, str], tuple[str, str]]:
    """owned(), parsed again only when a package file's mtime or size changed."""
    global _cache
    try:
        key = tuple(
            (p.name, st.st_mtime_ns, st.st_size)
            for p in sorted((config_root / "packages").glob("hfb_*.yaml"))
            for st in [p.stat()]
        )
    except OSError:
        key = None
    if key is None or _cache is None or _cache[0] != key:
        _cache = (key, owned(config_root))
    return _cache[1]


def log_entity(data: object) -> str | None:
    """The automation or script entity a `system_log_event` of level ERROR or
    CRITICAL is logged for, from the logger name; None for anything else."""
    if not isinstance(data, dict) or data.get("level") not in LEVELS:
        return None
    name = data.get("name")
    m = LOGGER_RE.fullmatch(name) if isinstance(name, str) else None
    return f"{m.group(1)}.{m.group(2)}" if m else None


def from_log(data: dict, entity_id: str, config_id: str | None, index: dict) -> dict | None:
    """A `system_log_event` (passed `log_entity` → `entity_id`) as a report when
    the entity's config id (an automation's `id` attribute, a script's key) is
    Homecraft's: in a package or `hfb_*`. The customer's own never, even when it
    holds the entity ID a Homecraft automation would have had."""
    if not config_id:
        return None
    domain, object_id = entity_id.split(".", 1)
    if (domain, config_id) in index:
        feature, version = index[(domain, config_id)]
    elif config_id.startswith("hfb_"):
        feature, version = "unknown", None
    else:
        return None
    report = {
        "feature": feature[: FIELDS["feature"]],
        "kind": "log",
        "code": object_id[: FIELDS["code"]],
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


def _key(report: dict) -> tuple:
    return tuple(report.get(f) for f in ("feature", "code", "source", "entity_id"))


class Dedup:
    """The same error (feature, code, source, entity_id) goes out at once and
    then at most once per DEDUP_WINDOW, merged with `count`; the merged report
    keeps the first occurrence's trace and message."""

    def __init__(self) -> None:
        # key → {"last_sent", "prev_sent", "pending"}
        self._keys: dict[tuple, dict] = {}
        self._warned = False

    def add(self, report: dict, wall: datetime) -> None:
        key = _key(report)
        entry = self._keys.get(key)
        if entry is None:
            if len(self._keys) >= MAX_KEYS:
                if not self._warned:
                    logger.warning("Příliš mnoho různých chyb najednou, další se zahodí")
                    self._warned = True
                return
            entry = self._keys[key] = {"last_sent": None, "prev_sent": None, "pending": None}
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
                entry["prev_sent"], entry["last_sent"] = entry["last_sent"], now
                entry["pending"] = None
            elif entry["pending"] is None and fresh:
                del self._keys[key]
        if len(self._keys) < MAX_KEYS:
            self._warned = False
        return out

    def unsent(self, reports: list[dict]) -> None:
        """Reports from `due` that were dropped (offline, paused): gone, but they
        do not count as sent, so the next occurrence goes out at once."""
        for report in reports:
            if entry := self._keys.get(_key(report)):
                entry["last_sent"] = entry["prev_sent"]


class Forwarder:
    """Subscribes to HA's events while the consent is on, Homecraft is
    connected and not paused; `run` never ends."""

    def __init__(self, client) -> None:
        self.client = client
        self.dedup = Dedup()
        self._failures = 0  # HA connection attempts failed in a row
        self._http: aiohttp.ClientSession | None = None

    def active(self) -> bool:
        c = self.client
        return c.error_reports and not c.paused and c.connected

    async def _index(self) -> dict:
        return await asyncio.to_thread(owned_cached, settings.CONFIG_ROOT)

    async def _config_id(self, entity_id: str) -> str | None:
        """An automation's config id (its `id` state attribute; the entity ID
        may belong to anyone), a script's key (the logger carries the key)."""
        domain, object_id = entity_id.split(".", 1)
        if domain == "script":
            return object_id
        if self._http is None:
            return None
        try:
            async with self._http.get(
                f"{settings.CORE_HTTP_URL}/api/states/{entity_id}",
                headers={"Authorization": f"Bearer {settings.SUPERVISOR_TOKEN}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status != 200:
                    return None
                state = await r.json()
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return None
        attrs = state.get("attributes") if isinstance(state, dict) else None
        config_id = attrs.get("id") if isinstance(attrs, dict) else None
        return config_id if isinstance(config_id, str) else None

    async def handle(self, msg: dict, wall: datetime) -> None:
        """One HA event message."""
        if not self.active() or msg.get("type") != "event":
            return
        event = msg.get("event")
        if not isinstance(event, dict):
            return
        kind, data = event.get("event_type"), event.get("data")
        if kind == "homecraft_error":
            report = clean(data)
        elif kind == "system_log_event" and (entity_id := log_entity(data)):
            config_id = await self._config_id(entity_id)
            report = from_log(data, entity_id, config_id, await self._index())
        else:
            return
        if report is not None:
            self.dedup.add(report, wall)

    async def flush(self, now: float) -> None:
        if not self.active():
            return
        if due := self.dedup.due(now):
            sent = await self.client.report_errors(due)
            self.dedup.unsent(due[sent:])

    async def run(self) -> None:
        async with aiohttp.ClientSession() as http:
            self._http = http
            while True:
                if not self.active():
                    self.dedup = Dedup()
                    await asyncio.sleep(FLUSH_EVERY)
                    continue
                try:
                    await self._listen(http)
                except (policy.OpError, aiohttp.ClientError, TimeoutError, ConnectionError) as e:
                    self._failed(type(e).__name__)
                except Exception:
                    logger.exception("Přeposílání chyb selhalo")
                    self._failed(None)
                else:
                    continue  # stopped by the consent, pause or offline: no wait
                await asyncio.sleep(HA_RETRY_FIRST if self._failures == 1 else HA_RETRY)

    def _failed(self, what: str | None) -> None:
        self._failures += 1
        if self._failures == 1 and what:
            logger.warning("Odběr chyb z Home Assistantu selhal: %s", what)

    async def _listen(self, http: aiohttp.ClientSession) -> None:
        ws = await ws_proxy.connect(http)
        try:
            for i, event_type in enumerate(SUBSCRIBE, 1):
                await ws.send_json({"id": i, "type": "subscribe_events", "event_type": event_type})
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
                            if msg.get("type") == "result":
                                self._subscribed(msg)
                            else:
                                await self.handle(msg, datetime.now(UTC))
                await self.flush(time.monotonic())
        finally:
            await ws.close()

    def _subscribed(self, msg: dict) -> None:
        """A subscribe_events result: a refusal reconnects, both accepted reset the backoff."""
        if msg.get("success") is not True:
            raise ConnectionError("subscribe_events refused")
        if msg.get("id") == len(SUBSCRIBE):
            self._failures = 0
