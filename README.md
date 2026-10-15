# Employee Attendance & Analytics API

FastAPI + MongoDB. All code is in `app/main.py`.

## Run
```
pip install -r requirements.txt
# set MONGO_URI and MONGO_DB (environment variables, or a local .env that is not committed)
uvicorn app.main:app --port 8000
```
Indexes are created at startup. `GET /health` returns 200 once MongoDB answers.

## Notes
- Instants are epoch milliseconds in the API and UTC datetimes in MongoDB; dates, months and shift times are IST strings.
- Rounding is done with integer arithmetic (half-up) so results do not depend on float behaviour.
- Analytics numbers (present days, averages, rates, moving average) are summed as integers inside the pipelines and divided once at the end.
- `moving_avg_7d` averages the reported (4-decimal) daily rates.

## Missing / known limits
(fill in honestly after your own testing)
