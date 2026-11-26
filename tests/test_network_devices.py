"""Network devices kept by the add-on (#631): validation, persistence, panel API."""

import pytest

from connector import network_devices, panel

CSRF = {panel.CSRF_HEADER: panel.NONCE}

GATEWAY = {
    "name": " BLU brána – půda ",
    "kind": "shelly_gateway",
    "address": "10.0.0.10",
    "hostname": "shellyblugwg3-a8032ab1c2d4.local",
    "urls": ["http://10.0.0.10", " ", "rtsp://10.0.0.41:554/stream1"],
    "mac": "a8-03-2a-b1-c2-d4",
    "note": "půda",
    "extra": "dropped",
}


def test_normalizes_a_device():
    [d] = network_devices.validate([GATEWAY])
    assert d == {
        "name": "BLU brána – půda",
        "kind": "shelly_gateway",
        "address": "10.0.0.10",
        "hostname": "shellyblugwg3-a8032ab1c2d4.local",
        "urls": ["http://10.0.0.10", "rtsp://10.0.0.41:554/stream1"],
        "mac": "A8:03:2A:B1:C2:D4",
        "note": "půda",
        "source": "manual",
    }
    [d] = network_devices.validate([{"name": "NAS", "address": "nas", "mac": ""}])
    assert d["kind"] == "other" and d["urls"] == [] and d["mac"] is None and d["hostname"] == ""


@pytest.mark.parametrize(
    "change",
    [
        {"urls": ["http://admin:secret@10.0.0.10"]},
        {"urls": ["rtsp://admin@10.0.0.41/stream"]},
        {"urls": ["javascript:alert(1)"]},
        {"urls": ["http://"]},
        {"urls": ["http://10.0.0.10:99999"]},
        {"urls": ["http://h/" + "x" * 500]},
        {"urls": [f"http://10.0.0.{i}" for i in range(21)]},
        {"name": " "},
        {"name": "x" * 101},
        {"address": "10.0.0.1 x"},
        {"address": ""},
        {"hostname": "nas.lan"},
        {"hostname": ".local"},
        {"hostname": "bad_name.local"},
        {"mac": "a8:03:2a-b1:c2:d4"},
        {"kind": "toaster"},
        {"source": "cloud"},
        {"note": "x" * 1001},
        {"name": 5},
    ],
)
def test_rejects(change):
    with pytest.raises(ValueError):
        network_devices.validate([{**GATEWAY, **change}])


@pytest.mark.parametrize("url", ["http://admin:secret@10.0.0.10", "ftp:\\\\a:secret@h"])
def test_credentials_are_not_echoed(url):
    with pytest.raises(ValueError) as e:
        network_devices.validate([{**GATEWAY, "urls": [url]}])
    assert "secret" not in str(e.value)


def test_list_limits():
    with pytest.raises(ValueError):
        network_devices.validate({"name": "x"})
    with pytest.raises(ValueError):
        network_devices.validate([{"name": "x", "address": "a"}] * 201)


def test_persists_across_reload(data_dir):
    assert network_devices.load() == []
    stored = network_devices.save([GATEWAY])
    assert network_devices.load() == stored
    assert (data_dir / "network_devices.json").stat().st_mode & 0o777 == 0o600
    (data_dir / "network_devices.json").write_text("{broken")
    assert network_devices.load() == []


def test_panel_api(run_panel):
    async def scenario(http):
        empty = await (await http.get("/api/network-devices")).json()
        no_csrf = (await http.put("/api/network-devices", json=[GATEWAY])).status
        bad = await http.put(
            "/api/network-devices",
            json=[{**GATEWAY, "urls": ["http://u:p@10.0.0.10"]}],
            headers=CSRF,
        )
        ok = await http.put("/api/network-devices", json=[GATEWAY], headers=CSRF)
        after = await (await http.get("/api/network-devices")).json()
        return empty, no_csrf, bad.status, (await bad.json())["ok"], ok.status, after

    empty, no_csrf, bad, bad_ok, ok, after = run_panel(scenario)
    assert (empty, no_csrf, bad, bad_ok, ok) == ([], 403, 400, False, 200)
    assert after == network_devices.load() and after[0]["mac"] == "A8:03:2A:B1:C2:D4"


def test_hosts_are_the_stored_addresses_and_names():
    network_devices.save(
        [
            {"name": "Mesh", "address": "10.0.0.2", "hostname": "Mesh.local"},
            {"name": "NAS", "address": "nas.lan"},
        ]
    )
    assert network_devices.hosts() == {"10.0.0.2", "mesh.local", "nas.lan"}
