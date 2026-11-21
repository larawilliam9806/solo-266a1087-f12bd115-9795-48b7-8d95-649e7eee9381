"""End-to-end API tests for the calibration service."""

import time

import pytest


def _upload_roster(client, records):
    return client.post("/tables/respondents", json={"records": records})


def _upload_targets(client, records):
    return client.post("/tables/targets", json={"records": records})


ROSTER = [
    {"household_id": "H1", "district": "D1", "size_band": "S1", "init_weight": 1.0},
    {"household_id": "H2", "district": "D1", "size_band": "S2", "init_weight": 2.0},
    {"household_id": "H3", "district": "D2", "size_band": "S1", "init_weight": 1.0},
    {"household_id": "H4", "district": "D2", "size_band": "S2", "init_weight": 3.0},
    {"household_id": "H5", "district": "D1", "size_band": "S1", "init_weight": 4.0},
]

TARGETS = [
    {"dimension": "district", "group_name": "D1", "target_count": 9.0},
    {"dimension": "district", "group_name": "D2", "target_count": 6.0},
    {"dimension": "size_band", "group_name": "S1", "target_count": 8.0},
    {"dimension": "size_band", "group_name": "S2", "target_count": 7.0},
]


def _wait_finished(client, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get("/weighting/status")
        assert resp.status_code == 200
        body = resp.json()
        if body["status"] not in ("running", "pending", "missing"):
            return body
        time.sleep(0.05)
    raise AssertionError("weighting did not finish in time")


def test_status_missing_tables(client):
    body = client.get("/weighting/status").json()
    assert body["status"] == "missing"
    assert set(body["missing_tables"]) == {"respondents", "targets"}

    resp = client.post("/weighting/jobs")
    assert resp.status_code == 409
    assert resp.json()["error"] == "missing_table"


def test_not_ready_before_weighting(client):
    assert _upload_roster(client, ROSTER).status_code == 200
    assert _upload_targets(client, TARGETS).status_code == 200
    body = client.get("/weighting/status").json()
    assert body["status"] == "pending"
    assert body["respondent_version"] == 1
    assert body["target_version"] == 1

    resp = client.get("/weighting/weights")
    assert resp.status_code == 409
    assert resp.json()["error"] == "not_ready"


def test_balanced_run_serves_weights_and_diffs(client):
    _upload_roster(client, ROSTER)
    _upload_targets(client, TARGETS)
    start = client.post("/weighting/jobs")
    assert start.status_code == 200
    assert start.json()["status"] == "running"

    final = _wait_finished(client)
    assert final["status"] == "balanced"
    assert final["respondent_version"] == 1
    assert final["target_version"] == 1

    weights = client.get("/weighting/weights").json()
    assert weights["respondent_version"] == 1
    assert weights["target_version"] == 1
    assert weights["total_count"] == 5
    by_id = {r["household_id"]: r["weight"] for r in weights["weights"]}

    # Factor limits 0.5x..2x the initial weight.
    for hh in ROSTER:
        w = by_id[hh["household_id"]]
        assert 0.5 * hh["init_weight"] - 1e-9 <= w <= 2.0 * hh["init_weight"] + 1e-9

    # Group weighted counts match both-dimension targets.
    sums = {"D1": 0.0, "D2": 0.0, "S1": 0.0, "S2": 0.0}
    for hh in ROSTER:
        w = by_id[hh["household_id"]]
        sums[hh["district"]] += w
        sums[hh["size_band"]] += w
    for t in TARGETS:
        assert abs(sums[t["group_name"]] - t["target_count"]) < 1e-5

    diffs = client.get("/weighting/differences").json()
    assert diffs["is_balanced"] is True
    assert len(diffs["differences"]) == 4
    for d in diffs["differences"]:
        assert abs(d["difference"]) < 1e-5


def test_unbalanced_serves_only_differences(client):
    _upload_roster(client, ROSTER)
    # Impossible targets: D1 max weighted count under 2x factor is
    # 2*(1+2+4) = 14, ask for 100; dimension totals also disagree.
    targets = [
        {"dimension": "district", "group_name": "D1", "target_count": 100.0},
        {"dimension": "district", "group_name": "D2", "target_count": 1.0},
        {"dimension": "size_band", "group_name": "S1", "target_count": 100.0},
        {"dimension": "size_band", "group_name": "S2", "target_count": 1.0},
    ]
    _upload_targets(client, targets)
    client.post("/weighting/jobs")
    final = _wait_finished(client)
    assert final["status"] == "unbalanced"
    assert final["objective_gap"] > 0

    resp = client.get("/weighting/weights")
    assert resp.status_code == 409
    assert resp.json()["error"] == "result_unavailable"

    diffs = client.get("/weighting/differences").json()
    assert diffs["status"] == "unbalanced"
    assert diffs["respondent_version"] == 1
    assert diffs["target_version"] == 1
    assert {d["group_name"] for d in diffs["differences"]} == {"D1", "D2", "S1", "S2"}
    # difference = weighted - target
    for d in diffs["differences"]:
        assert abs((d["weighted_count"] - d["target_count"]) - d["difference"]) < 1e-9


def test_missing_group_target_rejected(client):
    _upload_roster(client, ROSTER)
    targets = [
        {"dimension": "district", "group_name": "D1", "target_count": 9.0},
        {"dimension": "district", "group_name": "D2", "target_count": 6.0},
        {"dimension": "size_band", "group_name": "S1", "target_count": 8.0},
        # S2 missing
    ]
    _upload_targets(client, targets)
    resp = client.post("/weighting/jobs")
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "missing_group_targets"
    missing = body["details"]["missing_group_targets"]
    assert {"dimension": "size_band", "group_name": "S2"} in missing


def test_invalid_roster_keeps_previous_table_and_results(client):
    _upload_roster(client, ROSTER)
    _upload_targets(client, TARGETS)
    client.post("/weighting/jobs")
    _wait_finished(client)
    good = client.get("/weighting/weights")
    assert good.status_code == 200
    old_version = good.json()["respondent_version"]

    # Duplicate household id -> rejected, old table stays live.
    bad = ROSTER + [
        {"household_id": "H1", "district": "D1", "size_band": "S1", "init_weight": 1.0}
    ]
    resp = _upload_roster(client, bad)
    assert resp.status_code == 400
    still_good = client.get("/weighting/weights")
    assert still_good.status_code == 200
    assert still_good.json()["respondent_version"] == old_version

    # Non-positive weight -> rejected as well.
    resp2 = _upload_roster(
        client,
        [
            {"household_id": "X1", "district": "D1",
             "size_band": "S1", "init_weight": 0}
        ],
    )
    assert resp2.status_code == 400


def test_successful_replacement_supersedes_old_results(client):
    _upload_roster(client, ROSTER)
    _upload_targets(client, TARGETS)
    client.post("/weighting/jobs")
    _wait_finished(client)
    assert client.get("/weighting/weights").status_code == 200

    new_roster = [
        {"household_id": "N1", "district": "D1", "size_band": "S1", "init_weight": 2.0},
        {"household_id": "N2", "district": "D2", "size_band": "S2", "init_weight": 2.0},
    ]
    resp = _upload_roster(client, new_roster)
    assert resp.status_code == 200
    assert resp.json()["version"] == 2
    assert resp.json()["replaced_version"] == 1

    # Targets achievable by the two-household roster (each weight 2.0).
    new_targets = [
        {"dimension": "district", "group_name": "D1", "target_count": 2.0},
        {"dimension": "district", "group_name": "D2", "target_count": 2.0},
        {"dimension": "size_band", "group_name": "S1", "target_count": 2.0},
        {"dimension": "size_band", "group_name": "S2", "target_count": 2.0},
    ]
    t_resp = _upload_targets(client, new_targets)
    assert t_resp.json()["version"] == 2

    status = client.get("/weighting/status").json()
    assert status["status"] == "pending"
    assert status["respondent_version"] == 2

    # Old weights/differences must stop being served.
    for path in ("/weighting/weights", "/weighting/differences"):
        resp = client.get(path)
        assert resp.status_code == 409
        assert resp.json()["error"] == "not_ready"

    # New pair computes against the new version pair.
    client.post("/weighting/jobs")
    final = _wait_finished(client)
    assert final["respondent_version"] == 2
    body = client.get("/weighting/weights").json()
    assert body["respondent_version"] == 2
    assert body["target_version"] == 2
    assert {r["household_id"] for r in body["weights"]} == {"N1", "N2"}


def test_duplicate_running_job_rejected(client):
    _upload_roster(client, ROSTER)
    _upload_targets(client, TARGETS)
    # Monkeypatch the solver so the job stays running.
    from app import service as service_mod

    original = service_mod.weighting.solve

    def slow_solve(households, target_rows):
        time.sleep(1.0)
        return original(households, target_rows)

    service_mod.weighting.solve = slow_solve
    try:
        assert client.post("/weighting/jobs").status_code == 200
        resp = client.post("/weighting/jobs")
        assert resp.status_code == 409
        assert resp.json()["error"] == "weighting_in_progress"
    finally:
        service_mod.weighting.solve = original
    _wait_finished(client)


def test_csv_upload_roundtrip(client):
    roster_csv = (
        "household_id,district,size_band,init_weight\n"
        "H1,D1,S1,1\nH2,D1,S2,2\nH3,D2,S1,1\nH4,D2,S2,3\nH5,D1,S1,4\n"
    ).encode()
    target_csv = (
        "dimension,group_name,target_count\n"
        "district,D1,9\ndistrict,D2,6\nsize_band,S1,8\nsize_band,S2,7\n"
    ).encode()
    r1 = client.post(
        "/tables/respondents/csv",
        files={"file": ("roster.csv", roster_csv, "text/csv")},
    )
    assert r1.status_code == 200
    assert r1.json()["row_count"] == 5
    r2 = client.post(
        "/tables/targets/csv",
        files={"file": ("targets.csv", target_csv, "text/csv")},
    )
    assert r2.status_code == 200
    client.post("/weighting/jobs")
    final = _wait_finished(client)
    assert final["status"] == "balanced"


def test_bad_csv_rejected(client):
    bad_csv = b"household_id,district,size_band,init_weight\nH1,D1,S1,-3\n"
    resp = client.post(
        "/tables/respondents/csv",
        files={"file": ("bad.csv", bad_csv, "text/csv")},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_payload"


def test_version_endpoint_and_pagination(client):
    _upload_roster(client, ROSTER)
    _upload_targets(client, TARGETS)
    versions = client.get("/tables/versions").json()
    assert versions["respondents"]["version"] == 1
    assert versions["targets"]["version"] == 1
    assert "content_hash" in versions["respondents"]

    client.post("/weighting/jobs")
    _wait_finished(client)
    page1 = client.get("/weighting/weights?limit=2&offset=0").json()
    assert len(page1["weights"]) == 2
    assert page1["total_count"] == 5
    page2 = client.get("/weighting/weights?limit=2&offset=2").json()
    assert len(page2["weights"]) == 2
    ids1 = {r["household_id"] for r in page1["weights"]}
    ids2 = {r["household_id"] for r in page2["weights"]}
    assert ids1.isdisjoint(ids2)
