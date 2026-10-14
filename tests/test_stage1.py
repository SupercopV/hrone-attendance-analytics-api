"""Unit tests for Stage 1: Helpers, Rules R1-R5, R8, and Pydantic Schemas."""
import pytest
from datetime import datetime, timezone, timedelta
from pydantic import ValidationError
from app.main import (
    truncate_ms,
    round_half_up,
    ms_to_datetime,
    datetime_to_ms,
    determine_attendance_date,
    get_shift_datetimes,
    compute_late_minutes,
    compute_overtime,
    compute_work_hours,
    compute_half_day,
    EmployeeIn,
    PunchInIn,
    IST,
)


def test_truncate_ms():
    # 09:40:00.900 ms should truncate to 09:40:00.000 ms
    assert truncate_ms(1783312500999) == 1783312500000
    assert truncate_ms(1783312500000) == 1783312500000


def test_round_half_up():
    assert round_half_up(4.495, 2) == 4.50
    assert round_half_up(4.494, 2) == 4.49
    assert round_half_up(2.5, 0) == 3.0
    assert round_half_up(0.12345, 4) == 0.1235
    assert round_half_up(0.12344, 4) == 0.1234
    assert round_half_up(None, 2) is None


def test_datetime_conversions():
    ms = 1783312500000
    dt = ms_to_datetime(ms)
    assert dt.tzinfo == timezone.utc
    assert datetime_to_ms(dt) == ms


def test_r1_attendance_date_regular_shift():
    # Regular shift 09:30 - 18:30
    # 2026-07-06 09:28 IST = 2026-07-06 03:58 UTC
    dt = datetime(2026, 7, 6, 9, 28, 0, tzinfo=IST)
    ms = int(dt.timestamp() * 1000)
    att_date = determine_attendance_date(ms, "09:30", "18:30")
    assert att_date == "2026-07-06"


def test_r1_attendance_date_overnight_shift():
    # Overnight shift 22:00 - 06:00
    # Punch in 21:55 on 2026-07-06 IST -> attendance date is 2026-07-06
    dt1 = datetime(2026, 7, 6, 21, 55, 0, tzinfo=IST)
    ms1 = int(dt1.timestamp() * 1000)
    assert determine_attendance_date(ms1, "22:00", "06:00") == "2026-07-06"

    # Punch in 00:30 on 2026-07-07 IST -> belongs to previous day's shift 2026-07-06
    dt2 = datetime(2026, 7, 7, 0, 30, 0, tzinfo=IST)
    ms2 = int(dt2.timestamp() * 1000)
    assert determine_attendance_date(ms2, "22:00", "06:00") == "2026-07-06"

    # Punch in 05:59 on 2026-07-07 IST -> earlier than shift_end 06:00 -> previous day 2026-07-06
    dt3 = datetime(2026, 7, 7, 5, 59, 0, tzinfo=IST)
    ms3 = int(dt3.timestamp() * 1000)
    assert determine_attendance_date(ms3, "22:00", "06:00") == "2026-07-06"

    # Punch in 06:01 on 2026-07-07 IST -> not earlier than shift_end -> calendar date 2026-07-07
    dt4 = datetime(2026, 7, 7, 6, 1, 0, tzinfo=IST)
    ms4 = int(dt4.timestamp() * 1000)
    assert determine_attendance_date(ms4, "22:00", "06:00") == "2026-07-07"


def test_r2_late_minutes():
    # Shift 09:30:
    # 09:40:00 gives 0; 09:40:01 gives 10; 10:15:59 gives 45.
    shift_start_dt, _ = get_shift_datetimes("2026-07-06", "09:30", "18:30")

    # 09:40:00 IST -> 10 minutes (within grace period of 10:00) -> 0
    t0 = datetime(2026, 7, 6, 9, 40, 0, tzinfo=IST)
    assert compute_late_minutes(int(t0.timestamp() * 1000), shift_start_dt) == 0

    # 09:40:01 IST -> strictly more than 10:00 -> floor minutes since 09:30 is 10
    t1 = datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST)
    assert compute_late_minutes(int(t1.timestamp() * 1000), shift_start_dt) == 10

    # 10:15:59 IST -> 45 minutes 59 seconds -> floor minutes since 09:30 is 45
    t2 = datetime(2026, 7, 6, 10, 15, 59, tzinfo=IST)
    assert compute_late_minutes(int(t2.timestamp() * 1000), shift_start_dt) == 45

    # Early punch-in (09:15) -> 0
    t3 = datetime(2026, 7, 6, 9, 15, 0, tzinfo=IST)
    assert compute_late_minutes(int(t3.timestamp() * 1000), shift_start_dt) == 0


def test_r3_overtime():
    # Shift end 18:30
    _, shift_end_dt = get_shift_datetimes("2026-07-06", "09:30", "18:30")

    # Punch out 18:35 (5 min) -> under 30 min minimum -> 0
    out1 = datetime(2026, 7, 6, 18, 35, 0, tzinfo=IST)
    assert compute_overtime(int(out1.timestamp() * 1000), shift_end_dt) == 0

    # Punch out 18:59 (29 min) -> under 30 min minimum -> 0
    out2 = datetime(2026, 7, 6, 18, 59, 59, tzinfo=IST)
    assert compute_overtime(int(out2.timestamp() * 1000), shift_end_dt) == 0

    # Punch out 19:00 (30 min) -> exactly 30 min -> 30
    out3 = datetime(2026, 7, 6, 19, 0, 0, tzinfo=IST)
    assert compute_overtime(int(out3.timestamp() * 1000), shift_end_dt) == 30

    # Punch out 19:10 (40 min) -> 40 min -> 40
    out4 = datetime(2026, 7, 6, 19, 10, 0, tzinfo=IST)
    assert compute_overtime(int(out4.timestamp() * 1000), shift_end_dt) == 40


def test_r4_work_hours_and_r5_half_day():
    # 03:58:00 UTC to 13:05:00 UTC (Sample 1) -> 9.12 hours
    in_dt = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    out_dt = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
    wh = compute_work_hours(int(in_dt.timestamp() * 1000), int(out_dt.timestamp() * 1000))
    assert wh == 9.12
    assert compute_half_day(wh) is False

    # 3.5 hours worked -> half day is True
    out_half = in_dt + timedelta(hours=3, minutes=30)
    wh_half = compute_work_hours(int(in_dt.timestamp() * 1000), int(out_half.timestamp() * 1000))
    assert wh_half == 3.50
    assert compute_half_day(wh_half) is True

    # 4.495 hours -> rounded 4.50 -> half day is False (< 4.50 is false)
    in_ms = 1783312500000
    out_ms = in_ms + int(4.495 * 3600 * 1000)
    wh_border = compute_work_hours(in_ms, out_ms)
    assert wh_border == 4.50
    assert compute_half_day(wh_border) is False


def test_employee_schema_validation():
    # Valid
    emp = EmployeeIn(
        emp_code="EMP0001",
        name="Asha Rao",
        email="asha@example.com",
        department="Engineering",
        joined_on="2026-01-05",
    )
    assert emp.emp_code == "EMP0001"

    # Shift start equal to shift end -> invalid
    with pytest.raises(ValidationError):
        EmployeeIn(
            emp_code="EMP0002",
            name="Test",
            email="test@example.com",
            department="HR",
            shift_start="09:00",
            shift_end="09:00",
            joined_on="2026-01-05",
        )

    # Bad emp_code pattern
    with pytest.raises(ValidationError):
        EmployeeIn(
            emp_code="BADCODE",
            name="Test",
            email="test@example.com",
            department="HR",
            joined_on="2026-01-05",
        )


def test_punch_in_instant_validation():
    # Seconds-looking value (< 10^11) must be rejected
    with pytest.raises(ValidationError):
        PunchInIn(emp_code="EMP0001", punched_at=1783312500)

    # Float must be rejected (strict int)
    with pytest.raises(ValidationError):
        PunchInIn(emp_code="EMP0001", punched_at=1783312500000.5)

    # Valid epoch ms
    p = PunchInIn(emp_code="EMP0001", punched_at=1783312500000)
    assert p.punched_at == 1783312500000
