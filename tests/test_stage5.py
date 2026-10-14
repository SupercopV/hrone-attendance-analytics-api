import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient
import mongomock
from app.main import app, db


@pytest.fixture
def mock_db(monkeypatch):
    mock_client = mongomock.MongoClient()
    test_db = mock_client["test_attendance_db"]

    test_db.employees.create_index([("emp_code", 1)], unique=True)
    test_db.employees.create_index([("department", 1), ("joined_on", 1)])
    test_db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    test_db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    test_db.attendance_logs.create_index([("date", 1), ("late_minutes", 1), ("emp_code", 1)])

    monkeypatch.setattr("app.main.client", mock_client)
    monkeypatch.setattr("app.main.db", test_db)
    return test_db


@pytest.fixture
def client(mock_db):
    return TestClient(app)


def get_sample_explain_output(collection_name: str, index_name: str = "emp_code_1_date_1"):
    return {
        "explainVersion": "1",
        "queryPlanner": {
            "namespace": f"test_db.{collection_name}",
            "indexFilterSet": False,
            "winningPlan": {
                "stage": "FETCH",
                "inputStage": {
                    "stage": "IXSCAN",
                    "keyPattern": {"emp_code": 1, "date": 1},
                    "indexName": index_name,
                    "isMultiKey": False,
                    "direction": "forward",
                },
            },
            "rejectedPlans": [],
        },
        "executionStats": {
            "executionSuccess": True,
            "nReturned": 5,
            "executionTimeMillis": 1,
            "totalKeysExamined": 5,
            "totalDocsExamined": 5,
            "executionStages": {
                "stage": "FETCH",
                "nReturned": 5,
                "inputStage": {
                    "stage": "IXSCAN",
                    "nReturned": 5,
                    "indexName": index_name,
                },
            },
        },
        "serverInfo": {
            "host": "mongodb-primary",
            "port": 27017,
            "version": "6.0.8",
        },
    }


# =========================================================================== #
# Validation & Error Tests
# =========================================================================== #

def test_explain_unknown_endpoint_returns_422(client):
    res = client.get("/admin/explain/unknown_endpoint")
    assert res.status_code == 422
    assert "Unknown endpoint" in res.json()["detail"]


def test_explain_employee_monthly_parameter_validation(client):
    # Missing emp_code
    res1 = client.get("/admin/explain/employee_monthly?month=2026-07")
    assert res1.status_code == 422

    # Missing month
    res2 = client.get("/admin/explain/employee_monthly?emp_code=EMP0001")
    assert res2.status_code == 422

    # Invalid month format
    res3 = client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-13")
    assert res3.status_code == 422

    res4 = client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=not-a-month")
    assert res4.status_code == 422


def test_explain_department_summary_parameter_validation(client):
    # Missing month
    res1 = client.get("/admin/explain/department_summary")
    assert res1.status_code == 422

    # Invalid month pattern
    res2 = client.get("/admin/explain/department_summary?month=2026-00")
    assert res2.status_code == 422


def test_explain_late_leaderboard_parameter_validation(client):
    # Missing month
    res1 = client.get("/admin/explain/late_leaderboard")
    assert res1.status_code == 422

    # Invalid limit < 1
    res2 = client.get("/admin/explain/late_leaderboard?month=2026-07&limit=0")
    assert res2.status_code == 422

    # Invalid limit > 50
    res3 = client.get("/admin/explain/late_leaderboard?month=2026-07&limit=51")
    assert res3.status_code == 422


def test_explain_department_trend_parameter_validation(client):
    # Missing all parameters
    res1 = client.get("/admin/explain/department_trend")
    assert res1.status_code == 422

    # Missing department
    res2 = client.get("/admin/explain/department_trend?from=2026-07-01&to=2026-07-10")
    assert res2.status_code == 422

    # Missing from
    res3 = client.get("/admin/explain/department_trend?department=Engineering&to=2026-07-10")
    assert res3.status_code == 422

    # Missing to
    res4 = client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01")
    assert res4.status_code == 422

    # Invalid from date
    res5 = client.get("/admin/explain/department_trend?department=Engineering&from=not-a-date&to=2026-07-10")
    assert res5.status_code == 422

    # to < from
    res6 = client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-15&to=2026-07-10")
    assert res6.status_code == 422

    # Date range > 92 days (Jan 1 to May 1 = 120 days)
    res7 = client.get("/admin/explain/department_trend?department=Engineering&from=2026-01-01&to=2026-05-01")
    assert res7.status_code == 422


def test_explain_attendance_list_parameter_validation(client):
    # date_from > date_to
    res1 = client.get("/admin/explain/attendance_list?date_from=2026-07-15&date_to=2026-07-10")
    assert res1.status_code == 422

    # Invalid date_from format
    res2 = client.get("/admin/explain/attendance_list?date_from=invalid-date")
    assert res2.status_code == 422

    # Invalid page (< 1)
    res3 = client.get("/admin/explain/attendance_list?page=0")
    assert res3.status_code == 422

    # Invalid page_size (> 100)
    res4 = client.get("/admin/explain/attendance_list?page_size=150")
    assert res4.status_code == 422

    # Invalid status
    res5 = client.get("/admin/explain/attendance_list?status=NOT_A_STATUS")
    assert res5.status_code == 422


# =========================================================================== #
# Schema & Execution Tests (Mocked DB Command)
# =========================================================================== #

def test_explain_attendance_list_success_and_command_structure(client, mock_db):
    executed_commands = []

    def mock_command(cmd):
        executed_commands.append(cmd)
        return get_sample_explain_output("attendance_logs", "date_-1_emp_code_1")

    mock_db.command = MagicMock(side_effect=mock_command)

    res = client.get(
        "/admin/explain/attendance_list?emp_code=EMP0001&date_from=2026-07-01&date_to=2026-07-31&status=PRESENT&page=2&page_size=15"
    )
    assert res.status_code == 200
    body = res.json()
    assert body["endpoint"] == "attendance_list"
    assert body["collection"] == "attendance_logs"
    assert "explain" in body
    assert body["explain"]["queryPlanner"]["winningPlan"]["stage"] == "FETCH"
    assert body["explain"]["queryPlanner"]["winningPlan"]["inputStage"]["stage"] == "IXSCAN"

    # Verify executed command structure
    assert len(executed_commands) == 1
    cmd = executed_commands[0]
    assert cmd["verbosity"] == "executionStats"
    assert cmd["explain"]["find"] == "attendance_logs"
    assert cmd["explain"]["filter"]["emp_code"] == "EMP0001"
    assert cmd["explain"]["filter"]["date"] == {"$gte": "2026-07-01", "$lte": "2026-07-31"}
    assert cmd["explain"]["filter"]["status"] == "PRESENT"
    assert cmd["explain"]["skip"] == 15  # (page 2 - 1) * 15
    assert cmd["explain"]["limit"] == 15
    assert cmd["explain"]["sort"] == {"date": -1, "emp_code": 1}


def test_explain_employee_monthly_success_and_command_structure(client, mock_db):
    executed_commands = []

    def mock_command(cmd):
        executed_commands.append(cmd)
        return get_sample_explain_output("attendance_logs", "emp_code_1_date_1")

    mock_db.command = MagicMock(side_effect=mock_command)

    res = client.get("/admin/explain/employee_monthly?emp_code=EMP0002&month=2026-07")
    assert res.status_code == 200
    body = res.json()
    assert body["endpoint"] == "employee_monthly"
    assert body["collection"] == "attendance_logs"
    assert "explain" in body

    assert len(executed_commands) == 1
    cmd = executed_commands[0]
    assert cmd["verbosity"] == "executionStats"
    assert cmd["explain"]["aggregate"] == "attendance_logs"
    pipeline = cmd["explain"]["pipeline"]
    assert pipeline[0]["$match"]["emp_code"] == "EMP0002"
    assert pipeline[0]["$match"]["date"] == {"$gte": "2026-07-01", "$lte": "2026-07-31"}


def test_explain_department_summary_success_and_command_structure(client, mock_db):
    executed_commands = []

    def mock_command(cmd):
        executed_commands.append(cmd)
        return get_sample_explain_output("employees", "department_1_joined_on_1")

    mock_db.command = MagicMock(side_effect=mock_command)

    res = client.get("/admin/explain/department_summary?month=2026-07&department=Engineering")
    assert res.status_code == 200
    body = res.json()
    assert body["endpoint"] == "department_summary"
    assert body["collection"] == "employees"
    assert "explain" in body

    assert len(executed_commands) == 1
    cmd = executed_commands[0]
    assert cmd["verbosity"] == "executionStats"
    assert cmd["explain"]["aggregate"] == "employees"
    pipeline = cmd["explain"]["pipeline"]
    assert pipeline[0]["$match"]["department"] == "Engineering"
    assert pipeline[0]["$match"]["joined_on"] == {"$lte": "2026-07-31"}


def test_explain_late_leaderboard_success_and_command_structure(client, mock_db):
    executed_commands = []

    def mock_command(cmd):
        executed_commands.append(cmd)
        return get_sample_explain_output("attendance_logs", "date_1_late_minutes_1_emp_code_1")

    mock_db.command = MagicMock(side_effect=mock_command)

    res = client.get("/admin/explain/late_leaderboard?month=2026-07&limit=25&department=Sales")
    assert res.status_code == 200
    body = res.json()
    assert body["endpoint"] == "late_leaderboard"
    assert body["collection"] == "attendance_logs"
    assert "explain" in body

    assert len(executed_commands) == 1
    cmd = executed_commands[0]
    assert cmd["verbosity"] == "executionStats"
    assert cmd["explain"]["aggregate"] == "attendance_logs"
    pipeline = cmd["explain"]["pipeline"]
    assert pipeline[0]["$match"]["date"] == {"$gte": "2026-07-01", "$lte": "2026-07-31"}
    assert pipeline[0]["$match"]["late_minutes"] == {"$gt": 0}
    # Verify limit is applied in rank match
    rank_matches = [stage["$match"] for stage in pipeline if "$match" in stage and "rank" in stage["$match"]]
    assert len(rank_matches) == 1
    assert rank_matches[0]["rank"]["$lte"] == 25


def test_explain_department_trend_success_and_command_structure(client, mock_db):
    executed_commands = []

    def mock_command(cmd):
        executed_commands.append(cmd)
        return get_sample_explain_output("attendance_logs", "date_-1_emp_code_1")

    mock_db.command = MagicMock(side_effect=mock_command)

    res = client.get("/admin/explain/department_trend?department=Support&from=2026-07-01&to=2026-07-15")
    assert res.status_code == 200
    body = res.json()
    assert body["endpoint"] == "department_trend"
    assert body["collection"] == "attendance_logs"
    assert "explain" in body

    assert len(executed_commands) == 1
    cmd = executed_commands[0]
    assert cmd["verbosity"] == "executionStats"
    assert cmd["explain"]["aggregate"] == "attendance_logs"
    pipeline = cmd["explain"]["pipeline"]
    assert pipeline[0]["$match"]["date"] == {"$gte": "2026-07-01", "$lte": "2026-07-15"}


# =========================================================================== #
# Sensitive Info Redaction & JSON Sanitization Tests
# =========================================================================== #

def test_explain_redacts_sensitive_keys(client, mock_db):
    mock_db.command = MagicMock(
        return_value={
            "explainVersion": "1",
            "serverInfo": {
                "host": "localhost",
                "uri": "mongodb://user:super_secret_password@localhost:27017/test_db",
                "connectionString": "mongodb://localhost:27017",
            },
            "auth": {
                "password": "my_password",
                "secret": "top_secret_token",
            },
            "queryPlanner": {
                "winningPlan": {"stage": "IXSCAN"},
            },
        }
    )

    res = client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07")
    assert res.status_code == 200
    explain = res.json()["explain"]
    assert explain["serverInfo"]["uri"] == "[REDACTED]"
    assert explain["serverInfo"]["connectionString"] == "[REDACTED]"
    assert explain["auth"] == "[REDACTED]"
    assert explain["queryPlanner"]["winningPlan"]["stage"] == "IXSCAN"


# =========================================================================== #
# Read-Only Guarantee: Explain Does Not Modify Application Data
# =========================================================================== #

def test_explain_does_not_modify_application_data(client, mock_db):
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-05",
        },
    )
    mock_db.attendance_logs.insert_one(
        {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "work_hours": 9.12,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }
    )

    mock_db.command = MagicMock(return_value=get_sample_explain_output("attendance_logs"))

    # Snapshot before explains
    emp_before = list(mock_db.employees.find({}, {"_id": 0}))
    att_before = list(mock_db.attendance_logs.find({}, {"_id": 0}))

    # Execute all 5 explain endpoints
    client.get("/admin/explain/attendance_list?emp_code=EMP0001")
    client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07")
    client.get("/admin/explain/department_summary?month=2026-07")
    client.get("/admin/explain/late_leaderboard?month=2026-07")
    client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-10")

    # Snapshot after explains
    emp_after = list(mock_db.employees.find({}, {"_id": 0}))
    att_after = list(mock_db.attendance_logs.find({}, {"_id": 0}))

    assert emp_before == emp_after
    assert att_before == att_after


# =========================================================================== #
# Real MongoDB Integration Test (Conditional)
# =========================================================================== #

def test_real_mongodb_explain_integration():
    """Attempts to connect to a real MongoDB instance if MONGO_URI is set and reachable.
    Verifies that the winning query plan contains IXSCAN and zero COLLSCAN.
    """
    import os
    import pymongo

    mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    mongo_db_name = os.getenv("MONGO_DB", "hrms_attendance_test")

    try:
        real_client = pymongo.MongoClient(mongo_uri, serverSelectionTimeoutMS=1000)
        real_client.admin.command("ping")
    except Exception:
        pytest.skip("Real MongoDB 6.0+ instance is not running or not reachable on MONGO_URI")

    # If real MongoDB is reachable, test real explain execution:
    real_db = real_client[mongo_db_name]
    # Ensure indexes
    real_db.employees.create_index([("emp_code", 1)], unique=True)
    real_db.employees.create_index([("department", 1), ("joined_on", 1)])
    real_db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    real_db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    real_db.attendance_logs.create_index([("date", 1), ("late_minutes", 1), ("emp_code", 1)])

    # Explain find on attendance_logs
    res = real_db.command({
        "explain": {
            "find": "attendance_logs",
            "filter": {"emp_code": "EMP0001", "date": {"$gte": "2026-07-01"}},
            "sort": {"date": -1, "emp_code": 1},
            "skip": 0,
            "limit": 20,
        },
        "verbosity": "executionStats",
    })
    plan_str = str(res)
    assert "IXSCAN" in plan_str
    assert "COLLSCAN" not in plan_str
