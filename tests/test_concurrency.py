"""Concurrency: replacement while a weighting job is still running."""

import threading
import time

from tests.test_api import TARGETS, ROSTER, _upload_roster, _upload_targets, _wait_finished


def test_replacement_during_run_discards_stale_job(client, monkeypatch):
    from app import service as service_mod

    _upload_roster(client, ROSTER)
    _upload_targets(client, TARGETS)

    entered = threading.Event()
    release = threading.Event()
    original = service_mod.weighting.solve

    def blocking_solve(households, target_rows):
        entered.set()
        release.wait(timeout=10)
        return original(households, target_rows)

    monkeypatch.setattr(service_mod.weighting, "solve", blocking_solve)
    assert client.post("/weighting/jobs").status_code == 200
    assert entered.wait(timeout=5)

    # Replacement is allowed while the job is running.
    new_roster = [dict(r, init_weight=r["init_weight"] + 0.5) for r in ROSTER]
    resp = _upload_roster(client, new_roster)
    assert resp.status_code == 200
    assert resp.json()["version"] == 2

    # Old pair immediately goes back to "pending"; no stale weights served.
    status = client.get("/weighting/status").json()
    assert status["status"] == "pending"
    assert status["respondent_version"] == 2
    assert client.get("/weighting/weights").status_code == 409

    monkeypatch.undo()
    release.set()

    # Let the stale worker finish; it must not publish results for v1.
    time.sleep(0.3)
    status = client.get("/weighting/status").json()
    assert status["status"] == "pending"
    assert client.get("/weighting/weights").status_code == 409

    # Re-running against the current pair works and carries v2.
    assert client.post("/weighting/jobs").status_code == 200
    final = _wait_finished(client)
    assert final["status"] == "balanced"
    assert final["respondent_version"] == 2
    weights = client.get("/weighting/weights").json()
    assert weights["respondent_version"] == 2
    assert weights["target_version"] == 1


def test_failed_target_upload_preserves_live_data(client):
    _upload_roster(client, ROSTER)
    _upload_targets(client, TARGETS)
    client.post("/weighting/jobs")
    _wait_finished(client)
    before = client.get("/weighting/differences").json()

    bad = [
        {"dimension": "mystery", "group_name": "X", "target_count": 1.0},
    ]
    resp = _upload_targets(client, bad)
    assert resp.status_code == 400
    after = client.get("/weighting/differences").json()
    assert after["target_version"] == before["target_version"] == 1
    assert len(after["differences"]) == len(before["differences"])
