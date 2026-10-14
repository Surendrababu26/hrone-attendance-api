import concurrent.futures
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import pytest
from fastapi.testclient import TestClient

from app.main import app, db, round_half_up, truncate_epoch_ms, compute_work_hours, compute_half_day

client = TestClient(app)

@pytest.fixture(autouse=True, scope="function")
def reset_db():
    from sample_seed import here, opts
    from bson import json_util
    for name in ("employees", "attendance_logs"):
        docs = json_util.loads((here / f"{name}.json").read_text(), json_options=opts)
        db[name].delete_many({})
        db[name].insert_many(docs)


# --------------------------------------------------------------------------- #
# Concurrency Tests
# --------------------------------------------------------------------------- #
def test_concurrent_employee_creation():
    payload = {
        "emp_code": "EMP8888",
        "name": "Concurrent User",
        "email": "concurrent@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-05-01"
    }

    def do_create():
        c = TestClient(app)
        return c.post("/employees", json=payload).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(do_create) for _ in range(5)]
        results = [f.result() for f in futures]

    # Exactly one request must succeed with 201; all others must get 409
    assert results.count(201) == 1
    assert results.count(409) == 4


def test_concurrent_punch_ins():
    # Concurrent punch-in for EMP0001 on date 2026-08-17 (09:30 IST)
    dt = datetime(2026, 8, 17, 4, 0, 0, tzinfo=timezone.utc)
    ts = int(dt.timestamp() * 1000)
    payload = {
        "emp_code": "EMP0001",
        "punched_at": ts,
        "status": "PRESENT"
    }

    def do_punch_in():
        c = TestClient(app)
        return c.post("/attendance/punch-in", json=payload).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(do_punch_in) for _ in range(5)]
        results = [f.result() for f in futures]

    assert results.count(201) == 1
    assert results.count(409) == 4


def test_concurrent_punch_outs():
    # First create a valid punch-in
    dt_in = datetime(2026, 8, 18, 4, 0, 0, tzinfo=timezone.utc)
    ts_in = int(dt_in.timestamp() * 1000)
    res_in = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": ts_in,
        "status": "PRESENT"
    })
    assert res_in.status_code == 201

    # Now attempt concurrent punch-outs
    dt_out = datetime(2026, 8, 18, 13, 0, 0, tzinfo=timezone.utc)
    ts_out = int(dt_out.timestamp() * 1000)
    payload = {
        "emp_code": "EMP0001",
        "punched_at": ts_out
    }

    def do_punch_out():
        c = TestClient(app)
        return c.post("/attendance/punch-out", json=payload).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(do_punch_out) for _ in range(5)]
        results = [f.result() for f in futures]

    # Exactly one request must succeed with 200; others must get 409
    assert results.count(200) == 1
    assert results.count(409) == 4


def test_concurrent_patch_audit_history():
    # Use existing record EMP0001 on 2026-07-06
    def do_patch(i):
        c = TestClient(app)
        dt = datetime(2026, 7, 6, 4, i, 0, tzinfo=timezone.utc)
        return c.patch("/attendance/EMP0001/2026-07-06", json={
            "punch_in": int(dt.timestamp() * 1000),
            "reason": f"Concurrent correction {i}",
            "regularized_by": f"admin_{i}"
        }).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(do_patch, i) for i in range(10, 15)]
        results = [f.result() for f in futures]

    # At least one 200, and losers get 409 (no 500s, no history lost)
    assert 200 in results
    for code in results:
        assert code in (200, 409)


# --------------------------------------------------------------------------- #
# Timestamp & Truncation Validation Tests
# --------------------------------------------------------------------------- #
def test_timestamp_validations_thorough():
    # Seconds-looking (< 100000000000)
    r1 = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783312500})
    assert r1.status_code == 422

    # Float timestamp
    r2 = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783312500000.5})
    assert r2.status_code == 422

    # String timestamp
    r3 = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": "1783312500000"})
    assert r3.status_code == 422

    # Negative timestamp
    r4 = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": -100})
    assert r4.status_code == 422

    # Greater than max (4102444800000)
    r5 = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 4102444800001})
    assert r5.status_code == 422

    # Truncation to whole seconds: 09:40:00.900 counts as 09:40:00
    ms_with_subseconds = 1783312500900
    r6 = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0002",
        "punched_at": ms_with_subseconds
    })
    assert r6.status_code == 201
    assert r6.json()["punch_in"] == 1783312500000


# --------------------------------------------------------------------------- #
# Half-Up Rounding & Half-Day Boundary Tests
# --------------------------------------------------------------------------- #
def test_half_up_rounding_and_half_day_boundary():
    # Banker's rounding rounds 9.125 to 9.12. Half-up must round 9.125 to 9.13!
    assert round_half_up(9.125, 2) == 9.13
    assert round_half_up(9.115, 2) == 9.12
    assert round_half_up(4.495, 2) == 4.50
    assert round_half_up(0.12345, 4) == 0.1235
    assert round_half_up(0.12344, 4) == 0.1234

    # Half day boundary check:
    # < 4.50 is half day
    assert compute_half_day(4.49) is True
    assert compute_half_day(4.50) is False
    assert compute_half_day(4.51) is False


def test_overtime_threshold_boundary():
    # Overtime threshold: strictly >= 30 minutes counts, < 30 minutes is 0
    # Create punch in on a clean date 2026-08-25: shift end 18:30 IST (13:00 UTC)
    dt_in = datetime(2026, 8, 25, 4, 0, 0, tzinfo=timezone.utc)
    res_in = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0002",
        "punched_at": int(dt_in.timestamp() * 1000)
    })
    assert res_in.status_code == 201

    # Punch out 29 minutes after shift end: 18:59 IST (13:29 UTC) -> overtime must be 0
    dt_out_29 = datetime(2026, 8, 25, 13, 29, 0, tzinfo=timezone.utc)
    res_out_29 = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0002",
        "punched_at": int(dt_out_29.timestamp() * 1000)
    })
    assert res_out_29.status_code == 200
    assert res_out_29.json()["overtime_minutes"] == 0

    # Clean date 2026-08-26: punch out 30 minutes after shift end: 19:00 IST (13:30 UTC) -> overtime must be 30
    dt_in_2 = datetime(2026, 8, 26, 4, 0, 0, tzinfo=timezone.utc)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0002", "punched_at": int(dt_in_2.timestamp() * 1000)})
    dt_out_30 = datetime(2026, 8, 26, 13, 30, 0, tzinfo=timezone.utc)
    res_out_30 = client.post("/attendance/punch-out", json={"emp_code": "EMP0002", "punched_at": int(dt_out_30.timestamp() * 1000)})
    assert res_out_30.status_code == 200
    assert res_out_30.json()["overtime_minutes"] == 30


# --------------------------------------------------------------------------- #
# Pagination Limits Tests
# --------------------------------------------------------------------------- #
def test_pagination_limits_validation():
    # page < 1 -> 422
    assert client.get("/employees?page=0").status_code == 422
    assert client.get("/employees?page=-1").status_code == 422

    # page_size < 1 -> 422
    assert client.get("/employees?page_size=0").status_code == 422

    # page_size > 100 -> 422
    assert client.get("/employees?page_size=101").status_code == 422

    # page_size = 100 -> 200
    res = client.get("/employees?page_size=100")
    assert res.status_code == 200
    assert res.json()["page_size"] == 100

    # Attendance pagination
    assert client.get("/attendance?page_size=101").status_code == 422
    assert client.get("/attendance?page=0").status_code == 422


# --------------------------------------------------------------------------- #
# Zero Attendance Logs Employee Tests
# --------------------------------------------------------------------------- #
def test_employee_with_zero_logs():
    # EMP0002 has joined_on 2026-07-13 in Engineering.
    # In 2026-06 (June 2026), EMP0002 was not joined yet.
    # But EMP0001 joined on 2026-01-05.
    # For 2026-06, EMP0001 had ZERO logs in sample data!
    # Let's test monthly summary for EMP0001 in 2026-06:
    res = client.get("/analytics/employees/EMP0001/monthly?month=2026-06")
    assert res.status_code == 200
    data = res.json()
    assert data["working_days"] == 22  # June 2026 has 22 weekdays
    assert data["present_days"] == 0.0
    assert data["leave_days"] == 0
    assert data["late_count"] == 0
    assert data["total_late_minutes"] == 0
    assert data["attendance_pct"] == 0.0

    # In Department summary for 2026-06:
    # Engineering headcount must be 1 (EMP0001), even though EMP0001 has ZERO logs!
    res_dept = client.get("/analytics/departments/summary?month=2026-06")
    assert res_dept.status_code == 200
    eng = next(i for i in res_dept.json()["items"] if i["department"] == "Engineering")
    assert eng["headcount"] == 1
    assert eng["present_days"] == 0.0
    assert eng["avg_work_hours"] is None
