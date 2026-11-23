import asyncio
import base64
import json

import pytest

from connector import settings, system_log
from connector.policy import OpError

INCLUDE = "homeassistant:\n  packages: !include_dir_named packages\n"
DUPLICATE = (
    "Setup of package 'zz_tmp' failed: integration 'system_log' has duplicate key 'fire_event'"
)


@pytest.fixture
def root(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setattr(settings, "CONFIG_ROOT", config)
    return config


class FakeHA:
    def __init__(self, errors=None, warnings=None, restart_error=None):
        self.errors, self.warnings, self.restart_error = errors, warnings, restart_error
        self.calls = []

    async def core(self, method, path):
        self.calls.append((method, path))
        if path == "/api/services/homeassistant/restart" and self.restart_error:
            raise OpError(self.restart_error, "ServerDisconnectedError")
        body = json.dumps(
            {
                "result": "invalid" if self.errors else "valid",
                "errors": self.errors,
                "warnings": self.warnings,
            }
        )
        return {"status": 200, "headers": {}, "body_b64": base64.b64encode(body.encode()).decode()}


def enable(root, configuration, ha=None):
    (root / "configuration.yaml").write_text(configuration)
    return asyncio.run(system_log.enable(ha or FakeHA()))


def package(root):
    return root / system_log.FILE


def test_writes_the_package(root):
    result = enable(root, INCLUDE)
    assert result["state"] == "restart_needed"
    text = package(root).read_text()
    assert text.startswith(system_log.MARK + "\n") and "fire_event: true" in text
    assert package(root).stat().st_mode & 0o777 == 0o644
    assert not list((root / "packages").glob("*.hfb-tmp"))


def test_merges_with_an_own_system_log_without_fire_event(root):
    assert enable(root, INCLUDE + "system_log:\n  max_entries: 60\n")["state"] == "restart_needed"
    assert package(root).exists()


@pytest.mark.parametrize(
    ("config", "state", "hint"),
    [
        ("system_log:\n  fire_event: true\n", "own_on", None),
        ("system_log:\n  fire_event: false\n", "own_config", system_log.HINT_OWN),
        ("system_log: !include system_log.yaml\n", "own_config", system_log.HINT_TAGGED),
        ("system_log:\n  fire_event: [\n", "own_config", system_log.HINT_TAGGED),  # unparsable
    ],
)
def test_own_config_is_left_alone(root, config, state, hint):
    ha = FakeHA()
    result = enable(root, INCLUDE + config, ha)
    assert (result["state"], result["hint"]) == (state, hint)
    assert not package(root).exists() and ha.calls == []


def test_no_packages_include(root):
    result = enable(root, "default_config:\n")
    assert result["state"] == "no_packages" and result["hint"] == system_log.HINT_NO_PACKAGES
    assert not package(root).exists()


def test_conflict_in_warnings_removes_the_file(root):
    result = enable(root, INCLUDE, FakeHA(warnings=DUPLICATE))
    assert result["state"] == "conflict" and result["message"] == DUPLICATE
    assert not package(root).exists()


def test_ha_unreachable_removes_the_file(root):
    class Down(FakeHA):
        async def core(self, method, path):
            raise OpError("ha_unreachable", "ClientConnectorError")

    assert enable(root, INCLUDE, Down())["state"] == "error"
    assert not package(root).exists()


def test_a_foreign_file_is_never_touched(root):
    (root / "packages").mkdir()
    package(root).write_text("system_log:\n  fire_event: true\n")
    assert enable(root, INCLUDE)["state"] == "conflict"
    assert asyncio.run(system_log.disable())["state"] == "off"
    assert package(root).read_text() == "system_log:\n  fire_event: true\n"


def test_disable_removes_our_file(root):
    enable(root, INCLUDE)
    assert asyncio.run(system_log.disable())["state"] == "restart_needed"
    assert not package(root).exists()
    assert asyncio.run(system_log.disable())["state"] == "off"


def test_restart_only_after_a_clean_check(root):
    ha = FakeHA(warnings=DUPLICATE)
    assert asyncio.run(system_log.restart(ha)) == DUPLICATE
    assert ha.calls == [("POST", "/api/config/core/check_config")]

    ha = FakeHA(restart_error="ha_unreachable")  # HA dropped the connection: restarting
    assert asyncio.run(system_log.restart(ha)) is None
    assert ha.calls[-1] == ("POST", "/api/services/homeassistant/restart")
