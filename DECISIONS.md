# DECISIONS.md

1. **Indexes.** 
We created a unique index on `employees(emp_code)` and a compound unique index on `attendance_logs(emp_code, date)` to enforce the business key and guarantee that an employee has at most one record per attendance day. For performance, we created `attendance_logs(date desc, emp_code asc)` to satisfy pagination sorting, `employees(department, joined_on)` for department filtering, and `attendance_logs(date, late_minutes, emp_code)` for leaderboard and analytics queries. We considered and rejected an index on `(punch_in, punch_out)` because business rules address records strictly by `(emp_code, date)`.

2. **Punch-in race.** 
Both concurrent requests resolve the same IST attendance date and attempt `insert_one`. MongoDB's unique index on `(emp_code, date)` forces atomic serialization at the database layer. Exactly one insert acquires the lock and succeeds (returning HTTP 201). The second concurrent insert violates the unique constraint, raising `DuplicateKeyError`, which our exception handler catches and translates to HTTP 409 Conflict without data corruption.

3. **Ties.** 
Our pipeline uses MongoDB's `$setWindowFields` with `$rank` (standard competition ranking: 1, 2, 2, 4), filtering on `rank <= limit`. If multiple employees tie on late minutes at the cutoff rank, all tied employees are returned (e.g. if limit is 10 and three candidates tie for rank 10, all three are returned). Slicing or omitting tied employees would produce non-deterministic results.

4. **Headcount.** 
Rather than querying `attendance_logs` directly, our aggregation pipeline anchors on `employees` filtered by `joined_on <= month_end` (and optional department), then performs a left `$lookup` into `attendance_logs` for that month. Employees who have zero attendance records still produce an outer document with an empty logs array, guaranteeing an accurate department headcount count.

5. **One thing you would change for 100x data.** 
At 100x scale, we would maintain pre-aggregated daily and monthly counters asynchronously via change streams or background rollups rather than computing full `$lookup` joins and window functions over raw punch records on demand.
