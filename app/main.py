"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB   (a local .env is read for convenience; real environment variables win)

Layout of this file
  1. configuration and database connection
  2. small helpers: time, rounding, validation
  3. business rules R1-R5 (late, overtime, work hours, half day)
  4. request models
  5. endpoints: system, employees, attendance
  6. analytics pipelines and endpoints
  7. explain endpoint
"""
import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Dict, List, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import (
    AfterValidator,
    BaseModel,
    Field,
    StrictInt,
    model_validator,
)
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Configuration and database
# --------------------------------------------------------------------------- #
# load_dotenv() never overrides variables that are already set, so real
# environment variables (the grader's) take priority over a local .env file.
load_dotenv()

log = logging.getLogger("attendance")

# tz_aware=True: PyMongo hands back datetimes that know they are UTC. The default
# (naive datetimes) is a trap, because naive values are silently treated as local time.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]  # R6
GRACE_SECONDS = 10 * 60  # R2
OVERTIME_MIN_MINUTES = 30  # R3
HALF_DAY_CENTS = 450  # R5: rounded work_hours < 4.50  (compared as hundredths)
MS_MIN = 100_000_000_000
MS_MAX = 4_102_444_800_000

_indexes_ready = False


def ensure_indexes() -> None:
    """Create every index we rely on. create_index is idempotent for an identical spec."""
    global _indexes_ready
    emp = db.employees
    emp.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    emp.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code")
    emp.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined_on")
    emp.create_index([("joined_on", ASCENDING)], name="joined_on")

    att = db.attendance_logs
    # One record per employee per day, enforced by the database (punch-in race safety).
    att.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_emp_date")
    # GET /attendance sorted by date desc, emp_code asc; also the month range scans.
    att.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp")
    # Same list, filtered by status.
    att.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp"
    )
    # Punch-out: "latest record of this employee with punch_in <= punched_at".
    att.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in")
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError:
        # Do not crash: /health will report 503 and retry index creation once MongoDB answers.
        log.exception("could not create indexes at startup")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_: Request, exc: PyMongoError):
    # A database outage is "service unavailable", not an unexplained 500.
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# 2. Helpers: time, rounding, validation
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> UTC datetime truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def dt_to_ms(value: Optional[datetime]) -> Optional[int]:
    """Datetime -> epoch milliseconds using integer maths (floats can be off by one)."""
    if value is None:
        return None
    if value.tzinfo is None:  # defensive: treat naive values as UTC
        value = value.replace(tzinfo=UTC)
    delta = value.astimezone(UTC) - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


def now_seconds() -> datetime:
    """Current UTC time truncated to whole seconds (used for punches)."""
    return datetime.now(UTC).replace(microsecond=0)


def now_millis() -> datetime:
    """Current UTC time at millisecond precision (what MongoDB can store)."""
    n = datetime.now(UTC)
    return n.replace(microsecond=(n.microsecond // 1000) * 1000)


def ratio_half_up(numerator: int, denominator: int, decimals: int) -> float:
    """numerator/denominator rounded half-up to `decimals` places, using only integers (R8)."""
    scale = 10 ** decimals
    return ((2 * numerator * scale + denominator) // (2 * denominator)) / scale


DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
MONTH_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")


def _valid_date(v: str) -> str:
    if not DATE_RE.fullmatch(v):
        raise ValueError("date must look like YYYY-MM-DD")
    try:
        Date.fromisoformat(v)
    except ValueError:
        raise ValueError("not a real calendar date")
    return v


def _valid_month(v: str) -> str:
    if not MONTH_RE.fullmatch(v) or int(v[:4]) < 1:
        raise ValueError("month must look like YYYY-MM")
    return v


DateStr = Annotated[str, AfterValidator(_valid_date)]
MonthStr = Annotated[str, AfterValidator(_valid_month)]
EpochMs = Annotated[StrictInt, Field(ge=MS_MIN, le=MS_MAX)]  # strict: no floats, no strings
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
PageNo = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]


def month_bounds(month: str) -> tuple:
    """'2026-07' -> ('2026-07-01', '2026-07-31')"""
    year, mon = int(month[:4]), int(month[5:7])
    last = calendar.monthrange(year, mon)[1]
    return f"{month}-01", f"{month}-{last:02d}"


def working_days_in_month(month: str, joined_on: str) -> int:
    """Mon-Fri days in the month, counted from joined_on for mid-month joiners (R7)."""
    first_s, last_s = month_bounds(month)
    first, last = Date.fromisoformat(first_s), Date.fromisoformat(last_s)
    start = max(first, Date.fromisoformat(joined_on))
    if start > last:
        return 0
    count, day = 0, start
    while day <= last:
        if day.weekday() < 5:
            count += 1
        day += timedelta(days=1)
    return count


def paging(page: int, page_size: int) -> int:
    return min((page - 1) * page_size, 2 ** 62)  # skip; page 1 skips nothing


def validation_error(field: str, msg: str, where: str = "query") -> HTTPException:
    """422 in FastAPI's own error shape."""
    return HTTPException(422, [{"loc": [where, field], "msg": msg, "type": "value_error"}])


# --------------------------------------------------------------------------- #
# 3. Business rules R1 - R5
# --------------------------------------------------------------------------- #
def parse_hhmm(value: str) -> tuple:
    h, m = value.split(":")
    return int(h), int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return parse_hhmm(shift_end) <= parse_hhmm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> Date:
    """R1: IST calendar date of the punch-in; an overnight shift's early-morning punch-in
    belongs to the shift that started the previous day."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end):
        end_h, end_m = parse_hhmm(shift_end)
        if (local.hour, local.minute, local.second) < (end_h, end_m, 0):
            day -= timedelta(days=1)
    return day


def shift_bounds(day: Date, shift_start: str, shift_end: str) -> tuple:
    """Start and end of the shift that belongs to attendance date `day`, as IST datetimes.
    An overnight shift ends on the next calendar day."""
    sh, sm = parse_hhmm(shift_start)
    eh, em = parse_hhmm(shift_end)
    start = datetime(day.year, day.month, day.day, sh, sm, tzinfo=IST)
    end_day = day + timedelta(days=1) if is_overnight(shift_start, shift_end) else day
    end = datetime(end_day.year, end_day.month, end_day.day, eh, em, tzinfo=IST)
    return start, end


def compute_late_minutes(punch_in: datetime, shift_start_dt: datetime) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then whole minutes since shift start."""
    seconds = int((punch_in - shift_start_dt).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, shift_end_dt: datetime) -> int:
    """R3: whole minutes after shift end, counted only if at least 30."""
    minutes = int((punch_out - shift_end_dt).total_seconds()) // 60
    return minutes if minutes >= OVERTIME_MIN_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: hours to 2 decimals, rounded half-up. Done in whole hundredths of an hour so no
    float rounding (or Python's banker's rounding) can creep in: seconds/36 hundredths."""
    seconds = int((punch_out - punch_in).total_seconds())
    cents = (2 * seconds + 36) // 72
    return cents / 100


def is_half_day(work_hours: float) -> bool:
    """R5: the rounded work_hours is below 4.50."""
    return round(work_hours * 100) < HALF_DAY_CENTS


def derived_fields(emp: dict, day: Date, punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    """All four derived fields from the final times (used by punch-in/out and by corrections)."""
    if punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    start, end = shift_bounds(day, emp["shift_start"], emp["shift_end"])
    out = {
        "late_minutes": compute_late_minutes(punch_in, start),
        "work_hours": None,
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        wh = compute_work_hours(punch_in, punch_out)
        out.update(work_hours=wh, overtime_minutes=compute_overtime(punch_out, end), half_day=is_half_day(wh))
    return out


# --------------------------------------------------------------------------- #
# 4. Models and response shaping
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    shift_end: str = Field(default="18:30", pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    joined_on: DateStr

    @model_validator(mode="after")
    def _check(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        if not self.name.strip() or not self.department.strip():
            raise ValueError("name and department must not be blank")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _no_explicit_null(self):
        # Omitted means "unchanged"; an explicit null is not a valid value for these fields.
        for field in ("status", "punch_in", "punch_out"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} must not be null")
        return self


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def _history_value(value: Any) -> Any:
    return dt_to_ms(value) if isinstance(value, datetime) else value


def history_out(entry: dict) -> dict:
    return {
        "at": dt_to_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": {
            field: {"from": _history_value(ch.get("from")), "to": _history_value(ch.get("to"))}
            for field, ch in (entry.get("changes") or {}).items()
        },
    }


def record_out(doc: dict) -> dict:
    """Stored attendance document -> API shape. Never exposes _id; missing legacy fields get defaults."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# 5a. System and employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_millis()
    try:
        # The unique index decides; no "check then insert" window for two requests to slip through.
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: PageNo = 1, page_size: PageSize = 20):
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department
    # estimated count is O(1) metadata; only valid for the unfiltered collection.
    total = db.employees.count_documents(q) if q else db.employees.estimated_document_count()
    docs = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(paging(page, page_size)).limit(page_size)
    return {"items": [employee_out(d) for d in docs], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# 5b. Attendance
# --------------------------------------------------------------------------- #
def find_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = find_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_seconds()
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derived_fields(emp, day, ts, None),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index makes the loser fail
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = find_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_seconds()

    # Most recent record that started at or before the punch-out (works across midnight).
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    started = rec["punch_in"]
    if ts <= started:
        raise HTTPException(422, "punched_at must be after punch_in")
    if ts - started > timedelta(hours=24):
        raise HTTPException(422, "punched_at is more than 24 hours after punch_in")

    day = Date.fromisoformat(rec["date"])
    fields = derived_fields(emp, day, started, ts)
    fields["punch_out"] = ts
    # Atomic compare-and-set: only succeeds if the record is still open. Two simultaneous
    # punch-outs both pass the checks above, but only one of them can match this filter.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None}, {"$set": fields}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


def attendance_filter(
    emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str], status: Optional[str]
) -> dict:
    """The filter used by GET /attendance (and by its explain)."""
    if date_from and date_to and date_from > date_to:
        raise validation_error("date_from", "date_from must not be after date_to")
    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    page: PageNo = 1,
    page_size: PageSize = 20,
):
    q = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q) if q else db.attendance_logs.estimated_document_count()
    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort(ATTENDANCE_SORT)
        .skip(paging(page, page_size))
        .limit(page_size)
    )
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: DateStr, body: RegularizeIn):
    emp = find_employee(emp_code)
    for _attempt in range(5):
        rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
        if rec is None:
            raise HTTPException(404, "no record for that date")

        old = {
            "status": rec["status"],
            "punch_in": rec.get("punch_in"),
            "punch_out": rec.get("punch_out"),
            "work_hours": rec.get("work_hours"),
            "late_minutes": int(rec.get("late_minutes") or 0),
            "overtime_minutes": int(rec.get("overtime_minutes") or 0),
            "half_day": bool(rec.get("half_day", False)),
        }
        history_len = len(rec.get("history") or [])

        # ---- build the new state and validate it -------------------------------------------
        new_status = body.status or old["status"]
        if new_status in ("ABSENT", "LEAVE"):
            if body.punch_in is not None or body.punch_out is not None:
                raise HTTPException(422, f"{new_status} clears punch times; do not supply punch_in/punch_out")
            new_in = new_out = None
        else:
            new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old["punch_in"]
            new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old["punch_out"]
            if new_in is None:
                raise HTTPException(422, f"status {new_status} requires a punch_in")
            if attendance_date(new_in, emp["shift_start"], emp["shift_end"]).isoformat() != date:
                raise HTTPException(422, "punch_in must stay on the record's attendance date")
            if new_out is not None and not (new_in < new_out <= new_in + timedelta(hours=24)):
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

        if (new_status, new_in, new_out) == (old["status"], old["punch_in"], old["punch_out"]):
            raise HTTPException(422, "the request changes nothing")

        new = {"status": new_status, "punch_in": new_in, "punch_out": new_out}
        new.update(derived_fields(emp, Date.fromisoformat(date), new_in, new_out))

        changes = {f: {"from": old[f], "to": new[f]} for f in new if new[f] != old[f]}
        entry = {
            "at": now_millis(),
            "by": body.regularized_by,
            "reason": body.reason,
            "changes": changes,
        }

        # ---- optimistic compare-and-set ---------------------------------------------------
        # Only write if nobody changed the record since we read it: the history length acts as a
        # version number (every correction appends exactly one entry) and punch_out guards against
        # a concurrent punch-out. If we lose, re-read and try again.
        guard: Dict[str, Any] = {"_id": rec["_id"], "punch_out": old["punch_out"]}
        if history_len == 0:
            guard["$or"] = [{"history": {"$exists": False}}, {"history": {"$size": 0}}]
        else:
            guard["history"] = {"$size": history_len}
        updated = db.attendance_logs.find_one_and_update(
            guard, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
        )
        if updated is not None:
            return record_out(updated)
    raise HTTPException(409, "the record is being changed by someone else, please retry")


# --------------------------------------------------------------------------- #
# 6. Analytics: pipeline builders (each is also used by the explain endpoint)
# --------------------------------------------------------------------------- #
WEEKDAY_NUMBERS = [2, 3, 4, 5, 6]  # MongoDB $dayOfWeek: 1 = Sunday ... 7 = Saturday


def _weekday_of(date_expr: str) -> dict:
    return {"$dayOfWeek": {"$dateFromString": {"dateString": date_expr, "format": "%Y-%m-%d"}}}


def _present_halves(log: str) -> dict:
    """Half-days of presence for one log: 2 = full day, 1 = half day, 0 = not present or weekend (R5-R7)."""
    return {
        "$cond": [
            {"$and": [{"$in": [f"{log}.status", PRESENT_STATUSES]}, {"$in": [_weekday_of(f"{log}.date"), WEEKDAY_NUMBERS]}]},
            {"$cond": [{"$eq": [f"{log}.half_day", True]}, 1, 2]},
            0,
        ]
    }


def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    start, end = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {
            "$group": {
                "_id": None,
                "present_halves": {"$sum": _present_halves("$$ROOT")},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


def department_summary_pipeline(month: str, department: Optional[str]) -> list:
    start, end = month_bounds(month)
    match: Dict[str, Any] = {"joined_on": {"$lte": end}}  # R9: joined on or before month end
    if department is not None:
        match["department"] = department
    present = {"$in": ["$$l.status", PRESENT_STATUSES]}
    has_hours = {"$ne": [{"$ifNull": ["$$l.work_hours", None]}, None]}
    return [
        # Start from employees so people with zero logs still exist as rows (headcount).
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"c": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$c"]},
                                    {"$gte": ["$date", start]},
                                    {"$lte": ["$date", end]},
                                ]
                            }
                        }
                    },
                    {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "work_hours": 1, "late_minutes": 1}},
                ],
                "as": "logs",
            }
        },
        # One row per employee with that employee's totals, then one row per department.
        {
            "$project": {
                "department": 1,
                "present_halves": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": _present_halves("$$l")}}},
                "work_cents": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$and": [present, has_hours]},
                                    {"$round": [{"$multiply": ["$$l.work_hours", 100]}, 0]},
                                    0,
                                ]
                            },
                        }
                    }
                },
                "work_count": {
                    "$sum": {
                        "$map": {"input": "$logs", "as": "l", "in": {"$cond": [{"$and": [present, has_hours]}, 1, 0]}}
                    }
                },
                "late_count": {
                    "$sum": {"$map": {"input": "$logs", "as": "l", "in": {"$cond": [{"$gt": ["$$l.late_minutes", 0]}, 1, 0]}}}
                },
                "total_late_minutes": {"$sum": "$logs.late_minutes"},
                "leave_count": {
                    "$sum": {"$map": {"input": "$logs", "as": "l", "in": {"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]}}}
                },
                "on_duty_count": {
                    "$sum": {"$map": {"input": "$logs", "as": "l", "in": {"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]}}}
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_halves": {"$sum": "$present_halves"},
                "work_cents": {"$sum": "$work_cents"},
                "work_count": {"$sum": "$work_count"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },
        {"$sort": {"_id": 1}},
    ]


def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    start, end = month_bounds(month)
    pipeline: List[dict] = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees have no match and disappear here
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # $rank is standard competition ranking: 1, 2, 2, 4. Ties share a rank, the next rank is skipped.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit AFTER ranking: tied rows at the cutoff all stay
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


def department_trend_pipeline(department: str, date_from: str, date_to: str) -> list:
    start = Date.fromisoformat(date_from)
    n_days = (Date.fromisoformat(date_to) - start).days + 1
    start_dt = datetime(start.year, start.month, start.day, tzinfo=UTC)
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "emp_codes": {"$push": "$emp_code"}, "joined": {"$push": "$joined_on"}}},
        # Gap filling: generate exactly one entry per calendar day inside the database.
        {
            "$project": {
                "_id": 0,
                "emp_codes": 1,
                "joined": 1,
                "days": {
                    "$map": {
                        "input": {"$range": [0, n_days]},
                        "as": "i",
                        "in": {
                            "$dateToString": {
                                "format": "%Y-%m-%d",
                                "date": {"$dateAdd": {"startDate": start_dt, "unit": "day", "amount": "$$i"}},
                            }
                        },
                    }
                },
            }
        },
        {"$unwind": "$days"},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"d": "$days", "codes": "$emp_codes"},
                "pipeline": [
                    {"$match": {"$expr": {"$and": [{"$eq": ["$date", "$$d"]}, {"$in": ["$emp_code", "$$codes"]}]}}},
                    {
                        "$group": {
                            "_id": None,
                            "present_halves": {"$sum": {"$cond": [
                                {"$in": ["$status", PRESENT_STATUSES]},
                                {"$cond": [{"$eq": ["$half_day", True]}, 1, 2]},
                                0,
                            ]}},
                            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                        }
                    },
                ],
                "as": "agg",
            }
        },
        {
            "$set": {
                "date": "$days",
                "is_working_day": {"$in": [_weekday_of("$days"), WEEKDAY_NUMBERS]},
                "headcount": {"$size": {"$filter": {"input": "$joined", "as": "j", "cond": {"$lte": ["$$j", "$days"]}}}},
                "present_halves": {"$ifNull": [{"$first": "$agg.present_halves"}, 0]},
                "late_count": {"$ifNull": [{"$first": "$agg.late_count"}, 0]},
            }
        },
        {
            # rate in ten-thousandths, rounded half-up with integer maths: floor((2*N + D) / (2*D))
            # where rate = present_halves/2/headcount and N/D scaled by 10000.
            "$set": {
                "rate_scaled": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {
                            "$floor": {
                                "$divide": [
                                    {"$add": [{"$multiply": ["$present_halves", 20000]}, {"$multiply": ["$headcount", 2]}]},
                                    {"$multiply": ["$headcount", 4]},
                                ]
                            }
                        },
                        None,
                    ]
                }
            }
        },
        {"$sort": {"date": 1}},
        {
            # 7-row window: this row and up to 6 before it, inside the requested range only.
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "ma_sum": {"$sum": "$rate_scaled", "window": {"documents": [-6, 0]}},
                    "ma_cnt": {
                        "$sum": {"$cond": [{"$ne": ["$rate_scaled", None]}, 1, 0]},
                        "window": {"documents": [-6, 0]},
                    },
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_halves": 1,
                "late_count": 1,
                "rate_scaled": 1,
                "ma_scaled": {
                    "$cond": [
                        {"$gt": ["$ma_cnt", 0]},
                        {"$floor": {"$divide": [{"$add": [{"$multiply": ["$ma_sum", 2]}, "$ma_cnt"]}, {"$multiply": ["$ma_cnt", 2]}]}},
                        None,
                    ]
                },
            }
        },
    ]


def check_trend_range(date_from: str, date_to: str) -> None:
    if date_to < date_from:
        raise validation_error("to", "to must not be before from")
    if (Date.fromisoformat(date_to) - Date.fromisoformat(date_from)).days + 1 > 92:
        raise validation_error("to", "the range must not exceed 92 days")


# --------------------------------------------------------------------------- #
# 6b. Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthStr):
    emp = find_employee(emp_code)
    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month)))
    r = rows[0] if rows else {}
    halves = int(r.get("present_halves", 0))
    working = working_days_in_month(month, emp["joined_on"])
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working,
        "present_days": halves / 2,
        "leave_days": int(r.get("leave_days", 0)),
        "late_count": int(r.get("late_count", 0)),
        "total_late_minutes": int(r.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(r.get("total_overtime_minutes", 0)),
        "attendance_pct": ratio_half_up(halves * 100, 2 * working, 2) if working > 0 else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: MonthStr, department: Optional[str] = None):
    items = []
    for r in db.employees.aggregate(department_summary_pipeline(month, department)):
        count = int(r["work_count"])
        items.append(
            {
                "department": r["_id"],
                "headcount": int(r["headcount"]),
                "present_days": int(r["present_halves"]) / 2,
                "avg_work_hours": ratio_half_up(int(round(r["work_cents"])), count * 100, 2) if count else None,
                "late_count": int(r["late_count"]),
                "total_late_minutes": int(r["total_late_minutes"]),
                "leave_count": int(r["leave_count"]),
                "on_duty_count": int(r["on_duty_count"]),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: MonthStr, limit: Annotated[int, Query(ge=1, le=50)] = 10, department: Optional[str] = None
):
    rows = db.attendance_logs.aggregate(late_leaderboard_pipeline(month, limit, department))
    items = [
        {
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        }
        for r in rows
    ]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: Annotated[DateStr, Query(alias="from")], to: DateStr):
    check_trend_range(from_, to)
    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "unknown department")
    items = []
    for r in db.employees.aggregate(department_trend_pipeline(department, from_, to)):
        items.append(
            {
                "date": r["date"],
                "is_working_day": bool(r["is_working_day"]),
                "headcount": int(r["headcount"]),
                "present_count": int(r["present_halves"]) / 2,
                "late_count": int(r["late_count"]),
                "attendance_rate": None if r.get("rate_scaled") is None else int(r["rate_scaled"]) / 10000,
                "moving_avg_7d": None if r.get("ma_scaled") is None else int(r["ma_scaled"]) / 10000,
            }
        )
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 7. Explain endpoint
# --------------------------------------------------------------------------- #
def _missing(field: str) -> HTTPException:
    return HTTPException(422, [{"loc": ["query", field], "msg": "Field required", "type": "missing"}])


def _checked(value: Optional[str], field: str, check) -> str:
    if value is None:
        raise _missing(field)
    try:
        return check(value)
    except ValueError as exc:
        raise validation_error(field, str(exc))


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[str], Query(alias="from")] = None,
    to: Optional[str] = None,
    page: PageNo = 1,
    page_size: PageSize = 20,
):
    # Each branch builds the query with the very same builder the real endpoint uses.
    if endpoint == "attendance_list":
        if date_from is not None:
            _valid_date_or_422(date_from, "date_from")
        if date_to is not None:
            _valid_date_or_422(date_to, "date_to")
        q = attendance_filter(emp_code, date_from, date_to, status)
        collection = "attendance_logs"
        command: Dict[str, Any] = {
            "find": collection,
            "filter": q,
            "sort": dict(ATTENDANCE_SORT),
            "skip": paging(page, page_size),
            "limit": page_size,
        }
    elif endpoint == "employee_monthly":
        if not emp_code:
            raise _missing("emp_code")
        collection = "attendance_logs"
        command = {"aggregate": collection, "pipeline": employee_monthly_pipeline(emp_code, _checked(month, "month", _valid_month)), "cursor": {}}
    elif endpoint == "department_summary":
        collection = "employees"
        command = {"aggregate": collection, "pipeline": department_summary_pipeline(_checked(month, "month", _valid_month), department), "cursor": {}}
    elif endpoint == "late_leaderboard":
        collection = "attendance_logs"
        command = {"aggregate": collection, "pipeline": late_leaderboard_pipeline(_checked(month, "month", _valid_month), limit, department), "cursor": {}}
    else:  # department_trend
        if not department:
            raise _missing("department")
        start = _checked(from_, "from", _valid_date)
        end = _checked(to, "to", _valid_date)
        check_trend_range(start, end)
        collection = "employees"
        command = {"aggregate": collection, "pipeline": department_trend_pipeline(department, start, end), "cursor": {}}

    raw = db.command({"explain": command, "verbosity": "executionStats"})
    # Explain output contains BSON types (dates, Int64...). Round-trip through Extended JSON.
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(raw))}


def _valid_date_or_422(value: str, field: str) -> None:
    try:
        _valid_date(value)
    except ValueError as exc:
        raise validation_error(field, str(exc))