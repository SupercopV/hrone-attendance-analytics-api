"""Tests for Stage 4: Analytics Aggregations."""
import pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
import mongomock

import app.main
from app.main import app, IST


@pytest.fixture
def mock_db(monkeypatch):
    mock_client = mongomock.MongoClient()
    test_db = mock_client["test_attendance_db"]

    test_db.employees.create_index([("emp_code", 1)], unique=True)
    test_db.employees.create_index([("department", 1), ("joined_on", 1)])
    test_db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)

    monkeypatch.setattr("app.main.client", mock_client)
    monkeypatch.setattr("app.main.db", test_db)
    return test_db


@pytest.fixture
def client(mock_db):
    return TestClient(app)


# --------------------------------------------------------------------------- #
# 1. Employee Monthly Analytics Tests
# --------------------------------------------------------------------------- #
def test_employee_monthly_zero_records(client):
    # Employee joined 2026-07-01, July 2026 has 23 weekdays
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "joined_on": "2026-07-01",
        },
    )
    res = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 23  # July 2026 has 23 Mon-Fri
    assert data["present_days"] == 0.0
    assert data["leave_days"] == 0
    assert data["late_count"] == 0
    assert data["total_late_minutes"] == 0
    assert data["total_overtime_minutes"] == 0
    assert data["attendance_pct"] == 0.0


def test_employee_monthly_join_dates(client):
    # Mid-month joiner: joined 2026-07-20 (Monday)
    # Weekdays from July 20 to July 31 = 10 weekdays
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0002",
            "name": "Mid Month",
            "email": "mid@example.com",
            "department": "HR",
            "joined_on": "2026-07-20",
        },
    )
    res_mid = client.get("/analytics/employees/EMP0002/monthly?month=2026-07")
    assert res_mid.status_code == 200
    assert res_mid.json()["working_days"] == 10

    # Joined after month end: joined 2026-08-01
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0003",
            "name": "Future Joiner",
            "email": "future@example.com",
            "department": "HR",
            "joined_on": "2026-08-01",
        },
    )
    res_future = client.get("/analytics/employees/EMP0003/monthly?month=2026-07")
    assert res_future.status_code == 200
    data_fut = res_future.json()
    assert data_fut["working_days"] == 0
    assert data_fut["attendance_pct"] is None


def test_employee_monthly_weekday_vs_weekend_and_half_day(client, mock_db):
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "joined_on": "2026-07-01",
        },
    )
    # Mon 2026-07-06: Full day PRESENT -> +1.0
    mock_db.attendance_logs.insert_one({
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "half_day": False,
        "late_minutes": 15,
        "overtime_minutes": 35,
    })
    # Tue 2026-07-07: Half day WFH -> +0.5
    mock_db.attendance_logs.insert_one({
        "emp_code": "EMP0001",
        "date": "2026-07-07",
        "status": "WFH",
        "half_day": True,
        "late_minutes": 0,
        "overtime_minutes": 0,
    })
    # Wed 2026-07-08: LEAVE -> +1 leave_day
    mock_db.attendance_logs.insert_one({
        "emp_code": "EMP0001",
        "date": "2026-07-08",
        "status": "LEAVE",
        "half_day": False,
        "late_minutes": 0,
        "overtime_minutes": 0,
    })
    # Sat 2026-07-11 (Weekend!): PRESENT -> does NOT add to present_days, but adds to late & overtime!
    mock_db.attendance_logs.insert_one({
        "emp_code": "EMP0001",
        "date": "2026-07-11",
        "status": "PRESENT",
        "half_day": False,
        "late_minutes": 20,
        "overtime_minutes": 40,
    })

    res = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["present_days"] == 1.5  # 1.0 (Mon) + 0.5 (Tue) + 0.0 (Sat)
    assert data["leave_days"] == 1
    assert data["late_count"] == 2  # Mon (15) + Sat (20)
    assert data["total_late_minutes"] == 35  # 15 + 20
    assert data["total_overtime_minutes"] == 75  # 35 + 40
    # attendance_pct: 1.5 / 23 * 100 = 6.5217... -> 6.52
    assert data["attendance_pct"] == 6.52


# --------------------------------------------------------------------------- #
# 2. Department Summary Analytics Tests
# --------------------------------------------------------------------------- #
def test_department_summary_includes_zero_log_employees(client, mock_db):
    # Engineering: 3 employees joined before/in July 2026
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "Asha",
            "email": "asha@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01",
        },
    )
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0002",
            "name": "Bikram",
            "email": "bikram@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01",
        },
    )
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0003",
            "name": "Chitra",
            "email": "chitra@example.com",
            "department": "Engineering",
            "joined_on": "2026-07-15",
        },
    )
    # Employee joined in August -> must NOT count in July headcount!
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0004",
            "name": "Deepak",
            "email": "deepak@example.com",
            "department": "Engineering",
            "joined_on": "2026-08-01",
        },
    )

    # Only EMP0001 has attendance logs
    mock_db.attendance_logs.insert_one({
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "work_hours": 9.12,
        "half_day": False,
        "late_minutes": 10,
        "overtime_minutes": 0,
    })
    mock_db.attendance_logs.insert_one({
        "emp_code": "EMP0001",
        "date": "2026-07-07",
        "status": "PRESENT",
        "work_hours": 8.88,
        "half_day": False,
        "late_minutes": 0,
        "overtime_minutes": 0,
    })

    res = client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert len(data["items"]) == 1
    eng = data["items"][0]
    assert eng["department"] == "Engineering"
    assert eng["headcount"] == 3  # EMP0001, EMP0002, EMP0003 (EMP0004 joined in August)
    assert eng["present_days"] == 2.0
    # Average work hours across records: (9.12 + 8.88) / 2 = 9.0
    assert eng["avg_work_hours"] == 9.0
    assert eng["late_count"] == 1
    assert eng["total_late_minutes"] == 10


# --------------------------------------------------------------------------- #
# 3. Late Leaderboard Tests
# --------------------------------------------------------------------------- #
def test_late_leaderboard_standard_competition_ranking_and_ties(client, mock_db):
    for i in range(1, 5):
        client.post(
            "/employees",
            json={
                "emp_code": f"EMP000{i}",
                "name": f"Employee {i}",
                "email": f"emp{i}@example.com",
                "department": "Sales",
                "joined_on": "2026-01-01",
            },
        )

    # Late minutes:
    # EMP0001: 60 min -> Rank 1
    # EMP0002: 40 min -> Rank 2
    # EMP0003: 40 min -> Rank 2 (tied!)
    # EMP0004: 20 min -> Rank 4 (Rank 3 skipped!)
    mock_db.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-06", "late_minutes": 60})
    mock_db.attendance_logs.insert_one({"emp_code": "EMP0002", "date": "2026-07-06", "late_minutes": 40})
    mock_db.attendance_logs.insert_one({"emp_code": "EMP0003", "date": "2026-07-06", "late_minutes": 40})
    mock_db.attendance_logs.insert_one({"emp_code": "EMP0004", "date": "2026-07-06", "late_minutes": 20})

    # With limit=2: both EMP0002 and EMP0003 have rank 2 <= 2 -> both returned!
    res = client.get("/analytics/leaderboard/late?month=2026-07&limit=2")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 3
    assert [it["rank"] for it in items] == [1, 2, 2]
    assert [it["emp_code"] for it in items] == ["EMP0001", "EMP0002", "EMP0003"]

    # With limit=4: all 4 returned, rank sequence 1, 2, 2, 4
    res_all = client.get("/analytics/leaderboard/late?month=2026-07&limit=4")
    assert res_all.status_code == 200
    items_all = res_all.json()["items"]
    assert len(items_all) == 4
    assert [it["rank"] for it in items_all] == [1, 2, 2, 4]


# --------------------------------------------------------------------------- #
# 4. Department Trend Tests
# --------------------------------------------------------------------------- #
def test_department_trend_gap_filling_and_moving_avg(client, mock_db):
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "Asha",
            "email": "asha@example.com",
            "department": "Engineering",
            "joined_on": "2026-07-06",
        },
    )
    # Range 2026-07-06 (Mon) to 2026-07-12 (Sun) = 7 days
    # Mon 07-06: PRESENT -> rate = 1.0 / 1 = 1.0000
    # Tue 07-07: NO LOGS -> working day with 0 records -> rate = 0.0000
    # Wed 07-08: HALF DAY -> 0.5 / 1 = 0.5000
    # Thu 07-09: NO LOGS -> rate = 0.0000
    # Fri 07-10: PRESENT -> rate = 1.0000
    # Sat 07-11: Weekend -> rate = None
    # Sun 07-12: Weekend -> rate = None
    mock_db.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT", "half_day": False, "late_minutes": 0})
    mock_db.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-08", "status": "PRESENT", "half_day": True, "late_minutes": 0})
    mock_db.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-10", "status": "PRESENT", "half_day": False, "late_minutes": 0})

    res = client.get("/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-12")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 7

    # Day 0 (Mon): rate 1.0000, moving_avg = 1.0000
    assert items[0]["date"] == "2026-07-06"
    assert items[0]["is_working_day"] is True
    assert items[0]["attendance_rate"] == 1.0000
    assert items[0]["moving_avg_7d"] == 1.0000

    # Day 1 (Tue): rate 0.0000, moving_avg = (1.0 + 0.0) / 2 = 0.5000
    assert items[1]["date"] == "2026-07-07"
    assert items[1]["attendance_rate"] == 0.0000
    assert items[1]["moving_avg_7d"] == 0.5000

    # Day 5 (Sat): weekend, attendance_rate is None, moving_avg keeps preceding weekday average
    assert items[5]["date"] == "2026-07-11"
    assert items[5]["is_working_day"] is False
    assert items[5]["attendance_rate"] is None
    assert items[5]["moving_avg_7d"] is not None


def test_department_summary_department_filter_and_no_work_hours(client, mock_db):
    client.post(
        "/employees",
        json={"emp_code": "EMP0010", "name": "HR Person", "email": "hr@example.com", "department": "HR", "joined_on": "2026-01-01"},
    )
    client.post(
        "/employees",
        json={"emp_code": "EMP0020", "name": "Eng Person", "email": "eng@example.com", "department": "Engineering", "joined_on": "2026-01-01"},
    )
    # HR person has a record with no punch-out (work_hours is None)
    mock_db.attendance_logs.insert_one({"emp_code": "EMP0010", "date": "2026-07-06", "status": "PRESENT", "work_hours": None})

    res = client.get("/analytics/departments/summary?month=2026-07&department=HR")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    assert items[0]["department"] == "HR"
    assert items[0]["headcount"] == 1
    assert items[0]["avg_work_hours"] is None  # No records with work_hours


def test_late_leaderboard_department_filter(client, mock_db):
    client.post(
        "/employees",
        json={"emp_code": "EMP0001", "name": "Sales 1", "email": "s1@example.com", "department": "Sales", "joined_on": "2026-01-01"},
    )
    client.post(
        "/employees",
        json={"emp_code": "EMP0002", "name": "Eng 1", "email": "e1@example.com", "department": "Engineering", "joined_on": "2026-01-01"},
    )
    mock_db.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-06", "late_minutes": 50})
    mock_db.attendance_logs.insert_one({"emp_code": "EMP0002", "date": "2026-07-06", "late_minutes": 80})

    res = client.get("/analytics/leaderboard/late?month=2026-07&department=Sales")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    assert items[0]["emp_code"] == "EMP0001"
    assert items[0]["rank"] == 1
    assert items[0]["total_late_minutes"] == 50


def test_department_trend_headcount_increase_and_zero_headcount(client, mock_db):
    # Employee joins on 2026-07-08 (Wednesday)
    client.post(
        "/employees",
        json={"emp_code": "EMP0001", "name": "Joiner", "email": "j@example.com", "department": "Support", "joined_on": "2026-07-08"},
    )
    # Range: 2026-07-06 (Mon) to 2026-07-10 (Fri)
    # Mon 07-06: Headcount = 0 -> attendance_rate = None
    # Tue 07-07: Headcount = 0 -> attendance_rate = None
    # Wed 07-08: Headcount = 1 -> joined today, worked -> rate = 1.0000
    mock_db.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-08", "status": "PRESENT", "half_day": False, "late_minutes": 0})

    res = client.get("/analytics/departments/Support/trend?from=2026-07-06&to=2026-07-10")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 5

    # Day 0 & 1: headcount 0 -> attendance_rate is None
    assert items[0]["headcount"] == 0
    assert items[0]["attendance_rate"] is None
    assert items[0]["moving_avg_7d"] is None

    assert items[1]["headcount"] == 0
    assert items[1]["attendance_rate"] is None
    assert items[1]["moving_avg_7d"] is None

    # Day 2: headcount 1 -> present 1.0 -> rate = 1.0000, moving_avg = 1.0000
    assert items[2]["headcount"] == 1
    assert items[2]["present_count"] == 1.0
    assert items[2]["attendance_rate"] == 1.0000
    assert items[2]["moving_avg_7d"] == 1.0000


def test_department_trend_validation(client):
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "Asha",
            "email": "asha@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01",
        },
    )
    # Missing department -> 404
    res_404 = client.get("/analytics/departments/NonExistent/trend?from=2026-07-01&to=2026-07-05")
    assert res_404.status_code == 404

    # to < from -> 422
    res_inv = client.get("/analytics/departments/Engineering/trend?from=2026-07-10&to=2026-07-01")
    assert res_inv.status_code == 422

    # range > 92 days -> 422
    res_wide = client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-05-01")
    assert res_wide.status_code == 422

