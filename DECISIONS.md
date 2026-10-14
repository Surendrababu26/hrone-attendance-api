# DECISIONS.md

1. **Indexes.**
We created:
- `employees`: unique `{"emp_code": 1}` enforcing business key uniqueness and rapid lookups; `{"department": 1, "joined_on": 1}` for department queries and headcount scans.
- `attendance_logs`: unique compound `{"emp_code": 1, "date": 1}` enforcing the one-record-per-day rule and preventing punch-in races; `{"date": -1, "emp_code": 1}` for attendance list pagination; `{"emp_code": 1, "punch_in": -1}` for punch-out lookups; and `{"date": 1, "late_minutes": 1, "emp_code": 1}` for the late leaderboard.
We considered and rejected an index on `punch_out` alone, as compound `(emp_code, punch_in)` already satisfies lookups without extra index write overhead.

2. **Punch-in race.**
Both concurrent requests validate the employee and compute the IST attendance date. When both execute `insert_one()`, MongoDB's unique compound index on `(emp_code, date)` serializes them atomically. Exactly one request succeeds with HTTP 201, while the duplicate triggers a PyMongo `DuplicateKeyError`, which our code intercepts to return HTTP 409 Conflict.

3. **Ties.**
Our leaderboard pipeline uses `$setWindowFields` with `$rank` to produce standard competition ranking (1, 2, 2, 4). The `limit` filter (`rank <= limit`) applies strictly after ranking. If two employees tie at the cutoff rank, both are included in the results (returning more rows than `limit`), ordered by `total_late_minutes` descending, then `emp_code` ascending.

4. **Headcount.**
The department summary aggregation starts from the `employees` collection matching `{"joined_on": {"$lte": month_end}}`. Employees are grouped by department, counting `headcount: {"$sum": 1}` directly. An outer `$lookup` brings in their attendance logs, ensuring employees with zero logs are fully counted in headcount.

5. **One thing you would change for 100x the data.**
At 100x scale (~10 million records), we would maintain pre-aggregated daily/monthly rollup documents updated incrementally via MongoDB Change Streams or punch-time writes, turning expensive multi-stage aggregations into constant-time key-value lookups, while sharding attendance logs by `emp_code` and date.
