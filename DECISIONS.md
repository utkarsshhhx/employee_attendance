# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
5. **One thing you would change** if this had to serve 100x the data.
# DECISIONS.md


1. **Indexes.** `emp_code` unique on employees, so the database rejects duplicate employees. `(emp_code, date)` unique on attendance, which makes "one record per day" a database rule and also serves PATCH and the monthly report. `(date desc, emp_code)` and `(status, date desc, emp_code)` serve the sorted attendance list and the month scans. `(emp_code, punch_in desc)` finds the open record at punch-out. Employees also have `(department, emp_code)`, `(department, joined_on)` and `(joined_on)` for listing, headcount and the trend. I rejected a separate `late_minutes` index: the month range already narrows the data a lot and it would slow every write.

2. **Punch-in race.** Both requests compute the same date, then both try `insert_one`. The unique `(emp_code, date)` index lets MongoDB accept exactly one. The other gets `DuplicateKeyError`, which I turn into 409. There is no check before the insert.

3. **Ties.** The ranking uses `$rank` (1, 2, 2, 4) and the limit is applied to the rank afterwards, so everyone tied at a rank within the limit is returned. The response can have more rows than `limit`.

4. **Headcount.** The department summary starts from `employees` (joined on or before month end), not from the logs, and looks the logs up per employee. An employee with no logs is still a row, so they are counted.

5. **At 100x the data.** I would pre-aggregate: keep monthly totals per employee and department in a summary collection, updated on each punch/correction, instead of recomputing from logs on every request.
