"""
Employee Attendance & Analytics API - Production Implementation

Contract: openapi.yaml (v2.0.0)
Data Model: DATA_MODEL.md
Rules: R1 - R10

Run:
    uvicorn app.main:app --port 8000
"""

import calendar
import math
import os
from contextlib import asynccontextmanager
from datetime import date as dt_date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# Environment & MongoDB Setup
# --------------------------------------------------------------------------- #
# Real environment variables take priority over local .env
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# PyMongo client configured with tz_aware=True as specified in DATA_MODEL.md
client: MongoClient = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_db():
    """Create all required indexes idempotently at startup."""
    # employees collection indexes
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="idx_emp_dept_joined")
    db.employees.create_index([("joined_on", ASCENDING)], name="idx_emp_joined")

    # attendance_logs collection indexes
    # Natural unique key: at most one document per (emp_code, date)
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uniq_emp_date"
    )
    # Serves GET /attendance pagination & sorting (date desc, emp_code asc)
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="idx_att_date_desc_emp"
    )
    # Serves punch-out lookup: latest punch_in for an employee
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="idx_att_emp_punch_in"
    )
    # Serves late_leaderboard analytics
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)],
        name="idx_att_date_late_emp"
    )
    # Serves department_trend gap-filling date scanning
    db.attendance_logs.create_index(
        [("date", ASCENDING)],
        name="idx_att_date_asc"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: ensure indexes exist
    try:
        init_db()
    except Exception as e:
        print(f"Warning: Index creation at startup failed or skipped: {e}")
    yield
    # Shutdown
    client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan
)


# --------------------------------------------------------------------------- #
# Math & Rounding Helpers (R4, R8)
# --------------------------------------------------------------------------- #
def round_half_up(value: float, decimals: int = 2) -> float:
    """Half-up rounding per R8 (rates: 4 decimals, others: 2 decimals)."""
    d = Decimal(str(value))
    target = Decimal("10") ** -decimals
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Time Zone & Shift Calculation Helpers (R1 - R5)
# --------------------------------------------------------------------------- #
def truncate_epoch_ms(epoch_ms: int) -> int:
    """
    R1: Instants are truncated to whole seconds before calculation.
    (09:40:00.900 counts as 09:40:00).
    """
    return (epoch_ms // 1000) * 1000


def epoch_ms_to_utc_datetime(epoch_ms: int) -> datetime:
    """Convert truncated epoch ms to UTC timezone-aware datetime."""
    truncated = truncate_epoch_ms(epoch_ms)
    return datetime.fromtimestamp(truncated / 1000, tz=timezone.utc)


def utc_datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert UTC timezone-aware datetime to integer epoch ms."""
    if dt is None:
        return None
    return int(dt.timestamp() * 1000)


def determine_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in instant.
    Exception - overnight shifts (shift_end <= shift_start):
    a punch-in whose IST time is strictly earlier than shift_end belongs to the shift
    that started the previous calendar day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))

    is_overnight = (end_h, end_m) <= (start_h, start_m)

    if is_overnight:
        punch_time = punch_in_ist.time()
        end_time = time(end_h, end_m)
        if punch_time < end_time:
            # Belongs to shift that started previous calendar day
            shift_start_calendar_day = punch_in_ist.date() - timedelta(days=1)
            return shift_start_calendar_day.isoformat()

    return punch_in_ist.date().isoformat()


def get_shift_datetimes(attendance_date_str: str, shift_start: str, shift_end: str):
    """
    Returns (shift_start_dt, shift_end_dt) as timezone-aware IST datetimes
    for a given attendance date.
    """
    date_obj = dt_date.fromisoformat(attendance_date_str)
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))

    start_dt = datetime.combine(date_obj, time(start_h, start_m), tzinfo=IST)
    is_overnight = (end_h, end_m) <= (start_h, start_m)

    if is_overnight:
        end_date = date_obj + timedelta(days=1)
        end_dt = datetime.combine(end_date, time(end_h, end_m), tzinfo=IST)
    else:
        end_dt = datetime.combine(date_obj, time(end_h, end_m), tzinfo=IST)

    return start_dt, end_dt


def compute_late_minutes(punch_in_utc: datetime, attendance_date_str: str, shift_start: str, shift_end: str) -> int:
    """
    R2: Grace period is 10 minutes. A punch-in is late only if strictly more than
    10:00 after shift_start.
    When late: floor minutes elapsed since shift_start (not grace period end). Otherwise 0.
    """
    start_dt, _ = get_shift_datetimes(attendance_date_str, shift_start, shift_end)
    punch_in_ist = punch_in_utc.astimezone(IST)

    grace_end = start_dt + timedelta(minutes=10)
    if punch_in_ist > grace_end:
        elapsed = (punch_in_ist - start_dt).total_seconds()
        return int(math.floor(elapsed / 60))
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_sec = (punch_out_utc - punch_in_utc).total_seconds()
    raw_hours = Decimal(str(diff_sec)) / Decimal("3600")
    return float(raw_hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out_utc: datetime, attendance_date_str: str, shift_start: str, shift_end: str) -> int:
    """
    R3: whole minutes (floor) between shift_end and punch_out, counted only when >= 30; otherwise 0.
    """
    _, end_dt = get_shift_datetimes(attendance_date_str, shift_start, shift_end)
    punch_out_ist = punch_out_utc.astimezone(IST)

    if punch_out_ist > end_dt:
        elapsed = (punch_out_ist - end_dt).total_seconds()
        minutes = int(math.floor(elapsed / 60))
        return minutes if minutes >= 30 else 0
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5: half_day = true when the rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


def count_working_days_in_month(month_str: str, joined_on_str: str) -> int:
    """
    R7: Working days are Mon-Fri (no holiday calendar).
    For employee who joined mid-month, count Mon-Fri from joined_on to month end.
    If joined after month end: 0.
    """
    year, month = map(int, month_str.split("-"))
    _, last_day = calendar.monthrange(year, month)
    month_start_date = dt_date(year, month, 1)
    month_end_date = dt_date(year, month, last_day)

    joined_date = dt_date.fromisoformat(joined_on_str)
    if joined_date > month_end_date:
        return 0

    start_date = max(month_start_date, joined_date)
    count = 0
    cur = start_date
    while cur <= month_end_date:
        if cur.weekday() < 5:  # Mon=0 .. Fri=4
            count += 1
        cur += timedelta(days=1)
    return count


# --------------------------------------------------------------------------- #
# Pydantic Request & Response Models (following openapi.yaml exactly)
# --------------------------------------------------------------------------- #
class StatusEnum(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatusEnum(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str
    punched_at: Optional[int] = Field(
        default=None,
        ge=100000000000,
        le=4102444800000,
        strict=True,
        description="Epoch milliseconds."
    )
    status: PresenceStatusEnum = Field(default=PresenceStatusEnum.PRESENT)


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str
    punched_at: Optional[int] = Field(
        default=None,
        ge=100000000000,
        le=4102444800000,
        strict=True,
        description="Epoch milliseconds."
    )


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Optional[StatusEnum] = None
    punch_in: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000, strict=True)
    punch_out: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000, strict=True)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Document Serializers (Translating BSON to JSON matching OpenAPI contract)
# --------------------------------------------------------------------------- #
def serialize_employee(doc: dict) -> dict:
    created_at = doc.get("created_at")
    if isinstance(created_at, datetime):
        created_at_ms = int(created_at.timestamp() * 1000)
    elif isinstance(created_at, (int, float)):
        created_at_ms = int(created_at)
    else:
        created_at_ms = 0

    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": created_at_ms,
    }


def serialize_attendance(doc: dict) -> dict:
    punch_in = doc.get("punch_in")
    punch_in_ms = int(punch_in.timestamp() * 1000) if isinstance(punch_in, datetime) else None

    punch_out = doc.get("punch_out")
    punch_out_ms = int(punch_out.timestamp() * 1000) if isinstance(punch_out, datetime) else None

    history_list = []
    for h in doc.get("history") or []:
        at = h.get("at")
        at_ms = int(at.timestamp() * 1000) if isinstance(at, datetime) else 0

        changes = {}
        for k, v in (h.get("changes") or {}).items():
            from_val = v.get("from")
            to_val = v.get("to")
            if k in ("punch_in", "punch_out"):
                if isinstance(from_val, datetime):
                    from_val = int(from_val.timestamp() * 1000)
                if isinstance(to_val, datetime):
                    to_val = int(to_val.timestamp() * 1000)
            changes[k] = {"from": from_val, "to": to_val}

        history_list.append({
            "at": at_ms,
            "by": h.get("by", ""),
            "reason": h.get("reason", ""),
            "changes": changes,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": punch_in_ms,
        "punch_out": punch_out_ms,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_list,
    }


# --------------------------------------------------------------------------- #
# System Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    """Readiness probe. Returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"MongoDB unavailable: {str(e)}"
        )


# --------------------------------------------------------------------------- #
# Employee Endpoints
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=status.HTTP_201_CREATED, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """
    Create an employee.
    Enforces emp_code uniqueness at DB level (returns 409 on duplicate key).
    """
    now_utc = datetime.now(timezone.utc)
    # Truncate instant to whole seconds
    now_ms = truncate_epoch_ms(int(now_utc.timestamp() * 1000))
    created_at_dt = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)

    doc = body.model_dump()
    doc["created_at"] = created_at_dt

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="emp_code already exists")

    return serialize_employee(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """List employees sorted by emp_code ascending."""
    query = {}
    if department is not None:
        query["department"] = department

    total = db.employees.count_documents(query)
    skip = (page - 1) * page_size
    cursor = db.employees.find(query).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [serialize_employee(d) for d in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance Endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED, tags=["attendance"])
def punch_in(body: PunchInRequest):
    """
    Record a punch-in.
    Concurrency: handled by unique index on (emp_code, date) returning 409 on conflict.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        punched_at_ms = truncate_epoch_ms(body.punched_at)
    else:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        punched_at_ms = truncate_epoch_ms(now_ms)

    punch_in_dt = epoch_ms_to_utc_datetime(punched_at_ms)

    att_date = determine_attendance_date(
        punch_in_utc=punch_in_dt,
        shift_start=emp["shift_start"],
        shift_end=emp["shift_end"]
    )

    late_min = compute_late_minutes(
        punch_in_utc=punch_in_dt,
        attendance_date_str=att_date,
        shift_start=emp["shift_start"],
        shift_end=emp["shift_end"]
    )

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status.value,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="already punched in for this date"
        )

    return serialize_attendance(doc)


@app.post("/attendance/punch-out", status_code=status.HTTP_200_OK, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """
    Record a punch-out.
    Closes most recent record whose punch_in is at or before punched_at.
    Concurrency: atomic find_one_and_update ensures exactly one 200 and subsequent 409.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        punched_at_ms = truncate_epoch_ms(body.punched_at)
    else:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        punched_at_ms = truncate_epoch_ms(now_ms)

    punch_out_dt = epoch_ms_to_utc_datetime(punched_at_ms)

    # Find employee's most recent record whose punch_in <= punched_at
    candidate = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": punch_out_dt}
        },
        sort=[("punch_in", DESCENDING)]
    )

    if not candidate:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No punch-in found for this employee at or before punched_at"
        )

    if candidate.get("punch_out") is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Record is already punched out"
        )

    punch_in_dt = candidate["punch_in"]
    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    if diff_sec <= 0 or diff_sec > 86400:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punched_at must be strictly after punch_in and at most 24 hours after it"
        )

    att_date = candidate["date"]
    work_h = compute_work_hours(punch_in_dt, punch_out_dt)
    ot_min = compute_overtime(punch_out_dt, att_date, emp["shift_start"], emp["shift_end"])
    is_half = compute_half_day(work_h)

    # Atomic update on punch_out: None to prevent race conditions
    updated_doc = db.attendance_logs.find_one_and_update(
        {
            "_id": candidate["_id"],
            "punch_out": None
        },
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_h,
                "overtime_minutes": ot_min,
                "half_day": is_half,
            }
        },
        return_document=ReturnDocument.AFTER
    )

    if not updated_doc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Record was concurrently punched out"
        )

    return serialize_attendance(updated_doc)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = Query(default=None),
    date_from: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[StatusEnum] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    List attendance records sorted by date descending, then emp_code ascending.
    Streams results from MongoDB with skip and limit to scale to 100k records.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_from must be <= date_to"
        )

    query = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        date_q = {}
        if date_from:
            date_q["$gte"] = date_from
        if date_to:
            date_q["$lte"] = date_to
        query["date"] = date_q
    if status:
        query["status"] = status.value

    total = db.attendance_logs.count_documents(query)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(query).sort(
        [("date", DESCENDING), ("emp_code", ASCENDING)]
    ).skip(skip).limit(page_size)

    items = [serialize_attendance(d) for d in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    """
    Regularize (manually correct) one day's record.
    Recomputes derived fields and atomically appends one history entry.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attendance record not found")

    target_status = body.status.value if body.status is not None else rec["status"]

    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Cannot supply punch_in or punch_out when setting status to ABSENT or LEAVE"
            )
        new_punch_in_dt = None
        new_punch_out_dt = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Presence status
        if body.punch_in is not None:
            new_punch_in_ms = truncate_epoch_ms(body.punch_in)
            new_punch_in_dt = epoch_ms_to_utc_datetime(new_punch_in_ms)
        else:
            new_punch_in_dt = rec.get("punch_in")

        if new_punch_in_dt is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Presence status requires a punch_in"
            )

        # Validate punch_in stays on record's attendance date per R1
        calc_date = determine_attendance_date(new_punch_in_dt, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"punch_in belongs to attendance date {calc_date}, not {date}"
            )

        if body.punch_out is not None:
            new_punch_out_ms = truncate_epoch_ms(body.punch_out)
            new_punch_out_dt = epoch_ms_to_utc_datetime(new_punch_out_ms)
        else:
            new_punch_out_dt = rec.get("punch_out")

        if new_punch_out_dt is not None:
            diff_sec = (new_punch_out_dt - new_punch_in_dt).total_seconds()
            if diff_sec <= 0 or diff_sec > 86400:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out must be after punch_in and within 24 hours"
                )
            new_work_hours = compute_work_hours(new_punch_in_dt, new_punch_out_dt)
            new_overtime_minutes = compute_overtime(new_punch_out_dt, date, emp["shift_start"], emp["shift_end"])
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

        new_late_minutes = compute_late_minutes(new_punch_in_dt, date, emp["shift_start"], emp["shift_end"])

    # Build changes map (only actually changed fields)
    changes = {}

    def compare_and_add(field: str, old_val: Any, new_val: Any):
        if old_val != new_val:
            changes[field] = {"from": old_val, "to": new_val}

    compare_and_add("status", rec.get("status"), target_status)
    compare_and_add("punch_in", rec.get("punch_in"), new_punch_in_dt)
    compare_and_add("punch_out", rec.get("punch_out"), new_punch_out_dt)
    compare_and_add("work_hours", rec.get("work_hours"), new_work_hours)
    compare_and_add("late_minutes", rec.get("late_minutes", 0), new_late_minutes)
    compare_and_add("overtime_minutes", rec.get("overtime_minutes", 0), new_overtime_minutes)
    compare_and_add("half_day", rec.get("half_day", False), new_half_day)

    if not changes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Request changes nothing"
        )

    now_utc = datetime.now(timezone.utc)
    now_ms = truncate_epoch_ms(int(now_utc.timestamp() * 1000))
    history_at_dt = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)

    history_entry = {
        "at": history_at_dt,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Atomic update ensuring history length check prevents lost updates
    current_history_len = len(rec.get("history") or [])
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            f"history.{current_history_len}": {"$exists": False}
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": new_punch_in_dt,
                "punch_out": new_punch_out_dt,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {
                "history": history_entry
            }
        },
        return_document=ReturnDocument.AFTER
    )

    if not updated:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Concurrent modification conflict on attendance history"
        )

    return serialize_attendance(updated)


# --------------------------------------------------------------------------- #
# Analytics Endpoints (Implemented as MongoDB Aggregations)
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """
    Monthly attendance summary for one employee.
    Backed by MongoDB aggregation on attendance_logs.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    working_days = count_working_days_in_month(month, emp["joined_on"])

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$in": [
                                            {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                                            [2, 3, 4, 5, 6]  # Mon-Fri
                                        ]
                                    }
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        }
    ]

    agg_result = list(db.attendance_logs.aggregate(pipeline))
    stats = agg_result[0] if agg_result else {
        "present_days": 0.0,
        "leave_days": 0,
        "late_count": 0,
        "total_late_minutes": 0,
        "total_overtime_minutes": 0,
    }

    present_days = round_half_up(stats["present_days"], 2)
    if working_days > 0:
        raw_pct = (Decimal(str(present_days)) / Decimal(str(working_days))) * Decimal("100")
        attendance_pct = float(raw_pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": stats["leave_days"],
        "late_count": stats["late_count"],
        "total_late_minutes": stats["total_late_minutes"],
        "total_overtime_minutes": stats["total_overtime_minutes"],
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(default=None),
):
    """
    Department summary for a month.
    Aggregates from employees across to attendance_logs.
    Headcount includes employees with zero logs.
    """
    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    match_emp = {"joined_on": {"$lte": month_end}}
    if department:
        match_emp["department"] = department

    pipeline = [
        {"$match": match_emp},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "all_logs": {"$push": "$logs"}
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "logs": {
                    "$reduce": {
                        "input": "$all_logs",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]}
                    }
                }
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {
                                                "$in": [
                                                    {"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}},
                                                    [2, 3, 4, 5, 6]  # Mon-Fri
                                                ]
                                            }
                                        ]
                                    },
                                    {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                    0.0
                                ]
                            }
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": ["$$l.late_minutes", 0]}
                        }
                    }
                },
                "total_late_minutes": {"$sum": "$logs.late_minutes"},
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]}
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]}
                        }
                    }
                },
                "avg_work_hours": {
                    "$avg": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "l",
                                    "cond": {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$ne": ["$$l.work_hours", None]}
                                        ]
                                    }
                                }
                            },
                            "as": "fl",
                            "in": "$$fl.work_hours"
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": {"$round": ["$present_days", 2]},
                "avg_work_hours": {
                    "$cond": [
                        {"$ne": ["$avg_work_hours", None]},
                        {"$round": ["$avg_work_hours", 2]},
                        None
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": ASCENDING}}
    ]

    items = list(db.employees.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = Query(default=None),
):
    """
    Late-comers leaderboard for a month.
    Uses MongoDB $rank in $setWindowFields for standard competition ranking (1, 2, 2, 4).
    Limit applies after ranking.
    """
    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    pipeline = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"}
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {
            "$match": {"rank": {"$lte": limit}}
        },
        {
            "$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}
        }
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """
    Daily attendance trend for a department.
    Gap-filling performed inside MongoDB using $densify and moving average via $setWindowFields.
    """
    # Verify department exists
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown department: {department}"
        )

    f_dt = dt_date.fromisoformat(from_date)
    t_dt = dt_date.fromisoformat(to_date)
    if t_dt < f_dt:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'to' date must be on or after 'from' date"
        )

    span_days = (t_dt - f_dt).days + 1
    if span_days > 92:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Date range exceeds maximum of 92 days"
        )

    start_bson_dt = datetime.fromisoformat(f"{from_date}T00:00:00+00:00")
    end_bson_dt = datetime.fromisoformat(f"{to_date}T00:00:00+00:00") + timedelta(days=1)

    pipeline = [
        {"$documents": [{"date_dt": start_bson_dt}]},
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_bson_dt, end_bson_dt]
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "is_working_day": {
                    "$in": [{"$dayOfWeek": "$date_dt"}, [2, 3, 4, 5, 6]]
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day_str": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$day_str"]}
                                ]
                            }
                        }
                    },
                    {"$count": "count"}
                ],
                "as": "hc"
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day_str": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$date", "$$day_str"]}
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "emp"
                        }
                    },
                    {"$unwind": "$emp"},
                    {
                        "$match": {"emp.department": department}
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                        0.0
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": {"$ifNull": [{"$first": "$hc.count"}, 0]},
                "present_count": {"$ifNull": [{"$first": "$logs.present_count"}, 0.0]},
                "late_count": {"$ifNull": [{"$first": "$logs.late_count"}, 0]},
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]},
                        None
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"]
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d", None]},
                        {"$round": ["$moving_avg_7d", 4]},
                        None
                    ]
                }
            }
        },
        {"$sort": {"date": ASCENDING}}
    ]

    items = list(db.aggregate(pipeline))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = Query(default=None),
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(default=None),
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    status: Optional[StatusEnum] = Query(default=None),
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    MongoDB explain with executionStats verbosity for an endpoint's main query or aggregation.
    Validates required parameters identically to the explained endpoint.
    """
    valid_endpoints = [
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend"
    ]
    if endpoint not in valid_endpoints:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid endpoint for explain. Must be one of: {valid_endpoints}"
        )

    if endpoint == "attendance_list":
        collection_name = "attendance_logs"
        find_query = {}
        if emp_code:
            find_query["emp_code"] = emp_code
        if date_from or date_to:
            dq = {}
            if date_from:
                dq["$gte"] = date_from
            if date_to:
                dq["$lte"] = date_to
            find_query["date"] = dq
        if status:
            find_query["status"] = status.value

        skip = (page - 1) * page_size
        command = {
            "find": "attendance_logs",
            "filter": find_query,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size
        }
        explain_res = db.command("explain", command, verbosity="executionStats")

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="emp_code and month are required for employee_monthly"
            )
        collection_name = "attendance_logs"
        year, m = map(int, month.split("-"))
        _, last_day = calendar.monthrange(year, m)
        m_start = f"{month}-01"
        m_end = f"{month}-{last_day:02d}"

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": m_start, "$lte": m_end}
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "present_days": {"$sum": 1}
                }
            }
        ]
        command = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {}
        }
        explain_res = db.command("explain", command, verbosity="executionStats")

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="month is required for department_summary"
            )
        collection_name = "employees"
        year, m = map(int, month.split("-"))
        _, last_day = calendar.monthrange(year, m)
        m_end = f"{month}-{last_day:02d}"

        match_emp = {"joined_on": {"$lte": m_end}}
        if department:
            match_emp["department"] = department

        pipeline = [
            {"$match": match_emp},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "logs"
                }
            }
        ]
        command = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {}
        }
        explain_res = db.command("explain", command, verbosity="executionStats")

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="month is required for late_leaderboard"
            )
        collection_name = "attendance_logs"
        year, m = map(int, month.split("-"))
        _, last_day = calendar.monthrange(year, m)
        m_start = f"{month}-01"
        m_end = f"{month}-{last_day:02d}"

        pipeline = [
            {
                "$match": {
                    "date": {"$gte": m_start, "$lte": m_end},
                    "late_minutes": {"$gt": 0}
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"}
                }
            }
        ]
        command = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {}
        }
        explain_res = db.command("explain", command, verbosity="executionStats")

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="department, from, and to are required for department_trend"
            )
        collection_name = "attendance_logs"
        pipeline = [
            {
                "$match": {
                    "date": {"$gte": from_date, "$lte": to_date}
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "emp"
                }
            }
        ]
        command = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {}
        }
        explain_res = db.command("explain", command, verbosity="executionStats")

    # Convert non-serializable BSON types to JSON serializable objects
    def sanitize(obj):
        if isinstance(obj, dict):
            return {k: sanitize(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [sanitize(v) for v in obj]
        elif isinstance(obj, datetime):
            return obj.isoformat()
        elif hasattr(obj, "__str__") and type(obj).__module__.startswith("bson"):
            return str(obj)
        return obj

    return {
        "endpoint": endpoint,
        "collection": collection_name,
        "explain": sanitize(explain_res),
    }
