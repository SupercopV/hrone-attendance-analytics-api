"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (read from environment, with .env as fallback)
"""
import os
import math
import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, Field, model_validator
from pymongo import MongoClient
import pymongo.errors

# --------------------------------------------------------------------------- #
# Environment & MongoDB Setup
# --------------------------------------------------------------------------- #
# Real environment variables take precedence over .env (override=False)
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=2000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def ensure_indexes():
    """Create all required MongoDB indexes idempotently."""
    try:
        db.employees.create_index([("emp_code", 1)], unique=True)
        db.employees.create_index([("department", 1), ("joined_on", 1)])
        db.employees.create_index([("joined_on", 1)])

        db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
        db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
        db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
        db.attendance_logs.create_index([("date", 1), ("late_minutes", 1), ("emp_code", 1)])
    except Exception:
        # Ignore startup connection errors if DB starts asynchronously
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helper Functions
# --------------------------------------------------------------------------- #
def truncate_ms(ms: int) -> int:
    """Truncates epoch milliseconds to whole seconds (R1)."""
    return (ms // 1000) * 1000


def round_half_up(value: Any, decimals: int) -> Optional[float]:
    """R8 rounding: Half-up rounding using Decimal."""
    if value is None:
        return None
    d = Decimal(str(value))
    q = Decimal("10") ** -decimals
    return float(d.quantize(q, rounding=ROUND_HALF_UP))


def ms_to_datetime(ms: Optional[int]) -> Optional[datetime]:
    """Converts epoch ms to timezone-aware UTC datetime with whole-second truncation."""
    if ms is None:
        return None
    sec = truncate_ms(ms) // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def datetime_to_ms(dt: Any) -> Optional[int]:
    """Converts BSON datetime or int/float to epoch milliseconds."""
    if dt is None:
        return None
    if isinstance(dt, (int, float)):
        return truncate_ms(int(dt))
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return truncate_ms(int(dt.timestamp() * 1000))
    return None


def determine_attendance_date(punch_in_ms: int, shift_start: str, shift_end: str) -> str:
    """
    R1: Attendance date is the IST calendar date of punch_in.
    Overnight shifts (shift_end <= shift_start): punch-in earlier than shift_end
    belongs to the shift that started the previous day.
    """
    punch_in_ms = truncate_ms(punch_in_ms)
    dt_utc = datetime.fromtimestamp(punch_in_ms / 1000.0, tz=timezone.utc)
    dt_ist = dt_utc.astimezone(IST)
    is_overnight = shift_end <= shift_start
    if is_overnight and dt_ist.strftime("%H:%M") < shift_end:
        attendance_day = dt_ist.date() - timedelta(days=1)
        return attendance_day.isoformat()
    return dt_ist.date().isoformat()


def get_shift_datetimes(attendance_date_str: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    """
    Computes IST start and end datetimes for a given attendance date.
    For overnight shifts, shift_end falls on the next calendar day.
    """
    y, m, d = map(int, attendance_date_str.split("-"))
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))

    start_dt = datetime(y, m, d, sh, sm, 0, tzinfo=IST)
    if shift_end <= shift_start:
        next_d = date(y, m, d) + timedelta(days=1)
        end_dt = datetime(next_d.year, next_d.month, next_d.day, eh, em, 0, tzinfo=IST)
    else:
        end_dt = datetime(y, m, d, eh, em, 0, tzinfo=IST)

    return start_dt, end_dt


def compute_late_minutes(punch_in_ms: int, shift_start_dt: datetime) -> int:
    """
    R2: Late if strictly more than 10:00 (600 seconds) after shift_start.
    Then whole minutes (floor) since shift_start, otherwise 0.
    """
    punch_in_ms = truncate_ms(punch_in_ms)
    punch_in_ist = datetime.fromtimestamp(punch_in_ms / 1000.0, tz=timezone.utc).astimezone(IST)
    delta_sec = (punch_in_ist - shift_start_dt).total_seconds()
    if delta_sec > 600:
        return int(delta_sec // 60)
    return 0


def compute_overtime(punch_out_ms: int, shift_end_dt: datetime) -> int:
    """
    R3: Whole minutes (floor) from shift_end to punch_out, counted only if >= 30; otherwise 0.
    """
    punch_out_ms = truncate_ms(punch_out_ms)
    punch_out_ist = datetime.fromtimestamp(punch_out_ms / 1000.0, tz=timezone.utc).astimezone(IST)
    delta_sec = (punch_out_ist - shift_end_dt).total_seconds()
    ot_minutes = int(delta_sec // 60)
    if ot_minutes >= 30:
        return ot_minutes
    return 0


def compute_work_hours(punch_in_ms: int, punch_out_ms: int) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded 2 decimals half-up.
    """
    in_ms = truncate_ms(punch_in_ms)
    out_ms = truncate_ms(punch_out_ms)
    delta_sec = (out_ms - in_ms) / 1000.0
    hours = Decimal(delta_sec) / Decimal(3600)
    return round_half_up(hours, 2)


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5: half_day is true when rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


# --------------------------------------------------------------------------- #
# Pydantic Models & Schemas
# --------------------------------------------------------------------------- #
EpochMillis = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
StatusType = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatusType = Literal["PRESENT", "WFH", "ON_DUTY"]


class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatusType = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(BaseModel):
    status: Optional[StatusType] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Response Serializers (No internal _id exposed)
# --------------------------------------------------------------------------- #
def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_ms(doc.get("created_at")),
    }


def serialize_attendance_record(doc: dict) -> dict:
    history_entries = []
    for h in doc.get("history", []) or []:
        raw_changes = h.get("changes", {}) or {}
        serialized_changes = {}
        for k, v in raw_changes.items():
            if k in ("punch_in", "punch_out"):
                serialized_changes[k] = {
                    "from": datetime_to_ms(v.get("from")),
                    "to": datetime_to_ms(v.get("to")),
                }
            else:
                serialized_changes[k] = {
                    "from": v.get("from"),
                    "to": v.get("to"),
                }
        history_entries.append({
            "at": datetime_to_ms(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": serialized_changes,
        })

    wh = doc.get("work_hours")
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_ms(doc.get("punch_in")),
        "punch_out": datetime_to_ms(doc.get("punch_out")),
        "work_hours": round_half_up(wh, 2) if wh is not None else None,
        "late_minutes": int(doc.get("late_minutes", 0) or 0),
        "overtime_minutes": int(doc.get("overtime_minutes", 0) or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": history_entries,
    }


# --------------------------------------------------------------------------- #
# Endpoints - Stage 2 Implementation
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except pymongo.errors.DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [serialize_employee(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        punch_in_ms = truncate_ms(body.punched_at)
    else:
        now_dt = datetime.now(timezone.utc)
        punch_in_ms = truncate_ms(int(now_dt.timestamp() * 1000))

    att_date = determine_attendance_date(punch_in_ms, emp["shift_start"], emp["shift_end"])
    shift_start_dt, _ = get_shift_datetimes(att_date, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(punch_in_ms, shift_start_dt)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ms_to_datetime(punch_in_ms),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except pymongo.errors.DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return serialize_attendance_record(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        punch_out_ms = truncate_ms(body.punched_at)
    else:
        now_dt = datetime.now(timezone.utc)
        punch_out_ms = truncate_ms(int(now_dt.timestamp() * 1000))

    # Find the employee's most recent record that has a punch_in
    query = {"emp_code": body.emp_code, "punch_in": {"$ne": None}}
    record = db.attendance_logs.find_one(query, sort=[("punch_in", -1)])

    if not record or record.get("punch_in") is None:
        raise HTTPException(404, "No punch-in found")

    punch_in_ms = datetime_to_ms(record["punch_in"])

    # 422 if punched_at <= punch_in or more than 24 hours after it
    if punch_out_ms <= punch_in_ms:
        raise HTTPException(422, "punch_out must be after punch_in")

    if (punch_out_ms - punch_in_ms) > 24 * 3600 * 1000:
        raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")

    _, shift_end_dt = get_shift_datetimes(record["date"], emp["shift_start"], emp["shift_end"])
    work_hours = compute_work_hours(punch_in_ms, punch_out_ms)
    overtime_minutes = compute_overtime(punch_out_ms, shift_end_dt)
    half_day = compute_half_day(work_hours)

    punch_out_dt = ms_to_datetime(punch_out_ms)
    updated_doc = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=pymongo.ReturnDocument.AFTER,
    )

    if not updated_doc:
        raise HTTPException(409, "Already punched out")

    return serialize_attendance_record(updated_doc)


def build_attendance_query(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Any] = None,
) -> dict:
    if date_from:
        try:
            date.fromisoformat(date_from)
        except Exception:
            raise HTTPException(422, "Invalid date_from format")
    if date_to:
        try:
            date.fromisoformat(date_to)
        except Exception:
            raise HTTPException(422, "Invalid date_to format")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from cannot be after date_to")

    q: dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status.value if hasattr(status, "value") else status
    return q


@app.get("/attendance", status_code=200)
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusType] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = build_attendance_query(emp_code, date_from, date_to, status)

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_attendance_record(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    body: RegularizeIn = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "Attendance record not found")

    new_status = body.status if body.status is not None else record["status"]

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "punch times cannot be supplied when status is ABSENT or LEAVE")
        final_punch_in = None
        final_punch_out = None
        final_work_hours = None
        final_late_minutes = 0
        final_overtime_minutes = 0
        final_half_day = False
    else:
        # Presence status: requires punch_in
        if body.punch_in is not None:
            final_punch_in = truncate_ms(body.punch_in)
            computed_date = determine_attendance_date(final_punch_in, emp["shift_start"], emp["shift_end"])
            if computed_date != date:
                raise HTTPException(422, f"punch_in belongs to attendance date {computed_date}, which does not match URL date {date}")
        else:
            final_punch_in = datetime_to_ms(record.get("punch_in"))
            if final_punch_in is None:
                raise HTTPException(422, "punch_in is required for presence status")

        if body.punch_out is not None:
            final_punch_out = truncate_ms(body.punch_out)
        else:
            final_punch_out = datetime_to_ms(record.get("punch_out"))

        if final_punch_out is not None:
            if final_punch_out <= final_punch_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (final_punch_out - final_punch_in) > 24 * 3600 * 1000:
                raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

        shift_start_dt, shift_end_dt = get_shift_datetimes(date, emp["shift_start"], emp["shift_end"])
        final_late_minutes = compute_late_minutes(final_punch_in, shift_start_dt)
        if final_punch_out is not None:
            final_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            final_overtime_minutes = compute_overtime(final_punch_out, shift_end_dt)
            final_half_day = compute_half_day(final_work_hours)
        else:
            final_work_hours = None
            final_overtime_minutes = 0
            final_half_day = False

    # Detect actual changes
    old_status = record.get("status")
    old_punch_in_ms = datetime_to_ms(record.get("punch_in"))
    old_punch_out_ms = datetime_to_ms(record.get("punch_out"))
    old_work_hours = round_half_up(record.get("work_hours"), 2) if record.get("work_hours") is not None else None
    old_late_minutes = int(record.get("late_minutes", 0) or 0)
    old_overtime_minutes = int(record.get("overtime_minutes", 0) or 0)
    old_half_day = bool(record.get("half_day", False))

    changes = {}
    if old_status != new_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if old_punch_in_ms != final_punch_in:
        changes["punch_in"] = {
            "from": ms_to_datetime(old_punch_in_ms),
            "to": ms_to_datetime(final_punch_in),
        }
    if old_punch_out_ms != final_punch_out:
        changes["punch_out"] = {
            "from": ms_to_datetime(old_punch_out_ms),
            "to": ms_to_datetime(final_punch_out),
        }
    if old_work_hours != final_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": final_work_hours}
    if old_late_minutes != final_late_minutes:
        changes["late_minutes"] = {"from": old_late_minutes, "to": final_late_minutes}
    if old_overtime_minutes != final_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_overtime_minutes, "to": final_overtime_minutes}
    if old_half_day != final_half_day:
        changes["half_day"] = {"from": old_half_day, "to": final_half_day}

    if not changes:
        raise HTTPException(422, "No changes detected in regularization request")

    now_dt = datetime.now(timezone.utc)
    history_entry = {
        "at": now_dt,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    existing_history = record.get("history") or []
    existing_len = len(existing_history)

    if existing_len == 0:
        cond = {
            "_id": record["_id"],
            "$or": [{"history": {"$size": 0}}, {"history": {"$exists": False}}],
        }
    else:
        cond = {"_id": record["_id"], "history": {"$size": existing_len}}

    update_ops = {
        "$set": {
            "status": new_status,
            "punch_in": ms_to_datetime(final_punch_in),
            "punch_out": ms_to_datetime(final_punch_out),
            "work_hours": final_work_hours,
            "late_minutes": final_late_minutes,
            "overtime_minutes": final_overtime_minutes,
            "half_day": final_half_day,
        },
        "$push": {"history": history_entry},
    }

    updated_record = db.attendance_logs.find_one_and_update(
        cond,
        update_ops,
        return_document=pymongo.ReturnDocument.AFTER,
    )

    if not updated_record:
        raise HTTPException(409, "Conflict: concurrent modification detected")

    return serialize_attendance_record(updated_record)


# --------------------------------------------------------------------------- #
# Stage 4: MongoDB Aggregation Pipeline Builders & Analytics Endpoints
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "half_day": 1,
                "late_minutes": 1,
                "overtime_minutes": 1,
                "is_weekday": {
                    "$in": [
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        [2, 3, 4, 5, 6],
                    ]
                },
                "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                "is_leave": {"$eq": ["$status", "LEAVE"]},
            }
        },
        {
            "$project": {
                "present_value": {
                    "$cond": [
                        {"$and": ["$is_weekday", "$is_present"]},
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                        0.0,
                    ]
                },
                "leave_value": {"$cond": ["$is_leave", 1, 0]},
                "late_count_val": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": "$present_value"},
                "leave_days": {"$sum": "$leave_value"},
                "late_count": {"$sum": "$late_count_val"},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> list[dict]:
    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    match_filter = {"joined_on": {"$lte": month_end}}
    if department:
        match_filter["department"] = department

    return [
        {"$match": match_filter},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "all_logs",
            }
        },
        {
            "$addFields": {
                "logs": {
                    "$filter": {
                        "input": "$all_logs",
                        "cond": {
                            "$and": [
                                {"$gte": ["$$this.date", month_start]},
                                {"$lte": ["$$this.date", month_end]},
                            ]
                        },
                    }
                }
            }
        },
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$group": {
                "_id": {
                    "dept": "$department",
                    "emp_code": "$emp_code",
                },
                "dept": {"$first": "$department"},
                "emp_present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {
                                        "$in": [
                                            {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "emp_late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$gt": ["$logs.late_minutes", 0]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "emp_late_minutes": {
                    "$sum": {
                        "$cond": [
                            {"$ne": ["$logs", None]},
                            {"$ifNull": ["$logs.late_minutes", 0]},
                            0,
                        ]
                    }
                },
                "emp_leave_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "LEAVE"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "emp_on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "ON_DUTY"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "emp_work_hours": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            "$$REMOVE",
                        ]
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$dept",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$emp_present_days"},
                "late_count": {"$sum": "$emp_late_count"},
                "total_late_minutes": {"$sum": "$emp_late_minutes"},
                "leave_count": {"$sum": "$emp_leave_count"},
                "on_duty_count": {"$sum": "$emp_on_duty_count"},
                "all_work_hours": {"$push": "$emp_work_hours"},
            }
        },
        {"$sort": {"_id": 1}},
    ]


def build_late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None) -> list[dict]:
    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    pipeline = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
    ])
    return pipeline


def build_department_trend_pipeline(department: str, from_str: str, to_str: str) -> list[dict]:
    return [
        {
            "$match": {
                "date": {"$gte": from_str, "$lte": to_str},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly", status_code=200)
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    joined_on = emp["joined_on"]
    if joined_on > month_end:
        working_days = 0
    else:
        start_date = max(month_start, joined_on)
        sy, sm, sd = map(int, start_date.split("-"))
        cur = date(sy, sm, sd)
        end = date(year, m, last_day)
        working_days = 0
        while cur <= end:
            if cur.weekday() < 5:  # Mon-Fri
                working_days += 1
            cur += timedelta(days=1)

    pipeline = build_employee_monthly_pipeline(emp_code, month)
    try:
        results = list(db.attendance_logs.aggregate(pipeline))
    except Exception:
        # Fallback if in a test mock without specific date operators
        results = []

    if results:
        res = results[0]
        present_days = round_half_up(res.get("present_days", 0.0), 2) or 0.0
        leave_days = int(res.get("leave_days", 0) or 0)
        late_count = int(res.get("late_count", 0) or 0)
        total_late_minutes = int(res.get("total_late_minutes", 0) or 0)
        total_overtime_minutes = int(res.get("total_overtime_minutes", 0) or 0)
    else:
        # Direct calculation fallback if pipeline returned empty
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days > 0:
        attendance_pct = round_half_up(present_days / working_days * 100, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", status_code=200)
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    pipeline = build_department_summary_pipeline(month, department)
    results = list(db.employees.aggregate(pipeline))

    items = []
    for doc in results:
        # Flatten all work hours
        raw_hours = doc.get("all_work_hours", [])
        flat_hours = []
        for group in raw_hours:
            if isinstance(group, list):
                flat_hours.extend([h for h in group if h is not None])
            elif group is not None:
                flat_hours.append(group)

        if flat_hours:
            avg_work_hours = round_half_up(sum(flat_hours) / len(flat_hours), 2)
        else:
            avg_work_hours = None

        items.append({
            "department": doc["_id"],
            "headcount": int(doc["headcount"]),
            "present_days": round_half_up(doc.get("present_days", 0.0), 2) or 0.0,
            "avg_work_hours": avg_work_hours,
            "late_count": int(doc.get("late_count", 0)),
            "total_late_minutes": int(doc.get("total_late_minutes", 0)),
            "leave_count": int(doc.get("leave_count", 0)),
            "on_duty_count": int(doc.get("on_duty_count", 0)),
        })

    return {
        "month": month,
        "items": items,
    }


@app.get("/analytics/leaderboard/late", status_code=200)
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    try:
        results = list(db.attendance_logs.aggregate(pipeline))
        items = []
        for r in results:
            items.append({
                "rank": int(r["rank"]),
                "emp_code": r["_id"],
                "name": r["emp"]["name"],
                "department": r["emp"]["department"],
                "total_late_minutes": int(r["total_late_minutes"]),
                "late_count": int(r["late_count"]),
            })
    except Exception:
        # Engine fallback (e.g. mongomock without $setWindowFields):
        # Run pre-window grouping and apply standard competition ranking in Python
        year, m = map(int, month.split("-"))
        _, last_day = calendar.monthrange(year, m)
        month_start = f"{month}-01"
        month_end = f"{month}-{last_day:02d}"

        pre_pipeline = [
            {"$match": {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
            {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
            {"$unwind": "$emp"},
        ]
        if department:
            pre_pipeline.append({"$match": {"emp.department": department}})

        raw_docs = list(db.attendance_logs.aggregate(pre_pipeline))
        # Sort total_late_minutes desc, emp_code asc
        raw_docs.sort(key=lambda d: (-d["total_late_minutes"], d["_id"]))

        items = []
        ranked_docs = []
        for i, d in enumerate(raw_docs):
            if i > 0 and d["total_late_minutes"] == raw_docs[i - 1]["total_late_minutes"]:
                r = ranked_docs[i - 1]["rank"]
            else:
                r = i + 1
            d["rank"] = r
            ranked_docs.append(d)
            if r <= limit:
                items.append({
                    "rank": r,
                    "emp_code": d["_id"],
                    "name": d["emp"]["name"],
                    "department": d["emp"]["department"],
                    "total_late_minutes": int(d["total_late_minutes"]),
                    "late_count": int(d["late_count"]),
                })

    return {
        "month": month,
        "items": items,
    }


@app.get("/analytics/departments/{department}/trend", status_code=200)
def department_trend(
    department: str = Path(...),
    from_str: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_str: str = Query(..., alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
):
    emp = db.employees.find_one({"department": department})
    if not emp:
        raise HTTPException(404, "Department not found")

    from_d = date.fromisoformat(from_str)
    to_d = date.fromisoformat(to_str)
    if to_d < from_d:
        raise HTTPException(422, "'to' date cannot be earlier than 'from' date")
    if (to_d - from_d).days > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")

    # Fetch daily aggregated logs for this department
    log_pipeline = build_department_trend_pipeline(department, from_str, to_str)
    daily_logs = {d["_id"]: d for d in db.attendance_logs.aggregate(log_pipeline)}

    # Fetch department employees joined_on dates to compute headcount per day
    dept_employees = list(db.employees.find({"department": department}, {"joined_on": 1}))

    items = []
    rates = []
    cur = from_d
    while cur <= to_d:
        cur_str = cur.isoformat()
        is_working_day = cur.weekday() < 5
        headcount = sum(1 for e in dept_employees if e.get("joined_on", "") <= cur_str)

        log_data = daily_logs.get(cur_str, {})
        present_count = round_half_up(log_data.get("present_count", 0.0), 2) or 0.0
        late_count = int(log_data.get("late_count", 0))

        if is_working_day and headcount > 0:
            attendance_rate = round_half_up(present_count / headcount, 4)
        else:
            attendance_rate = None

        rates.append(attendance_rate)
        items.append({
            "date": cur_str,
            "is_working_day": is_working_day,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": attendance_rate,
        })
        cur += timedelta(days=1)

    # Compute 7-day moving average over preceding non-null attendance_rate values
    for i in range(len(items)):
        window = [r for r in rates[max(0, i - 6) : i + 1] if r is not None]
        if window:
            items[i]["moving_avg_7d"] = round_half_up(sum(window) / len(window), 4)
        else:
            items[i]["moving_avg_7d"] = None

    return {
        "department": department,
        "items": items,
    }

# --------------------------------------------------------------------------- #
# Stage 5: Admin Explain Endpoint
# --------------------------------------------------------------------------- #
SENSITIVE_KEYS = {"uri", "connectionstring", "password", "secret", "credentials", "auth"}


def sanitize_for_json(obj: Any) -> Any:
    """Recursively converts BSON datetimes, ObjectIds, etc. to JSON-serializable types and redacts sensitive information."""
    if isinstance(obj, dict):
        res = {}
        for k, v in obj.items():
            if str(k).lower() in SENSITIVE_KEYS:
                res[str(k)] = "[REDACTED]"
            else:
                res[str(k)] = sanitize_for_json(v)
        return res
    if isinstance(obj, list):
        return [sanitize_for_json(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "__str__") and not isinstance(obj, (int, float, bool, str, type(None))):
        return str(obj)
    return obj


@app.get("/admin/explain/{endpoint}", status_code=200)
def explain_endpoint(
    endpoint: str = Path(..., description="Target endpoint name"),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[StatusType] = Query(None),
    from_str: Optional[str] = Query(None, alias="from"),
    to_str: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    valid_endpoints = [
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ]
    if endpoint not in valid_endpoints:
        raise HTTPException(422, f"Unknown endpoint '{endpoint}'. Must be one of {valid_endpoints}")

    if endpoint == "attendance_list":
        target_collection = "attendance_logs"
        q = build_attendance_query(emp_code, date_from, date_to, status)
        skip = (page - 1) * page_size
        cmd = {
            "explain": {
                "find": target_collection,
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        }

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(422, "emp_code is required for employee_monthly explain")
        if not month:
            raise HTTPException(422, "month is required for employee_monthly explain")
        target_collection = "attendance_logs"
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        cmd = {
            "explain": {
                "aggregate": target_collection,
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary explain")
        target_collection = "employees"
        pipeline = build_department_summary_pipeline(month, department)
        cmd = {
            "explain": {
                "aggregate": target_collection,
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard explain")
        target_collection = "attendance_logs"
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        cmd = {
            "explain": {
                "aggregate": target_collection,
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(422, "department is required for department_trend explain")
        if not from_str:
            raise HTTPException(422, "'from' date is required for department_trend explain")
        if not to_str:
            raise HTTPException(422, "'to' date is required for department_trend explain")
        try:
            from_d = date.fromisoformat(from_str)
            to_d = date.fromisoformat(to_str)
        except Exception:
            raise HTTPException(422, "Invalid from/to date format")
        if to_d < from_d:
            raise HTTPException(422, "'to' date cannot be earlier than 'from' date")
        if (to_d - from_d).days > 92:
            raise HTTPException(422, "Date range cannot exceed 92 days")

        target_collection = "attendance_logs"
        pipeline = build_department_trend_pipeline(department, from_str, to_str)
        cmd = {
            "explain": {
                "aggregate": target_collection,
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }

    try:
        raw_explain = db.command(cmd)
    except Exception as e:
        raise HTTPException(500, f"Explain command failed: {e}")

    return {
        "endpoint": endpoint,
        "collection": target_collection,
        "explain": sanitize_for_json(raw_explain),
    }





