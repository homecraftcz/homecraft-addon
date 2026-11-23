"""The features summary Homecraft reports (homecraftcz/homecraft#636): op, file, panel API."""

import pytest
from test_client_ops import run_against_homecraft

from connector import audit, client, features

HEATING = {
    "name": "Topení",
    "version": "1.17.0",
    "available": "1.18.0",
    "instances": ["Okruh 1", "Okruh 2"],
    "applied_at": 1763900000,
    "status": "update",
    "error": "",
    "files": ["packages/hfb_heating.yaml"],
    "devices": [],
    "extra": "dropped",
}
REPORT = {"project_url": "https://app.homecraft.cz/projects/dum", "features": [HEATING]}


def test_report_is_stored_and_read_back_during_a_pause():
    got = run_against_homecraft(
        [
            {"id": 1, "op": "features.report", **REPORT},
            {"id": 2, "op": "features.report", "features": "x"},
        ],
        expect=2,
        paused=True,
    )
    by_id = {g["id"]: g for g in got}
    assert by_id[1]["ok"] is True
    assert by_id[2]["error"]["code"] == "bad_request"
    stored = features.load()
    assert stored["project_url"] == REPORT["project_url"]
    assert stored["features"] == [{k: v for k, v in HEATING.items() if k != "extra"}]
    assert stored["received_at"] > 0
    assert {"Features (1)", "Features"} <= {e["target"] for e in audit.recent()}
    assert client._audit_target({"op": "features.report", **REPORT}) == "Features (1)"


@pytest.mark.parametrize(
    "report",
    [
        {**REPORT, "project_url": "javascript:alert(1)"},
        {**REPORT, "project_url": "https://user:pw@example.com"},
        {**REPORT, "features": [{**HEATING, "status": "broken"}]},
        {**REPORT, "features": [{**HEATING, "name": " "}]},
        {**REPORT, "features": [{**HEATING, "applied_at": "yesterday"}]},
        {**REPORT, "features": [{**HEATING, "files": ["x" * 256]}]},
        {**REPORT, "features": [HEATING] * (features.MAX_FEATURES + 1)},
    ],
)
def test_bad_reports_are_refused(report):
    with pytest.raises(ValueError):
        features.validate(report)


def test_load_without_a_file_is_empty():
    assert features.load() == features.EMPTY


def test_panel_reads_the_file_on_each_request(run_panel):
    async def scenario(http):
        before = await (await http.get("/api/features")).json()
        features.save(REPORT)
        return before, await (await http.get("/api/features")).json()

    before, after = run_panel(scenario)
    assert before["features"] == []
    assert after["features"][0]["name"] == "Topení"
