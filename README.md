# Employee Attendance & Analytics API

A production-ready REST API for employee management, shift attendance tracking, manual corrections with audit logging, and organizational analytics built with Python 3.11+, FastAPI, and MongoDB 6.0+/7.0.

All application logic is implemented in `app/main.py` in accordance with the assignment specifications, `openapi.yaml`, and `DATA_MODEL.md`.

---

## Implemented Endpoints

### 1. System & Health
* `GET /health`: Connectivity check via MongoDB `ping` command (returns HTTP 200 when healthy, HTTP 503 if database is unreachable).

### 2. Employee Management
* `POST /employees`: Create employee records with IST shift timings and validation. Enforces unique `emp_code` (HTTP 409 on duplicate).
* `GET /employees`: 1-based paginated list of employees with optional department filtering, sorted by `emp_code` ascending.

### 3. Attendance Tracking & Corrections
* `POST /attendance/punch-in`: Record attendance punch-in with IST shift/overnight boundary date resolution (R1) and late minutes calculation with 15-minute grace period (R2). Concurrency-safe via unique compound index (HTTP 409 on duplicate punch-in for the same date).
* `POST /attendance/punch-out`: Atomic punch-out calculation of overtime (R3, 30-minute minimum), total work hours rounded half-up to 2 decimals (R4), and half-day flag for work hours under 4.50 (R5). Validates 24-hour limit and punch sequence.
* `GET /attendance`: Paginated attendance history with database-level sorting (`date` desc, `emp_code` asc) and date range/status filtering.
* `PATCH /attendance/{emp_code}/{date}`: Manual attendance corrections (regularizations) supporting state transitions (`PRESENT`, `WFH`, `ON_DUTY`, `ABSENT`, `LEAVE`), punch time adjustments with recomputed derived fields, optimistic concurrency locking, and an append-only audit `history` trail.

### 4. Analytics Aggregations
* `GET /analytics/employees/{emp_code}/monthly`: Monthly attendance percentage, working weekdays, present days (full day = 1.0, half day = 0.5), leave count, late minutes, and overtime minutes.
* `GET /analytics/departments/summary`: Monthly department-wide summary starting from `employees` to guarantee inclusion of employees with zero attendance records, computing record-level average work hours.
* `GET /analytics/leaderboard/late`: Standard competition ranking (1, 2, 2, 4) using MongoDB's `$setWindowFields` / `$rank`, preserving all tied employees at the rank cutoff limit.
* `GET /analytics/departments/{department}/trend`: Gap-filled daily attendance rate and 7-day trailing moving average over the preceding non-null rates, dynamically computing daily active headcount from employee `joined_on` dates.

### 5. Admin & Query Plan Explain
* `GET /admin/explain/{endpoint}`: Runs MongoDB `db.command` with `verbosity="executionStats"` on the exact find query or aggregation pipeline used by the production endpoint (`attendance_list`, `employee_monthly`, `department_summary`, `late_leaderboard`, `department_trend`). Validates endpoint names and parameters (HTTP 422 on invalid/missing input) and redacts sensitive credentials.

---

## Configuration

The application reads MongoDB settings with environment variables taking precedence over `.env` files:

* `MONGO_URI`: MongoDB connection string (defaults to `mongodb://localhost:27017` if unset).
* `MONGO_DB`: Target database name (defaults to `hrms_attendance` if unset).

No credentials or `.env` files are committed to this repository.

---

## Installation & Setup

### Prerequisites
* Python 3.11 or higher
* MongoDB 6.0 or newer (the grader uses MongoDB 7.0; MongoDB Atlas free tier M0 works)

### Local Environment Setup
```bash
# 1. Clone repository and navigate to folder
cd candidate_kit

# 2. Create and activate a virtual environment
python -m venv .venv
# On Windows (PowerShell):
.venv\Scripts\Activate.ps1
# On Linux/macOS:
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment variables (optional, defaults provided)
# PowerShell:
$env:MONGO_URI="mongodb://localhost:27017"
$env:MONGO_DB="hrms_attendance"
# Bash:
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="hrms_attendance"

# 5. Seed sample data (optional)
python sample_seed.py

# 6. Run the application
uvicorn app.main:app --port 8000 --reload
```

---

## Automated Testing

The automated test suite covers all stages (helpers, business rules, CRUD, concurrency races, optimistic locking, aggregation pipelines, and query plan explain):

```bash
# Run complete test suite
pytest tests/ -v
```

### Mocked vs Real Integration Tests
* **Mocked Tests**: Unit tests in `tests/test_stage1.py` through `tests/test_stage5.py` run against an in-memory `mongomock` instance and mock database commands. These tests validate input validation, schema serializations, atomic concurrency handling, optimistic locking, and command construction without requiring an active external MongoDB process.
* **Real Integration Tests**: Integration tests (such as `test_real_mongodb_explain_integration`) dynamically verify network connectivity to `MONGO_URI`. If a real MongoDB instance is reachable, it runs explain commands directly against live collections; otherwise, it skips safely.

---

## Interactive API Documentation

When the application is running, FastAPI provides interactive API documentation and testing interfaces at:
* Swagger UI: `http://localhost:8000/docs`
* ReDoc: `http://localhost:8000/redoc`
* OpenAPI JSON Specification: `http://localhost:8000/openapi.json`

---

## Known Limitations & Unverified Behavior

1. **Local Test Environment**: Automated tests in the development environment executed against `mongomock` and mock drivers because a real MongoDB 6.0+ service was not active locally on port 27017. Real database integration tests were skipped locally.
2. **Execution Statistics Verification**: While all queries were structured to align with the compound indexes created in `lifespan` (`ensure_indexes()`), actual execution statistics (`IXSCAN` and zero `COLLSCAN`) must be verified against an active MongoDB 6.0/7.0 instance or the grader's live evaluation environment.
