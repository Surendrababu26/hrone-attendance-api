import os
import pytest
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app.main import app, db, truncate_epoch_ms, determine_attendance_date, compute_late_minutes, compute_work_hours, compute_overtime

client = TestClient(app)

@pytest.fixture(autouse=True, scope="function")
def setup_database():
    """Seed test data before each test."""
    from sample_seed import here, opts
    from bson import json_util

    for name in ("employees", "attendance_logs"):
        docs = json_util.loads((here / f"{name}.json").read_text(), json_options=opts)
        db[name].delete_many({})
        db[name].insert_many(docs)


# --------------------------------------------------------------------------- #
# System Tests
# --------------------------------------------------------------------------- #
def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# --------------------------------------------------------------------------- #
# Employee Tests
# --------------------------------------------------------------------------- #
def test_create_employee():
    payload = {
        "emp_code": "EMP9999",
        "name": "Test User",
        "email": "test@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-06-01"
    }
    res = client.post("/employees", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP9999"
    assert "_id" not in data
    assert isinstance(data["created_at"], int)
    assert data["created_at"] > 100000000000

    # Duplicate create must return 409
    res_dup = client.post("/employees", json=payload)
    assert res_dup.status_code == 409

    # Shift start == shift end must return 422
    bad_payload = payload.copy()
    bad_payload["emp_code"] = "EMP9998"
    bad_payload["shift_end"] = "09:30"
    res_bad = client.post("/employees", json=bad_payload)
    assert res_bad.status_code == 422


def test_list_employees():
    res = client.get("/employees?page=1&page_size=3")
    assert res.status_code == 200
    data = res.json()
    assert "items" in data
    assert "total" in data
    assert len(data["items"]) <= 3
    assert data["page"] == 1
    # Check sorted ascending by emp_code
    codes = [item["emp_code"] for item in data["items"]]
    assert codes == sorted(codes)

    # Filter by department
    res_dept = client.get("/employees?department=Sales")
    assert res_dept.status_code == 200
    for item in res_dept.json()["items"]:
        assert item["department"] == "Sales"


# --------------------------------------------------------------------------- #
# Attendance Punch-in & Punch-out Tests
# --------------------------------------------------------------------------- #
def test_punch_in_and_out():
    # Punch in for EMP0001 on 2026-08-03 (Monday) at 09:45 IST
    # 09:45 IST = 04:15 UTC -> 2026-08-03 04:15:00 UTC
    dt_in = datetime(2026, 8, 3, 4, 15, 0, tzinfo=timezone.utc)
    ts_in = int(dt_in.timestamp() * 1000)

    res_in = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": ts_in,
        "status": "PRESENT"
    })
    assert res_in.status_code == 201
    in_data = res_in.json()
    assert in_data["emp_code"] == "EMP0001"
    assert in_data["date"] == "2026-08-03"
    assert in_data["punch_in"] == ts_in
    assert in_data["punch_out"] is None
    # 09:45 is 15 minutes after 09:30 (shift_start). Grace period is 10 min. Late = 15.
    assert in_data["late_minutes"] == 15
    assert "_id" not in in_data
    assert "id" not in in_data

    # Duplicate punch-in for same date must return 409
    res_dup = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": ts_in,
        "status": "PRESENT"
    })
    assert res_dup.status_code == 409

    # Punch out at 19:15 IST (13:45 UTC) -> Overtime: shift end 18:30 -> 45 min overtime (>= 30)
    dt_out = datetime(2026, 8, 3, 13, 45, 0, tzinfo=timezone.utc)
    ts_out = int(dt_out.timestamp() * 1000)

    res_out = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": ts_out
    })
    assert res_out.status_code == 200
    out_data = res_out.json()
    assert out_data["punch_out"] == ts_out
    assert out_data["work_hours"] == 9.5
    assert out_data["overtime_minutes"] == 45
    assert out_data["half_day"] is False

    # Second punch-out on same record must return 409
    res_out_dup = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": ts_out + 1000
    })
    assert res_out_dup.status_code == 409


def test_punch_in_validation():
    # Unknown employee -> 404
    res = client.post("/attendance/punch-in", json={"emp_code": "UNKNOWN99"})
    assert res.status_code == 404

    # Invalid status (e.g. ABSENT not allowed on punch-in) -> 422
    res = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "ABSENT"})
    assert res.status_code == 422

    # Float punched_at -> 422
    res = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783312500000.5})
    assert res.status_code == 422

    # Seconds-looking punched_at (< 100000000000) -> 422
    res = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783312500})
    assert res.status_code == 422


# --------------------------------------------------------------------------- #
# R1 & R2 Boundary Tests (Overnight, Grace period)
# --------------------------------------------------------------------------- #
def test_grace_period_boundaries():
    # Shift 09:30 IST. Grace period is 10 minutes.
    # 09:40:00 -> late_minutes = 0
    # 09:40:01 -> late_minutes = 10
    # 10:15:59 -> late_minutes = 45
    # Let's test using EMP0002 on fresh dates:
    # 2026-08-10 at 09:40:00 IST = 04:10:00 UTC
    dt_40_00 = datetime(2026, 8, 10, 4, 10, 0, tzinfo=timezone.utc)
    res1 = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0002",
        "punched_at": int(dt_40_00.timestamp() * 1000)
    })
    assert res1.status_code == 201
    assert res1.json()["late_minutes"] == 0

    # 2026-08-11 at 09:40:01 IST = 04:10:01 UTC
    dt_40_01 = datetime(2026, 8, 11, 4, 10, 1, tzinfo=timezone.utc)
    res2 = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0002",
        "punched_at": int(dt_40_01.timestamp() * 1000)
    })
    assert res2.status_code == 201
    assert res2.json()["late_minutes"] == 10

    # 2026-08-12 at 10:15:59 IST = 04:45:59 UTC
    dt_15_59 = datetime(2026, 8, 12, 4, 45, 59, tzinfo=timezone.utc)
    res3 = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0002",
        "punched_at": int(dt_15_59.timestamp() * 1000)
    })
    assert res3.status_code == 201
    assert res3.json()["late_minutes"] == 45


def test_overnight_shift_punch_in_and_out():
    # EMP0005 has shift 22:00 to 06:00
    # Punch in on July 21 02:30 IST (strictly before 06:00 shift end)
    # Belongs to shift that started July 20!
    # 2026-07-21 02:30 IST = 2026-07-20 21:00 UTC
    dt_in = datetime(2026, 7, 20, 21, 0, 0, tzinfo=timezone.utc)
    res_in = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0005",
        "punched_at": int(dt_in.timestamp() * 1000)
    })
    assert res_in.status_code == 201
    assert res_in.json()["date"] == "2026-07-20"


# --------------------------------------------------------------------------- #
# Regularize (PATCH) Tests
# --------------------------------------------------------------------------- #
def test_regularize_attendance():
    # Regularize existing seeded record EMP0001 on 2026-07-06
    # Original punch_in was 09:28 IST (03:58 UTC). Change punch_in to 10:05 IST (04:35 UTC) -> late_minutes = 35
    dt_new_in = datetime(2026, 7, 6, 4, 35, 0, tzinfo=timezone.utc)
    ts_new_in = int(dt_new_in.timestamp() * 1000)

    res = client.patch("/attendance/EMP0001/2026-07-06", json={
        "punch_in": ts_new_in,
        "reason": "Biometric device scanner lagged",
        "regularized_by": "hr_manager"
    })
    assert res.status_code == 200
    data = res.json()
    assert data["late_minutes"] == 35
    assert len(data["history"]) == 1
    h = data["history"][0]
    assert h["by"] == "hr_manager"
    assert h["reason"] == "Biometric device scanner lagged"
    assert "late_minutes" in h["changes"]
    assert h["changes"]["late_minutes"]["from"] == 0
    assert h["changes"]["late_minutes"]["to"] == 35

    # Changing nothing returns 422
    res_no_change = client.patch("/attendance/EMP0001/2026-07-06", json={
        "punch_in": ts_new_in,
        "reason": "Trying to submit duplicate change",
        "regularized_by": "hr_manager"
    })
    assert res_no_change.status_code == 422


# --------------------------------------------------------------------------- #
# Analytics Tests
# --------------------------------------------------------------------------- #
def test_employee_monthly_analytics():
    # EMP0001 for 2026-07
    res = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 23
    assert data["present_days"] == 2.0
    assert data["late_count"] == 0
    assert data["attendance_pct"] == 8.7


def test_department_summary():
    res = client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    assert len(data["items"]) >= 3
    # Check departments sorted
    depts = [i["department"] for i in data["items"]]
    assert depts == sorted(depts)
    eng = next(i for i in data["items"] if i["department"] == "Engineering")
    assert eng["headcount"] == 2
    assert eng["present_days"] == 3.0
    assert eng["avg_work_hours"] == 9.04


def test_late_leaderboard_competition_ranking():
    # Insert two tied employees with 40 late minutes, and one with 50
    db.attendance_logs.insert_many([
        {
            "emp_code": "EMP0001",
            "date": "2026-09-01",
            "status": "PRESENT",
            "punch_in": datetime(2026, 9, 1, 4, 30, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 9, 1, 13, 0, tzinfo=timezone.utc),
            "late_minutes": 50,
            "overtime_minutes": 0,
            "work_hours": 8.5,
            "half_day": False,
            "history": []
        },
        {
            "emp_code": "EMP0002",
            "date": "2026-09-01",
            "status": "PRESENT",
            "punch_in": datetime(2026, 9, 1, 4, 20, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 9, 1, 13, 0, tzinfo=timezone.utc),
            "late_minutes": 40,
            "overtime_minutes": 0,
            "work_hours": 8.5,
            "half_day": False,
            "history": []
        },
        {
            "emp_code": "EMP0003",
            "date": "2026-09-01",
            "status": "PRESENT",
            "punch_in": datetime(2026, 9, 1, 4, 20, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 9, 1, 13, 0, tzinfo=timezone.utc),
            "late_minutes": 40,
            "overtime_minutes": 0,
            "work_hours": 8.5,
            "half_day": False,
            "history": []
        },
        {
            "emp_code": "EMP0004",
            "date": "2026-09-01",
            "status": "PRESENT",
            "punch_in": datetime(2026, 9, 1, 4, 10, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 9, 1, 13, 0, tzinfo=timezone.utc),
            "late_minutes": 10,
            "overtime_minutes": 0,
            "work_hours": 8.5,
            "half_day": False,
            "history": []
        }
    ])

    res = client.get("/analytics/leaderboard/late?month=2026-09&limit=2")
    assert res.status_code == 200
    items = res.json()["items"]
    # Rank 1: EMP0001 (50)
    # Rank 2: EMP0002 (40) and EMP0003 (40) tied at rank 2!
    # Because limit=2 applies after ranking (rank <= 2), BOTH tied employees at rank 2 are included!
    # Total rows = 3 (EMP0001, EMP0002, EMP0003), next rank (EMP0004) would be 4!
    assert len(items) == 3
    assert items[0]["emp_code"] == "EMP0001"
    assert items[0]["rank"] == 1
    assert items[1]["rank"] == 2
    assert items[2]["rank"] == 2
    # Tied rows ordered by emp_code ascending
    assert items[1]["emp_code"] == "EMP0002"
    assert items[2]["emp_code"] == "EMP0003"


def test_department_trend():
    res = client.get("/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-14")
    assert res.status_code == 200
    data = res.json()
    assert data["department"] == "Engineering"
    assert len(data["items"]) == 9  # exactly 9 calendar days
    # Weekends have is_working_day = False and attendance_rate = null
    july11 = next(i for i in data["items"] if i["date"] == "2026-07-11")
    assert july11["is_working_day"] is False
    assert july11["attendance_rate"] is None


def test_admin_explain():
    endpoints = [
        ("attendance_list", {"date_from": "2026-07-01", "date_to": "2026-07-31"}),
        ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
        ("department_summary", {"month": "2026-07"}),
        ("late_leaderboard", {"month": "2026-07"}),
        ("department_trend", {"department": "Engineering", "from": "2026-07-06", "to": "2026-07-14"}),
    ]
    for ep, params in endpoints:
        res = client.get(f"/admin/explain/{ep}", params=params)
        assert res.status_code == 200, f"Explain failed for {ep}: {res.text}"
        data = res.json()
        assert data["endpoint"] == ep
        assert "collection" in data
        assert "explain" in data
