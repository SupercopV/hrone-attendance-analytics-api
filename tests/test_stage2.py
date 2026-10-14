"""Tests for Stage 2: CRUD, Rules R1-R5, Pagination, and Concurrency."""
import pytest
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock
from fastapi.testclient import TestClient
import mongomock

import app.main
from app.main import app, IST


@pytest.fixture
def mock_db(monkeypatch):
    """Provides a fresh isolated mongomock client and database."""
    mock_client = mongomock.MongoClient()
    test_db = mock_client["test_attendance_db"]

    # Re-create unique indexes on mongomock
    test_db.employees.create_index([("emp_code", 1)], unique=True)
    test_db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    test_db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    test_db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])

    monkeypatch.setattr("app.main.client", mock_client)
    monkeypatch.setattr("app.main.db", test_db)
    return test_db


@pytest.fixture
def client(mock_db):
    return TestClient(app)


# 1. Health check tests
def test_health_ok(client, mock_db, monkeypatch):
    # Mock ping command to succeed
    mock_db.client.admin.command = MagicMock(return_value={"ok": 1.0})
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_unavailable(client, mock_db, monkeypatch):
    # Mock ping command to raise exception
    def fail_ping(*args, **kwargs):
        raise Exception("Connection refused")

    mock_db.client.admin.command = fail_ping
    response = client.get("/health")
    assert response.status_code == 503
    assert "Database unavailable" in response.json()["detail"]


# 2. Employee creation and listing tests
def test_create_employee_success_and_duplicate(client):
    payload = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }
    # Create employee
    res1 = client.post("/employees", json=payload)
    assert res1.status_code == 201
    data = res1.json()
    assert data["emp_code"] == "EMP0001"
    assert "_id" not in data
    assert "id" not in data
    assert isinstance(data["created_at"], int)
    assert data["created_at"] >= 100_000_000_000

    # Duplicate emp_code must return 409
    res2 = client.post("/employees", json=payload)
    assert res2.status_code == 409


def test_employee_pagination_and_sorting(client):
    # Insert multiple employees
    for i in [3, 1, 2, 5, 4]:
        client.post(
            "/employees",
            json={
                "emp_code": f"EMP000{i}",
                "name": f"Employee {i}",
                "email": f"emp{i}@example.com",
                "department": "Engineering" if i <= 3 else "HR",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-01-05",
            },
        )

    # 1-based pagination & sorted by emp_code asc
    res = client.get("/employees?page=1&page_size=3")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 5
    assert data["page"] == 1
    assert data["page_size"] == 3
    items = data["items"]
    assert len(items) == 3
    assert [item["emp_code"] for item in items] == ["EMP0001", "EMP0002", "EMP0003"]

    # Filter by department
    res_hr = client.get("/employees?department=HR")
    assert res_hr.status_code == 200
    data_hr = res_hr.json()
    assert data_hr["total"] == 2
    assert [item["emp_code"] for item in data_hr["items"]] == ["EMP0004", "EMP0005"]


# 3. Attendance Punch-in tests
def test_punch_in_unknown_employee(client):
    res = client.post("/attendance/punch-in", json={"emp_code": "EMP9999"})
    assert res.status_code == 404


def test_punch_in_success_and_duplicate(client):
    # Create employee
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

    # Punch in 09:40:01 IST (late by 10 min) on 2026-07-06
    # 09:40:01 IST = 04:10:01 UTC
    dt = datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST)
    ms = int(dt.timestamp() * 1000)

    res1 = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms})
    assert res1.status_code == 201
    rec = res1.json()
    assert rec["emp_code"] == "EMP0001"
    assert rec["date"] == "2026-07-06"
    assert rec["status"] == "PRESENT"
    assert rec["punch_in"] == ms
    assert rec["punch_out"] is None
    assert rec["late_minutes"] == 10
    assert rec["work_hours"] is None
    assert rec["overtime_minutes"] == 0
    assert rec["half_day"] is False
    assert "_id" not in rec
    assert "id" not in rec

    # Duplicate punch in for same date must return 409
    res2 = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms + 60000})
    assert res2.status_code == 409


def test_punch_in_overnight_shift(client):
    # Create employee with overnight shift 22:00 - 06:00
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0005",
            "name": "Overnight Worker",
            "email": "overnight@example.com",
            "department": "Security",
            "shift_start": "22:00",
            "shift_end": "06:00",
            "joined_on": "2026-01-05",
        },
    )

    # Punch in at 00:30 IST on 2026-07-07
    # Since 00:30 < shift_end (06:00), belongs to shift starting 2026-07-06!
    dt = datetime(2026, 7, 7, 0, 30, 0, tzinfo=IST)
    ms = int(dt.timestamp() * 1000)

    res = client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms})
    assert res.status_code == 201
    rec = res.json()
    assert rec["date"] == "2026-07-06"
    # Late minutes from 22:00 to 00:30 is 150 minutes
    assert rec["late_minutes"] == 150


# 4. Attendance Punch-out tests
def test_punch_out_without_punch_in(client):
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
    # No punch in exists
    res = client.post("/attendance/punch-out", json={"emp_code": "EMP0001"})
    assert res.status_code == 404


def test_punch_out_success_and_calculations(client):
    # Create employee
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

    # Sample 1: in at 09:28 IST (03:58 UTC), out at 18:35 IST (13:05 UTC)
    in_dt = datetime(2026, 7, 6, 9, 28, 0, tzinfo=IST)
    out_dt = datetime(2026, 7, 6, 18, 35, 0, tzinfo=IST)
    in_ms = int(in_dt.timestamp() * 1000)
    out_ms = int(out_dt.timestamp() * 1000)

    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": in_ms})

    res = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": out_ms})
    assert res.status_code == 200
    rec = res.json()
    assert rec["work_hours"] == 9.12
    assert rec["overtime_minutes"] == 0  # 5 min is < 30 min threshold
    assert rec["half_day"] is False
    assert rec["punch_out"] == out_ms

    # Second punch out for the same record must return 409
    res_dup = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": out_ms + 1000})
    assert res_dup.status_code == 409


def test_punch_out_validation_constraints(client):
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
    in_dt = datetime(2026, 7, 6, 9, 30, 0, tzinfo=IST)
    in_ms = int(in_dt.timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": in_ms})

    # Punch out earlier than punch in -> 422
    res_early = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": in_ms - 1000})
    assert res_early.status_code == 422

    # Punch out > 24 hours after punch in -> 422
    res_late = client.post(
        "/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": in_ms + 25 * 3600 * 1000}
    )
    assert res_late.status_code == 422


# 5. Attendance listing tests
def test_list_attendance(client):
    # Add employee
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
    # Add punch-in for 2026-07-06
    in_ms = int(datetime(2026, 7, 6, 9, 30, 0, tzinfo=IST).timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": in_ms})

    # List records
    res = client.get("/attendance?emp_code=EMP0001")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 1
    assert data["items"][0]["emp_code"] == "EMP0001"
    assert "_id" not in data["items"][0]

    # date_from > date_to -> 422
    res_bad_dates = client.get("/attendance?date_from=2026-07-10&date_to=2026-07-01")
    assert res_bad_dates.status_code == 422


# 6. Concurrency tests (Punch-in and Punch-out races)
def test_concurrent_punch_in_race(client):
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
    in_ms = int(datetime(2026, 7, 6, 9, 30, 0, tzinfo=IST).timestamp() * 1000)

    # Simulate 5 concurrent punch-in requests for the same employee and date
    results = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [
            executor.submit(client.post, "/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": in_ms})
            for _ in range(5)
        ]
        results = [f.result().status_code for f in futures]

    # Exactly one request gets 201, rest get 409
    assert results.count(201) == 1
    assert results.count(409) == 4


def test_concurrent_punch_out_race(client):
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
    in_ms = int(datetime(2026, 7, 6, 9, 30, 0, tzinfo=IST).timestamp() * 1000)
    out_ms = int(datetime(2026, 7, 6, 18, 30, 0, tzinfo=IST).timestamp() * 1000)

    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": in_ms})

    # Simulate 5 concurrent punch-out requests
    results = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [
            executor.submit(client.post, "/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": out_ms})
            for _ in range(5)
        ]
        results = [f.result().status_code for f in futures]

    # Exactly one request gets 200, rest get 409
    assert results.count(200) == 1
    assert results.count(409) == 4

