import asyncio
import base64
import time
import uuid

import pytest
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from connector import audit, panel, policy, settings
from connector.client import Client


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    audit._recent.clear()
    return tmp_path


@pytest.fixture
def card_key(monkeypatch):
    """A fresh card signing key pinned as the only CARD_PUBLIC_KEYS entry;
    returns sign(text) -> base64 signature."""
    key = Ed25519PrivateKey.generate()
    monkeypatch.setattr(policy, "CARD_PUBLIC_KEYS", (key.public_key(),))
    return lambda text: base64.b64encode(key.sign(text.encode())).decode()


@pytest.fixture
def agent():
    return Client("ws://127.0.0.1:1/ws", uuid.uuid4(), Ed25519PrivateKey.generate(), "t")


@pytest.fixture
def run_panel(agent, monkeypatch):
    """Run `scenario(http)` against the panel, its requests coming from the
    ingress proxy (the test client is 127.0.0.1) unless `from_ingress=False`."""

    # No HA in tests: a fresh cached house name, so no lookup starts.
    cache = {"value": "Dům Test", "at": time.monotonic(), "task": None}
    monkeypatch.setattr(panel, "_house_cache", cache)

    def run(scenario, from_ingress=True):
        monkeypatch.setattr(settings, "DEV", False)
        if from_ingress:
            monkeypatch.setattr(panel, "INGRESS_PROXY", "127.0.0.1")

        async def go():
            async with TestClient(TestServer(panel.make_app(agent, "CODE", "ABC-DEF"))) as http:
                return await scenario(http)

        return asyncio.run(go())

    return run
