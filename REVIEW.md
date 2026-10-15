# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|

| 1 | `list_employees` | `skip = page * page_size` skips the first page. | `GET /employees?page=1` returns `items: []` while `total` is 6. | `skip = (page - 1) * page_size`. |
| 2 | `list_employees` | `total` is `count_documents({})`, ignoring the department filter (R10). | `?department=Sales` returns 2 items but `total: 6`. | Count with the same filter that is used for the page. |
| 3 | `list_employees` | No sort, so page boundaries are not stable; the contract says `emp_code` ascending. | Items can repeat or vanish between page 1 and 2. | Sort by `emp_code`; indexes `(emp_code)` unique and `(department, emp_code)`. |
| 4 | `list_employees`, `list_attendance` | `page` and `page_size` are not validated (R10: page >= 1, page_size 1..100). | `page=0` gives a negative slice start in `list_attendance` (wrong rows); `page_size=100000` is accepted. | `Query(ge=1)` and `Query(ge=1, le=100)`, so FastAPI answers 422. |
| 5 | `list_attendance` | Loads every matching document into Python, sorts and slices there, and no index exists. Slow and memory-hungry at 100k records; sorts by `date` only, so ties are unstable (contract: date desc, emp_code asc). | Time the call on 100k records; `explain` shows a COLLSCAN. | Filter, sort, `skip` and `limit` in MongoDB; compound indexes `(date desc, emp_code)`, `(status, date desc, emp_code)`, `(emp_code, date)`. |
| 6 | `list_attendance`, `punch_in` | Responses include an `id` (the `_id`) and raw datetimes. The contract forbids `_id` and wants epoch milliseconds. | Response has `"id": "..."` and ISO date strings. | `record_out()` builds the response explicitly; instants via `dt_to_ms()`. |
| 7 | `list_attendance` | `date_from`, `date_to`, `status` are unvalidated. | `status=NOPE` returns 200 with nothing; `date_from=2026-13-45` accepted; `date_from > date_to` should be 422. | Real-date validation, `Literal` status, explicit range check. |
| 8 | `MongoClient(...)` | Default naive datetimes: PyMongo returns UTC values without a time zone, which Python then treats as local time. | Subtracting an IST-aware time from a stored value raises `TypeError`; or silently shifts by 5:30. | `tz_aware=True` and all arithmetic in aware datetimes. |
| 9 | `punch_in` | `datetime.fromtimestamp(ms/1000)` uses the server's local time zone, and `ts.date()` uses that too, not IST (R1). | Punch-in at 00:10 IST on 8 July (18:40 UTC on 7 July) is stored as 7 July on a UTC server. Overnight shifts are never handled. | `ms_to_dt()` (UTC) and `attendance_date()` (IST, overnight rule). |
| 10 | `punch_in` | Sub-second part is kept (R1 says truncate to whole seconds). | `punched_at` ending in 900 ms is stored and returned with the milliseconds. | `ms // 1000` truncation in `ms_to_dt()`. |
| 11 | `PunchInIn`, `punch_in` | `if body.punched_at` treats `0` as "not provided"; `Optional[int]` accepts numeric strings and whole floats, and any size, so seconds-looking values pass. Contract: 422. | `{"punched_at": "1783312500000"}` and `{"punched_at": 1783312500}` are accepted. | `StrictInt` with range 1e11..4.1e12, and `is not None` check. |
| 12 | `punch_in` | Unknown employee: `emp` is `None`, then `emp["shift_start"]` raises `TypeError`, which is a 500. | `POST` with `emp_code: "EMP9999"`. | `find_employee()` raises 404. |
| 13 | `PunchInIn` | `status` is any string. Only PRESENT, WFH, ON_DUTY are allowed here. | `status: "ABSENT"` or `"banana"` creates a record. | `Literal["PRESENT","WFH","ON_DUTY"]`. |
| 14 | `punch_in` | Check-then-insert race: two simultaneous requests both pass `find_one`, both insert. | Fire 10 parallel requests: several 201s and duplicate rows. | Unique index `(emp_code, date)`; insert and catch `DuplicateKeyError` -> 409. |
| 15 | `create_employee` | Same race on `emp_code`. | Parallel duplicate creates give several 201s. | Unique index on `emp_code`; catch `DuplicateKeyError` -> 409. |
| 16 | `EmployeeIn`, `create_employee` | No validation (emp_code format, email, `HH:MM`, shift_start == shift_end, `joined_on` real date); `created_at` is `datetime.now()` (local, naive) returned as an ISO string. | `shift_start: "99:99"`, `joined_on: "2026-02-30"` accepted; `created_at` not epoch ms. | Field patterns and validators; `created_at` set from UTC clock and returned as epoch ms. |
| 17 | `compute_late_minutes` | Shift start is built on the punch-in's own date and time zone (UTC/naive), not IST; no overnight handling; the grace check runs on already floored minutes. | Shift 09:30, punch-in 09:40:01 gives 0 (should be 10); 10:15:59 only works if the zone happens to match. | Compute in seconds against the IST shift start: late if `> 600` s, then `seconds // 60`. |
| 18 | `compute_work_hours` | `round()` is not half-up (it rounds the binary float). | 9630 s = 2.675 h: starter gives 2.67, R4 requires 2.68. | Integer arithmetic in hundredths of an hour: `(2*seconds + 36) // 72`. |
| 19 | `compute_overtime` | No 30-minute minimum (R3); shift end is taken on the record date, so overnight shifts are wrong; naive/aware mismatch can raise `TypeError`. | Shift ends 18:30, punch-out 18:50: starter returns 20, R3 says 0. Overnight shift returns negative -> 0 or a huge value. | `shift_bounds()` gives the real end (next day for overnight); return minutes only if `>= 30`. |
| 20 | `/health` | Always returns ok; never touches MongoDB. Contract: 503 when MongoDB does not answer. | Stop MongoDB, call `/health`: still 200. | `ping`, 503 on failure. |
| 21 | module level | No indexes at all (also none that enforce uniqueness). | `explain` on any list shows COLLSCAN; races above. | `ensure_indexes()` at startup (idempotent, fixed index names), retried from `/health` if MongoDB was down at start. |
| 22 | all response paths | Legacy documents may lack `history` / `half_day` / `late_minutes`; a direct dump of the document would break or expose `_id`. | Record 9 in `DATA_MODEL.md`. | `record_out()` applies defaults (empty history, `half_day` false, 0 minutes). |

## Looked at and decided it was fine

- `load_dotenv()` does not override variables that are already set, so the grader's real environment variables win over a local `.env`.
- `{"_id": 0}` projection in `list_employees` is correct, and `doc.pop("_id")` after `insert_one` in `create_employee` does hide `_id`. I replaced it with an explicit response builder instead.
- Plain `def` endpoints run in FastAPI's thread pool and `MongoClient` is thread-safe, so I did not switch to Motor/async.
- Filtering the `date` string with `$gte`/`$lte` is fine because `YYYY-MM-DD` sorts the same as calendar order.

