# Employee Attendance & Analytics API

Backend service built for the HROne Engineering Assignment implementing employee attendance tracking, regularization, audit history, and reporting analytics using **FastAPI** and **MongoDB**.

All application logic lives in `app/main.py` following the contract defined in `openapi.yaml` and the schema in `DATA_MODEL.md`.

---

## Prerequisites

- **Python**: 3.11+ (Tested on Python 3.13.7)
- **MongoDB**: 6.0 or newer (Tested on MongoDB Server 9.0.2 / compatible with MongoDB 7.0)
- **Git**

---

## Configuration

The application reads configuration from environment variables (local `.env` supported, but system environment variables take priority):

| Variable | Description | Default |
|---|---|---|
| `MONGO_URI` | MongoDB connection URI | `mongodb://localhost:27017` |
| `MONGO_DB` | MongoDB database name | `attendance_db` |

---

## Local Setup & Run

### 1. Windows (PowerShell)

```powershell
# Create virtual environment
python -m venv .venv

# Activate virtual environment
.\.venv\Scripts\Activate.ps1

# Install required dependencies
pip install -r requirements.txt

# (Optional) Install test packages if running automated test suite
pip install pytest httpx

# Seed sample data into MongoDB
python sample_seed.py

# Start API server
uvicorn app.main:app --port 8000 --reload
```

### 2. Linux / macOS (Bash)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```

---

## Testing

Run the automated test suite with pytest:

```powershell
pytest -o pythonpath=. -v
```

All 13 test suites verify:
- Readiness probe (`/health`)
- Employee creation, unique business code constraints, shift validation, pagination
- Punch-in and punch-out mechanics, whole-second truncation, overnight shifts, 24-hour limit
- Concurrent punch-in (409) and punch-out race conditions (409)
- Regularization (PATCH) with audit history tracking and concurrent modification safety
- Monthly analytics, department summary with zero-log headcount, competition ranking ties, daily trends with `$densify`, and query plans with `IXSCAN` via `/admin/explain/{endpoint}`.

---

## API Documentation

When the server is running, interactive API docs are available at:
- Swagger UI: `http://localhost:8000/docs`
- OpenAPI JSON: `http://localhost:8000/openapi.json`

---

## Endpoints Overview

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Readiness probe (pings MongoDB) |
| `POST` | `/employees` | Create employee (unique `emp_code`, validated shifts) |
| `GET` | `/employees` | List employees (paginated, sorted by `emp_code` asc) |
| `POST` | `/attendance/punch-in` | Punch-in (calculates IST date, overnight shift, late minutes) |
| `POST` | `/attendance/punch-out` | Punch-out (calculates work hours, overtime, half day) |
| `GET` | `/attendance` | List attendance records (paginated, sorted date desc, emp_code asc) |
| `PATCH`| `/attendance/{emp_code}/{date}` | Regularize record (recomputes metrics, appends history) |
| `GET` | `/analytics/employees/{emp_code}/monthly`| Employee monthly summary (working days, present days, %) |
| `GET` | `/analytics/departments/summary` | Department summary (includes 0-log headcount, average hours) |
| `GET` | `/analytics/leaderboard/late` | Competition ranking (1, 2, 2, 4) of late minutes |
| `GET` | `/analytics/departments/{department}/trend` | Daily trend with MongoDB `$densify` and 7-day moving avg |
| `GET` | `/admin/explain/{endpoint}` | Explain query plan (`IXSCAN` verified, `executionStats`) |

---

## Additional Documentation

- `REVIEW.md`: Details 26 defects identified in the starter code, symptoms, and how each was fixed.
- `DECISIONS.md`: Concise answers to the five architectural questions (indexes, race conditions, ranking ties, headcount, and 100x scalability).
