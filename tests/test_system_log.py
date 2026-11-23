import asyncio
import base64
import json

import pytest

from connector import errors, settings, state, system_log
from connector.policy import OpError

INCLUDE = "homeassistant:\n  packages: !include_dir_named packages\n"
DUPLICATE = (
    "Setup of package 'zz_tmp' failed: integration 'system_log' has duplicate key 'fire_event'"
)


@pytest.fixture
def root(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(settings, "CONFIG_ROOT", config)
    monkeypatch.setattr(settings, "DATA_DIR", data)
    return config


class FakeHA:
    def __init__(self, errors=None, warnings=None, restart_status=200, down=None):
        self.errors, self.warnings, self.restart_status = errors, warnings, restart_status
        self.down = down  # an OpError code every call raises
        self.calls = []

    async def core(self, method, path):
        self.calls.append((method, path))
        if self.down:
            raise OpError(self.down, "ClientConnectorError")
        if path == "/api/services/homeassistant/restart":
            if self.restart_status is None:  # HA dropped the connection
                raise OpError("ha_unreachable", "ServerDisconnectedError")
            return {"status": self.restart_status, "headers": {}, "body_b64": ""}
        body = json.dumps(
            {
                "result": "invalid" if self.errors else "valid",
                "errors": self.errors,
                "warnings": self.warnings,
            }
        )
        return {"status": 200, "headers": {}, "body_b64": base64.b64encode(body.encode()).decode()}


def enable(root, configuration, ha=None, previous=None):
    (root / "configuration.yaml").write_text(configuration)
    return asyncio.run(system_log.enable(ha or FakeHA(), previous))


def package(root):
    return root / system_log.FILE


def test_writes_the_package(root):
    assert enable(root, INCLUDE) == system_log.status("restart_needed")
    text = package(root).read_text()
    assert text.startswith(system_log.MARK + "\n") and "fire_event: true" in text
    assert not list((root / "packages").glob("*.hfb-tmp"))


def test_merges_with_an_own_system_log_without_fire_event(root):
    assert enable(root, INCLUDE + "system_log:\n  max_entries: 60\n")["state"] == "restart_needed"
    assert package(root).exists()


@pytest.mark.parametrize(
    ("config", "state", "reason"),
    [
        ("system_log:\n  fire_event: true\n", "own_on", None),
        ("system_log:\n  fire_event: false\n", "own_config", "own_off"),
        ("system_log: !include system_log.yaml\n", "own_config", "tagged"),
        ("system_log:\n  fire_event: [\n", "own_config", "tagged"),  # unparsable
    ],
)
def test_own_config_is_left_alone(root, config, state, reason):
    ha = FakeHA()
    result = enable(root, INCLUDE + config, ha)
    assert (result["state"], result["reason"]) == (state, reason)
    assert not package(root).exists() and ha.calls == []


def test_no_packages_include(root):
    assert enable(root, "default_config:\n")["state"] == "no_packages"
    assert not package(root).exists()


def test_conflict_in_warnings_removes_the_file(root):
    result = enable(root, INCLUDE, FakeHA(warnings=DUPLICATE))
    assert result == system_log.status("conflict", detail=DUPLICATE)
    assert not package(root).exists()


def test_an_unrelated_invalid_config_keeps_the_file(root):
    result = enable(root, INCLUDE, FakeHA(errors="Invalid config for 'light'"))
    assert result == system_log.status("ha_config_invalid", detail="Invalid config for 'light'")
    assert package(root).exists()


def test_ha_unreachable_removes_only_a_new_file(root):
    assert enable(root, INCLUDE, FakeHA(down="ha_unreachable")) == system_log.status(
        "error", "ha_unreachable"
    )
    assert not package(root).exists()
    enable(root, INCLUDE)  # written; HA may run with it
    assert enable(root, INCLUDE, FakeHA(down="ha_unreachable"))["state"] == "error"
    assert package(root).exists()


def test_an_existing_file_keeps_the_saved_status(root):
    enable(root, INCLUDE)
    active = system_log.status("active")
    assert enable(root, INCLUDE, previous=active) == active
    assert enable(root, INCLUDE, previous=system_log.status("error", "x"))["state"] == (
        "restart_needed"
    )


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
    assert asyncio.run(system_log.restart(ha)) == ("conflict", DUPLICATE)
    assert ha.calls == [("POST", "/api/config/core/check_config")]

    ha = FakeHA(errors="Invalid config for 'light'")
    assert asyncio.run(system_log.restart(ha))[0] == "ha_config_invalid"

    ha = FakeHA(restart_status=None)  # HA dropped the connection: restarting
    assert asyncio.run(system_log.restart(ha)) is None
    assert ha.calls[-1] == ("POST", "/api/services/homeassistant/restart")

    assert asyncio.run(system_log.restart(FakeHA(restart_status=500))) == (
        "restart_failed",
        "HTTP 500",
    )


@pytest.fixture
def probe(monkeypatch):
    calls = []

    def set_result(result):
        async def fake(session):
            calls.append(1)
            return result

        monkeypatch.setattr(system_log, "probe", fake)
        return calls

    return set_result


def recheck(consent=True):
    async def go():
        return await system_log.recheck(None, lambda: consent)

    return asyncio.run(go())


def test_recheck_probes_a_waiting_state(root, probe):
    calls = probe(True)
    state.save_system_log(system_log.status("restart_needed"))
    assert recheck() == system_log.status("active") == state.load_system_log()
    assert calls == [1]

    probe(False)
    state.save_system_log(system_log.status("own_on"))
    assert recheck() == system_log.status("own_on", "probe_failed")


def test_recheck_enables_again_after_the_config_is_fixed(root, probe, monkeypatch):
    calls = probe(False)
    monkeypatch.setattr(system_log.http_proxy, "HttpProxy", lambda session: FakeHA())
    (root / "configuration.yaml").write_text(INCLUDE)
    state.save_system_log(system_log.status("no_packages"))
    assert recheck()["state"] == "restart_needed" and package(root).exists()
    assert calls == []  # a fresh file needs the restart first


def test_recheck_with_the_consent_off(root, probe):
    probe(False)
    state.save_system_log(system_log.status("restart_needed"))
    assert recheck(consent=False) == system_log.status("off")
    calls = probe(True)
    calls.clear()
    state.save_system_log(system_log.status("off"))
    assert recheck(consent=False) == system_log.status("off") and calls == []


def test_the_forwarder_ignores_the_probe():
    data = {"name": system_log.PROBE_LOGGER, "level": "ERROR", "message": ["test"]}
    assert errors.log_entity(data) is None
