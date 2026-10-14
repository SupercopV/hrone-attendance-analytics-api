"""Tests for Stage 3: Manual Attendance Corrections (PATCH /attendance/{emp_code}/{date})."""
import pytest
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
import mongomock

import app.main
from app.main import app, IST


@pytest.fixture
def mock_db(monkeypatch):
    mock_client = mongomock.MongoClient()
    test_db = mock_client["test_attendance_db"]

    test_db.employees.create_index([("emp_code", 1)], unique=True)
    test_db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    test_db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])

    monkeypatch.setattr("app.main.client", mock_client)
    monkeypatch.setattr("app.main.db", test_db)
    return test_db


@pytest.fixture
def client(mock_db):
    return TestClient(app)


@pytest.fixture
def seeded_client(client):
    # Create employee EMP0001 (09:30 - 18:30)
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-05",
        },
    )
    # Create employee EMP0005 (Overnight 22:00 - 06:00)
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0005",
            "name": "Overnight Guard",
            "email": "guard@example.com",
            "department": "Security",
            "shift_start": "22:00",
            "shift_end": "06:00",
            "joined_on": "2026-01-05",
        },
    )
    return client


def test_missing_employee_or_record(seeded_client):
    # Unknown employee
    res1 = seeded_client.patch(
        "/attendance/EMP9999/2026-07-06",
        json={"reason": "Correction", "regularized_by": "admin"},
    )
    assert res1.status_code == 404

    # Existing employee but unknown date record
    res2 = seeded_client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={"reason": "Correction", "regularized_by": "admin"},
    )
    assert res2.status_code == 404


def test_regularize_success_sample8_scenario(seeded_client):
    # Setup initial record on 2026-07-07:
    # Punched in at 10:05 IST (35 min late), out at 18:30 IST
    in_dt = datetime(2026, 7, 7, 10, 5, 0, tzinfo=IST)
    out_dt = datetime(2026, 7, 7, 18, 30, 0, tzinfo=IST)
    in_ms = int(in_dt.timestamp() * 1000)
    out_ms = int(out_dt.timestamp() * 1000)

    seeded_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": in_ms})
    seeded_client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": out_ms})

    # HR regularizes punch_in from 10:05 IST to 09:28 IST (Sample 8 in DATA_MODEL.md)
    new_in_dt = datetime(2026, 7, 7, 9, 28, 0, tzinfo=IST)
    new_in_ms = int(new_in_dt.timestamp() * 1000)

    res = seeded_client.patch(
        "/attendance/EMP0001/2026-07-07",
        json={
            "punch_in": new_in_ms,
            "reason": "biometric glitch",
            "regularized_by": "hr.admin",
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["punch_in"] == new_in_ms
    assert data["late_minutes"] == 0  # 09:28 is inside grace
    assert data["work_hours"] == 9.03  # 09:28 to 18:30 is 9h 2m = 9.03h
    assert "_id" not in data
    assert "id" not in data

    # History audit check
    assert len(data["history"]) == 1
    h = data["history"][0]
    assert h["by"] == "hr.admin"
    assert h["reason"] == "biometric glitch"
    assert isinstance(h["at"], int)
    changes = h["changes"]
    assert "punch_in" in changes
    assert changes["punch_in"]["from"] == in_ms
    assert changes["punch_in"]["to"] == new_in_ms
    assert changes["late_minutes"]["from"] == 35
    assert changes["late_minutes"]["to"] == 0
    assert "work_hours" in changes


def test_absent_leave_clears_punches_and_rejects_punch_input(seeded_client):
    # Setup initial present record
    in_dt = datetime(2026, 7, 6, 9, 30, 0, tzinfo=IST)
    out_dt = datetime(2026, 7, 6, 18, 30, 0, tzinfo=IST)
    seeded_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": int(in_dt.timestamp() * 1000)})
    seeded_client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": int(out_dt.timestamp() * 1000)})

    # Supplying punch times when setting ABSENT -> 422
    bad_res = seeded_client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={
            "status": "ABSENT",
            "punch_in": int(in_dt.timestamp() * 1000),
            "reason": "Marking absent",
            "regularized_by": "hr.admin",
        },
    )
    assert bad_res.status_code == 422

    # Setting ABSENT clears punch times and derived fields
    good_res = seeded_client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={
            "status": "ABSENT",
            "reason": "Was not in office",
            "regularized_by": "hr.admin",
        },
    )
    assert good_res.status_code == 200
    rec = good_res.json()
    assert rec["status"] == "ABSENT"
    assert rec["punch_in"] is None
    assert rec["punch_out"] is None
    assert rec["work_hours"] is None
    assert rec["late_minutes"] == 0
    assert rec["overtime_minutes"] == 0
    assert rec["half_day"] is False
    assert len(rec["history"]) == 1


def test_presence_status_requires_punch_in(seeded_client, mock_db):
    # Seed an ABSENT record directly (no punch times)
    mock_db.attendance_logs.insert_one({
        "emp_code": "EMP0001",
        "date": "2026-07-08",
        "status": "ABSENT",
        "punch_in": None,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    })

    # Regularize to PRESENT without providing punch_in -> 422
    res = seeded_client.patch(
        "/attendance/EMP0001/2026-07-08",
        json={
            "status": "PRESENT",
            "reason": "Forgot to punch",
            "regularized_by": "manager",
        },
    )
    assert res.status_code == 422


def test_punch_in_date_mismatch_rejected(seeded_client):
    in_dt = datetime(2026, 7, 6, 9, 30, 0, tzinfo=IST)
    seeded_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": int(in_dt.timestamp() * 1000)})

    # Try to set punch_in to next day 2026-07-07 on 2026-07-06 record -> 422
    next_day_in = datetime(2026, 7, 7, 9, 30, 0, tzinfo=IST)
    res = seeded_client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={
            "punch_in": int(next_day_in.timestamp() * 1000),
            "reason": "Wrong day update",
            "regularized_by": "manager",
        },
    )
    assert res.status_code == 422


def test_punch_out_constraints_on_regularize(seeded_client):
    in_dt = datetime(2026, 7, 6, 9, 30, 0, tzinfo=IST)
    in_ms = int(in_dt.timestamp() * 1000)
    seeded_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": in_ms})

    # punch_out earlier than punch_in -> 422
    res_early = seeded_client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={
            "punch_out": in_ms - 1000,
            "reason": "Early out",
            "regularized_by": "manager",
        },
    )
    assert res_early.status_code == 422

    # punch_out > 24 hours after punch_in -> 422
    res_late = seeded_client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={
            "punch_out": in_ms + 25 * 3600 * 1000,
            "reason": "Too late out",
            "regularized_by": "manager",
        },
    )
    assert res_late.status_code == 422


def test_no_op_regularize_returns_422(seeded_client):
    in_dt = datetime(2026, 7, 6, 9, 30, 0, tzinfo=IST)
    in_ms = int(in_dt.timestamp() * 1000)
    seeded_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": in_ms})

    # Sending exact same status with no changes -> 422
    res = seeded_client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={
            "status": "PRESENT",
            "reason": "No actual change",
            "regularized_by": "manager",
        },
    )
    assert res.status_code == 422


def test_audit_history_preservation_across_multiple_corrections(seeded_client):
    in_dt = datetime(2026, 7, 6, 9, 30, 0, tzinfo=IST)
    in_ms = int(in_dt.timestamp() * 1000)
    seeded_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": in_ms})

    # First correction: add punch_out
    out1_ms = int((in_dt + timedelta(hours=8)).timestamp() * 1000)
    res1 = seeded_client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={"punch_out": out1_ms, "reason": "First correction", "regularized_by": "admin1"},
    )
    assert res1.status_code == 200
    assert len(res1.json()["history"]) == 1

    # Second correction: change status to WFH
    res2 = seeded_client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={"status": "WFH", "reason": "Second correction", "regularized_by": "admin2"},
    )
    assert res2.status_code == 200
    history = res2.json()["history"]
    assert len(history) == 2
    assert history[0]["by"] == "admin1"
    assert history[1]["by"] == "admin2"


def test_optimistic_concurrency_race(seeded_client, monkeypatch):
    import threading

    in_dt = datetime(2026, 7, 6, 9, 30, 0, tzinfo=IST)
    in_ms = int(in_dt.timestamp() * 1000)
    seeded_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": in_ms})

    out1_ms = int((in_dt + timedelta(hours=8)).timestamp() * 1000)
    out2_ms = int((in_dt + timedelta(hours=9)).timestamp() * 1000)

    # Use a barrier to force a race condition: both threads read the record at history len 0
    import app.main as app_main
    barrier = threading.Barrier(2)
    orig_find_one = app_main.db.attendance_logs.find_one

    def barrier_find_one(*args, **kwargs):
        res = orig_find_one(*args, **kwargs)
        if kwargs.get("filter", {}).get("date") == "2026-07-06" or (args and isinstance(args[0], dict) and args[0].get("date") == "2026-07-06"):
            try:
                barrier.wait(timeout=2.0)
            except Exception:
                pass
        return res

    monkeypatch.setattr(app_main.db.attendance_logs, "find_one", barrier_find_one)

    results = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(
            seeded_client.patch,
            "/attendance/EMP0001/2026-07-06",
            json={"punch_out": out1_ms, "reason": "Manager 1 edit", "regularized_by": "manager1"},
        )
        f2 = executor.submit(
            seeded_client.patch,
            "/attendance/EMP0001/2026-07-06",
            json={"punch_out": out2_ms, "reason": "Manager 2 edit", "regularized_by": "manager2"},
        )
        results = [f1.result().status_code, f2.result().status_code]

    # Exactly one must succeed (200) and the loser gets 409
    assert sorted(results) == [200, 409]

