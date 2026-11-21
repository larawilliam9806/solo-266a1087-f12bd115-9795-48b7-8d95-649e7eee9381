"""Verification of legacy tables and CSV header hardening.

The legacy-tables scenario is simulated by clearing the ``verified`` flag
that path-uploaded snapshots carry; that is exactly the state an older
database is in after the schema migration.
"""

import sqlite3
import threading

from tests.test_api import (
    ROSTER,
    TARGETS,
    _upload_roster,
    _upload_targets,
    _wait_finished,
)


def _clear_verification(service):
    with sqlite3.connect(service._db_path) as conn:
        conn.execute("UPDATE respondent_versions SET verified = 0")
        conn.execute("UPDATE target_versions SET verified = 0")
        conn.commit()


def _status(client):
    return client.get("/weighting/status").json()


def test_csv_duplicate_required_column_rejected(client):
    roster_csv = (
        "household_id,district,size_band,size_band,init_weight\n"
        "H1,D1,S1,S2,1\n"
    ).encode()
    resp = client.post(
        "/tables/respondents/csv",
        files={"file": ("dup.csv", roster_csv, "text/csv")},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "invalid_payload"
    assert body["details"]["duplicate_columns"] == ["size_band"]
    assert "size_band" in body["message"]

    # Nothing was written and the live table is still absent.
    versions = client.get("/tables/versions").json()
    assert versions["respondents"] is None


def test_csv_duplicate_required_column_with_whitespace_rejected(client):
    target_csv = (
        " dimension , group_name , target_count , target_count \n"
        "district,D1,8,9\n"
    ).encode()
    resp = client.post(
        "/tables/targets/csv",
        files={"file": ("dup.csv", target_csv, "text/csv")},
    )
    assert resp.status_code == 400
    assert resp.json()["details"]["duplicate_columns"] == ["target_count"]


def test_duplicate_column_upload_keeps_live_table_and_results(client):
    _upload_roster(client, ROSTER)
    _upload_targets(client, TARGETS)
    client.post("/weighting/jobs")
    _wait_finished(client)
    before = client.get("/weighting/weights").json()

    bad_csv = (
        "household_id,district,district,size_band,init_weight\n"
        "X1,D1,D2,S1,1\n"
    ).encode()
    resp = client.post(
        "/tables/respondents/csv",
        files={"file": ("dup.csv", bad_csv, "text/csv")},
    )
    assert resp.status_code == 400

    # Pointer, versions and served results all unchanged.
    versions = client.get("/tables/versions").json()
    assert versions["respondents"]["version"] == before["respondent_version"]
    after = client.get("/weighting/weights").json()
    assert after["respondent_version"] == before["respondent_version"]
    assert after["target_version"] == before["target_version"]
    assert after["weights"] == before["weights"]


def test_legacy_tables_require_verification_before_use(client, service):
    _upload_roster(client, ROSTER)
    _upload_targets(client, TARGETS)
    _clear_verification(service)

    status = _status(client)
    assert status["status"] == "unverified"
    assert {
        (p["table"], p["version"]) for p in status["pending_verification"]
    } == {("respondents", 1), ("targets", 1)}

    job = client.post("/weighting/jobs")
    assert job.status_code == 409
    assert job.json()["error"] == "unverified_table"
    assert set(job.json()["details"]["unverified_tables"]) == {
        "respondents",
        "targets",
    }

    for path in ("/weighting/weights", "/weighting/differences"):
        resp = client.get(path)
        assert resp.status_code == 409
        assert resp.json()["error"] == "unverified_table"


def test_verification_is_per_table_and_independent(client, service):
    _upload_roster(client, ROSTER)
    _upload_targets(client, TARGETS)
    _clear_verification(service)

    # Confirm respondents only; targets remain pending.
    ok = client.post(
        "/tables/respondents/verify?version=1", json={"records": ROSTER}
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["verified"] is True
    assert body["ready_for_weighting"] is False
    assert [p["table"] for p in body["pending_verification"]] == ["targets"]

    # Still gated on the unverified target table.
    assert _status(client)["status"] == "unverified"
    assert client.post("/weighting/jobs").json()["error"] == "unverified_table"

    # Replacing the (still unverified) target table does not disturb the
    # respondent verification already granted.
    new_targets = [dict(t, target_count=t["target_count"] + 0.5) for t in TARGETS]
    assert _upload_targets(client, new_targets).json()["version"] == 2
    versions = client.get("/tables/versions").json()
    assert versions["respondents"]["verified"] is True
    assert versions["targets"]["verified"] is True  # new uploads are usable
    assert [p["table"] for p in versions["pending_verification"]] == []


def test_verification_mismatch_reports_reason_and_keeps_state(client, service):
    _upload_roster(client, ROSTER)
    _upload_targets(client, TARGETS)
    _clear_verification(service)

    wrong = [dict(ROSTER[0], init_weight=ROSTER[0]["init_weight"] + 1)]
    wrong += ROSTER[1:]
    wrong = wrong[:-1]  # also drop one row

    resp = client.post(
        "/tables/respondents/verify?version=1", json={"records": wrong}
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "verification_mismatch"
    details = body["details"]
    assert details["mismatch_count"] >= 2
    assert details["missing_keys"] == [{"household_id": "H5"}]
    assert details["field_mismatches"][0]["field"] == "init_weight"
    assert "不一致" in body["message"]
    assert details["kept_verified"] is False

    # The rejection left the table pending, not verified.
    assert _status(client)["status"] == "unverified"

    # A second, correct submission now succeeds.
    ok = client.post(
        "/tables/respondents/verify?version=1",
        json={"records": list(reversed(ROSTER))},
    )
    assert ok.status_code == 200
    assert ok.json()["verified"] is True


def test_failed_verification_preserves_previous_verified_state(client, service):
    _upload_roster(client, ROSTER)
    assert client.post(
        "/tables/respondents/verify?version=1", json={"records": ROSTER}
    ).status_code == 200

    resp = client.post(
        "/tables/respondents/verify?version=1",
        json={"records": ROSTER[:2]},
    )
    assert resp.status_code == 409
    assert resp.json()["details"]["kept_verified"] is True
    versions = client.get("/tables/versions").json()
    assert versions["respondents"]["verified"] is True


def test_verification_of_superseded_version_rejected(client, service):
    _upload_roster(client, ROSTER)
    _clear_verification(service)
    # Replace the table; version 1 is no longer current.
    _upload_roster(client, [dict(r, init_weight=1.5) for r in ROSTER])

    resp = client.post(
        "/tables/respondents/verify?version=1", json={"records": ROSTER}
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "version_superseded"
    assert body["details"]["current_version"] == 2

    # The superseded version stays unverified.
    with sqlite3.connect(service._db_path) as conn:
        row = conn.execute(
            "SELECT verified FROM respondent_versions WHERE version = 1"
        ).fetchone()
    assert row[0] == 0


def test_verification_of_unknown_version_rejected(client):
    _upload_roster(client, ROSTER)
    resp = client.post(
        "/tables/respondents/verify?version=99", json={"records": ROSTER}
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "version_not_found"


def test_verification_accepts_csv_and_json_variants(client, service):
    _upload_roster(client, ROSTER)
    _upload_targets(client, TARGETS)
    _clear_verification(service)

    roster_csv = (
        "household_id,district,size_band,init_weight\n"
        "H5,D1,S1,4\nH4,D2,S2,3\nH3,D2,S1,1\nH2,D1,S2,2\nH1,D1,S1,1\n"
    ).encode()
    r1 = client.post(
        "/tables/respondents/verify/csv?version=1",
        files={"file": ("roster.csv", roster_csv, "text/csv")},
    )
    assert r1.status_code == 200

    target_csv = (
        "dimension,group_name,target_count\n"
        "size_band,S2,7\nsize_band,S1,8\n"
        "district,D2,6\ndistrict,D1,9\n"
    ).encode()
    r2 = client.post(
        "/tables/targets/verify/csv",
        data={"version": "1"},
        files={"file": ("targets.csv", target_csv, "text/csv")},
    )
    assert r2.status_code == 200
    assert r2.json()["ready_for_weighting"] is True

    final = client.post("/weighting/jobs")
    assert final.status_code == 200
    assert _wait_finished(client)["status"] == "balanced"
    assert client.get("/weighting/weights").status_code == 200


def test_legacy_results_resume_after_both_verified(client, service):
    _upload_roster(client, ROSTER)
    _upload_targets(client, TARGETS)
    client.post("/weighting/jobs")
    finished = _wait_finished(client)
    assert finished["status"] == "balanced"
    job_id = finished["job_id"]

    # Legacy DB: results exist but tables await confirmation.
    _clear_verification(service)
    assert client.get("/weighting/weights").status_code == 409
    assert client.post("/weighting/jobs").json()["error"] == "unverified_table"

    for table, records in (
        ("respondents", ROSTER),
        ("targets", TARGETS),
    ):
        resp = client.post(
            f"/tables/{table}/verify?version=1", json={"records": records}
        )
        assert resp.status_code == 200

    # Same version pair: the original job is restored, no re-run needed.
    weights = client.get("/weighting/weights")
    assert weights.status_code == 200
    assert weights.json()["job_id"] == job_id
    assert weights.json()["respondent_version"] == 1
    assert weights.json()["target_version"] == 1
    assert _status(client)["status"] == "balanced"


def test_reuploaded_version_is_immediately_usable(client, service):
    _upload_roster(client, ROSTER)
    _upload_targets(client, TARGETS)
    _clear_verification(service)
    assert _status(client)["status"] == "unverified"

    # A fresh replacement upload is a corrected submission: no manual check.
    resp = _upload_roster(client, ROSTER)
    assert resp.status_code == 200
    assert resp.json()["version"] == 2
    status = _status(client)
    # The respondent side is now usable; the old target version still waits.
    assert status["status"] == "unverified"
    assert [p["table"] for p in status["pending_verification"]] == ["targets"]

    assert _upload_targets(client, TARGETS).status_code == 200
    assert _status(client)["status"] == "pending"
    assert client.post("/weighting/jobs").status_code == 200
    assert _wait_finished(client)["status"] == "balanced"


def test_verification_during_concurrent_replacement(client, service, monkeypatch):
    """A replacement of the other table never waits for a verification."""
    from app import service as service_mod

    _upload_roster(client, ROSTER)
    _upload_targets(client, TARGETS)
    _clear_verification(service)

    entered = service_mod.WeightingService._compare_snapshot
    release = threading.Event()
    started = threading.Event()

    def slow_compare(self, table, stored, submitted):
        started.set()
        release.wait(timeout=10)
        return entered(self, table, stored, submitted)

    monkeypatch.setattr(
        service_mod.WeightingService, "_compare_snapshot", slow_compare
    )

    result = {}

    def verify():
        result["resp"] = client.post(
            "/tables/respondents/verify?version=1", json={"records": ROSTER}
        )

    worker = threading.Thread(target=verify)
    worker.start()
    assert started.wait(timeout=5)

    # The target table is replaced while the respondent check is blocked.
    t_resp = _upload_targets(client, TARGETS)
    assert t_resp.status_code == 200
    assert t_resp.json()["version"] == 2

    release.set()
    worker.join(timeout=10)
    assert result["resp"].status_code == 200
    assert result["resp"].json()["verified"] is True

    # Target v2 (fresh upload) is usable; only it needed no manual check.
    versions = client.get("/tables/versions").json()
    assert versions["respondents"]["verified"] is True
    assert versions["targets"]["version"] == 2
    assert versions["targets"]["verified"] is True


def test_status_lists_pending_tables(client, service):
    _upload_roster(client, ROSTER)
    _upload_targets(client, TARGETS)
    _clear_verification(service)

    body = client.get("/tables/verifications").json()
    tables = {t["table"]: t for t in body["tables"]}
    assert tables["respondents"]["verified"] is False
    assert tables["targets"]["verified"] is False
    assert body["ready_for_weighting"] is False
    assert {p["table"] for p in body["pending_verification"]} == {
        "respondents",
        "targets",
    }


def test_interrupted_job_recovered_on_startup(client, service):
    from app.database import db_session
    from app.service import WeightingService

    _upload_roster(client, ROSTER)
    _upload_targets(client, TARGETS)
    client.post("/weighting/jobs")
    finished = _wait_finished(client)
    assert finished["status"] == "balanced"

    with db_session(service._db_path) as conn:
        conn.execute(
            "INSERT INTO weighting_jobs "
            "(respondent_version, target_version, status, created_at) "
            "VALUES (1, 1, 'running', '2020-01-01T00:00:00+00:00')"
        )
        conn.commit()

    restarted = WeightingService(db_path=service._db_path)
    from app.main import create_app
    from fastapi.testclient import TestClient

    client2 = TestClient(create_app(service=restarted))
    status = client2.get("/weighting/status").json()
    assert status["status"] == "failed"
    assert "中断" in status["error_reason"]
    assert client2.post("/weighting/jobs").status_code == 200
    assert _wait_finished(client2)["status"] == "balanced"
