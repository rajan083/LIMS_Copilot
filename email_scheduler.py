"""email_scheduler.py - "send later" and repeating report emails (single-file version).

How it works
  1. The model calls schedule_report_email. That only SAVES A DRAFT (status awaiting_confirmation).
  2. The user clicks Schedule in the chat. That calls confirm_pending(user).
  3. From then on sending is AUTOMATIC: a background worker checks every 30 seconds, rebuilds the
     report with fresh data and emails it. Nobody has to press anything again.

The worker only runs while your app runs, so start it once at startup (api.py):

    import email_scheduler
    email_scheduler.start_in_background()

Wiring that is still needed (see the notes at the bottom of this file):
  * run_tool_call: dispatch SCHEDULER_TOOL_NAMES to handle_scheduler_tool(name, args, user, roles, session_id)
  * tools list:    tools = tools + SCHEDULER_TOOLS
  * api.py:        pending_schedule = get_pending_job(user) in the message response, plus confirm/cancel endpoints
                   that call confirm_pending(user) and cancel_pending(user)

Jobs are stored in the same MongoDB as the LIMS data unless SCHEDULER_DB_NAME is set. Your README advises a
read-only database user, and this file WRITES, so point SCHEDULER_DB_NAME at a database that user may write to.
"""
import asyncio
import copy
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from bson import ObjectId
from croniter import croniter
from pymongo import ReturnDocument

from db import db
import email_report
from email_report import EMAIL_FULL_RE, _smtp_send, is_external

logger = logging.getLogger("lims.scheduler")

# ---------------------------------------------------------------- settings
TZ = ZoneInfo(os.environ.get("SCHEDULER_TZ", "Asia/Kolkata"))
MAX_JOBS_PER_USER = 10
MIN_INTERVAL_SECONDS = 24 * 3600      # repeating jobs may run at most once a day
MAX_CONSECUTIVE_FAILURES = 3
STALE_RUNNING_MINUTES = 15            # a job stuck in "running" this long is released
POLL_SECONDS = 30
DRAFT_TTL_MINUTES = 60                # an unconfirmed draft is forgotten after this
ONE_TIME_GRACE_HOURS = 24             # a one-time email later than this is not sent
MAX_AHEAD_DAYS = 366
SCHEDULER_ROLES = {"accounts", "admin"}

SCHEDULABLE_TOOLS = [
    "export_records_to_excel",
    "export_joined_records_to_excel",
    "export_summary_to_excel",
    "export_records_to_pdf",
]
ACTIVE = ["awaiting_confirmation", "pending", "running", "paused"]
ACTIONS = ["cancel", "pause", "resume"]
PRESETS = ["today", "yesterday", "last_7_days", "last_30_days", "last_week", "month_to_date", "last_month"]


# ---------------------------------------------------------------- helpers
def _col():
    name = os.environ.get("SCHEDULER_DB_NAME")
    return (db.client[name] if name else db)["scheduled_emails"]


def _now():
    return datetime.now(timezone.utc)


def _local(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ)


def _fmt(dt):
    return _local(dt).strftime("%a %d %b %Y, %I:%M %p")


def _next_cron(expr):
    return croniter(expr, _now().astimezone(TZ)).get_next(datetime).astimezone(timezone.utc)


def _oid(job_id):
    return ObjectId(job_id) if job_id and ObjectId.is_valid(str(job_id)) else None


def _notify(job, text):
    _col().database["scheduler_notifications"].insert_one({
        "owner": job["owner"],
        "text": text,
        "created_at": _now(),
        "delivered": False,
    })


def pop_notifications(owner):
    """Returns undelivered notifications for this user and marks them delivered."""
    col = _col().database["scheduler_notifications"]
    out = []
    while True:
        n = col.find_one_and_update({"owner": owner, "delivered": False},
                                    {"$set": {"delivered": True}},
                                    sort=[("created_at", 1)])
        if not n:
            return out
        out.append({"text": n["text"], "at": _fmt(n["created_at"])})

# ---------------------------------------------------------------- relative dates
def resolve_preset(preset):
    """Returns (date_from, date_to). date_to is EXCLUSIVE, like the query tools ($lt)."""
    today = datetime.now(TZ).date()
    d = timedelta(days=1)
    first = today.replace(day=1)
    monday = today - timedelta(days=today.weekday())
    table = {
        "today": (today, today + d),
        "yesterday": (today - d, today),
        "last_7_days": (today - 7 * d, today),
        "last_30_days": (today - 30 * d, today),
        "last_week": (monday - 7 * d, monday),                  # Monday to Sunday
        "month_to_date": (first, today + d),
        "last_month": ((first - d).replace(day=1), first),
    }
    if preset not in table:
        raise ValueError(f"Unknown date_preset '{preset}'. Use one of: {', '.join(PRESETS)}")
    return tuple(x.isoformat() for x in table[preset])


def _dicts(node):
    """Every dict inside the arguments (top level, 'sheets' items, 'left'/'right' sides)."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _dicts(v)
    elif isinstance(node, list):
        for v in node:
            yield from _dicts(v)


def _check_presets(args):
    for d in _dicts(args):
        preset = d.get("date_preset")
        if not preset:
            continue
        if preset not in PRESETS:
            return f"Unknown date_preset '{preset}'. Use one of: {', '.join(PRESETS)}"
        if not d.get("date_field"):
            return "date_preset needs date_field in the same object (for example 'order_date')."
        if d.get("date_from") or d.get("date_to"):
            return "Use either date_preset or date_from/date_to, not both."
    return None


def _has_fixed_dates(args):
    return any((d.get("date_from") or d.get("date_to")) and not d.get("date_preset") for d in _dicts(args))


def _apply_presets(node):
    """Replace every {"date_preset": X} in the args tree with date_from/date_to (in place)."""
    for d in _dicts(node):
        preset = d.pop("date_preset", None)
        if preset:
            d["date_from"], d["date_to"] = resolve_preset(preset)


# ---------------------------------------------------------------- jobs
def _summary(tool, recipient, cron, when):
    kind = f"Repeating ({cron}), first run" if cron else "One time,"
    return f"{tool} to {recipient}. {kind} {_fmt(when)} ({TZ.key})."


def create_job(owner, roles, a, session_id=""):
    """Saves a DRAFT in 'awaiting_confirmation'. Nothing is sent until confirm_pending()."""
    if roles is not None and not (set(roles) & SCHEDULER_ROLES):
        return {"error": "Access denied: this user's role cannot schedule emails."}

    tool, args = a.get("report_tool"), a.get("report_args")
    recipient = (a.get("recipient") or "").strip()
    run_at, cron, in_minutes = a.get("run_at"), (a.get("cron") or "").strip(), a.get("in_minutes")

    if tool not in SCHEDULABLE_TOOLS:
        return {"error": f"Cannot schedule '{tool}'. Allowed: {SCHEDULABLE_TOOLS}"}
    if not isinstance(args, dict) or not args:
        return {"error": "report_args is required: give the arguments the export tool needs."}
    if not EMAIL_FULL_RE.match(recipient):
        return {"error": "A valid recipient email address is required."}

    typed = [x.lower() for x in email_report._typed.get(session_id, [])]
    shown = [x.lower() for x in email_report._shown.get(session_id, [])]
    if recipient.lower() not in typed + shown:
        return {"error": "That address was not typed by the user or shown to them in this chat. Ask the user to type the address."}
    source = "typed" if recipient.lower() in typed else "shown"

    if len([x for x in (run_at, cron, in_minutes) if x not in (None, "")]) != 1:
        return {"error": "Provide exactly one of run_at, in_minutes or cron. If the user did not say when, ask them."}
    problem = _check_presets(args)
    if problem:
        return {"error": problem}

    try:
        if cron:
            if not croniter.is_valid(cron) or len(cron.split()) != 5:
                return {"error": "cron must have 5 fields, for example '0 9 * * 1' (Mondays at 9:00)."}
            if _has_fixed_dates(args):
                return {"error": "A repeating report must use date_preset instead of fixed dates, "
                                 "otherwise every run would cover the same period."}
            it = croniter(cron, _now().astimezone(TZ))
            times = [it.get_next(float) for _ in range(8)]
            if min(b - a_ for a_, b in zip(times, times[1:])) < MIN_INTERVAL_SECONDS:
                return {"error": "Recurring reports can run at most once per day."}
            next_run = _next_cron(cron)
        elif in_minutes not in (None, ""):
            minutes = int(in_minutes)
            if not 1 <= minutes <= MAX_AHEAD_DAYS * 24 * 60:
                return {"error": "in_minutes must be between 1 and 527040."}
            next_run = _now() + timedelta(minutes=minutes)
        else:
            dt = datetime.fromisoformat(str(run_at).strip())
            next_run = (dt if dt.tzinfo else dt.replace(tzinfo=TZ)).astimezone(timezone.utc)
            if next_run < _now() - timedelta(minutes=1):
                return {"error": "run_at is in the past. Ask the user for a time in the future."}
            if next_run > _now() + timedelta(days=MAX_AHEAD_DAYS):
                return {"error": "That is more than a year away. Ask the user for an earlier date."}
    except (ValueError, TypeError) as e:
        return {"error": f"Invalid schedule: {e}"}

    _col().delete_many({"owner": owner, "status": "awaiting_confirmation"})      # one draft at a time
    if _col().count_documents({"owner": owner, "status": {"$in": ACTIVE}}) >= MAX_JOBS_PER_USER:
        return {"error": f"Limit of {MAX_JOBS_PER_USER} scheduled emails reached. Ask the user to cancel one first."}

    summary = _summary(tool, recipient, cron, next_run)
    job_id = _col().insert_one({
        "owner": owner,
        "roles": list(roles) if roles is not None else None,
        "recipient": recipient,
        "recipient_source": source,
        "subject": " ".join(str(a.get("subject") or "LIMS report").split())[:150],
        "body": str(a.get("body") or "PFA report")[:2000],
        "report": {"tool": tool, "args": args},
        "cron": cron or None,
        "summary": summary,
        "status": "awaiting_confirmation",
        "next_run_at": next_run,
        "created_at": _now(),
        "run_count": 0,
        "consecutive_failures": 0,
        "last_error": None,
    }).inserted_id
    return {
        "status": "awaiting_user_confirmation",   # handle_turn already stops on this value
        "job_id": str(job_id),
        "summary": summary,
        "message": (f"Prepared: {summary} It is NOT scheduled yet; it is waiting for the user's confirmation. "
                    "Tell the user it is ready and ask them to confirm. Do NOT say it is scheduled."),
    }


def _public(j):
    return {
        "to": j["recipient"],
        "source": j.get("recipient_source"),
        "external": is_external(j["recipient"]),
        "subject": j["subject"],
        "body": j["body"],
        "report": j["report"]["tool"],
        "repeats": bool(j["cron"]),
        "cron": j["cron"],
        "summary": j["summary"],
    }


def _draft(owner):
    cutoff = _now() - timedelta(minutes=DRAFT_TTL_MINUTES)
    return _col().find_one({"owner": owner, "status": "awaiting_confirmation", "created_at": {"$gte": cutoff}},
                           sort=[("created_at", -1)])


def get_pending_job(owner):
    """The draft waiting for the user's click, shaped for the chat UI (or None)."""
    j = _draft(owner)
    return _public(j) if j else None


def _activate(j):
    when = j["next_run_at"]
    if j["cron"]:
        when = _next_cron(j["cron"])                    # a draft left open still gets the next real slot
    elif _local(when) < datetime.now(TZ) - timedelta(minutes=1):
        _col().delete_one({"_id": j["_id"]})
        return {"ok": False, "message": "That time has already passed. Ask for the schedule again with a new time."}
    res = _col().update_one({"_id": j["_id"], "status": "awaiting_confirmation"},
                            {"$set": {"status": "pending", "next_run_at": when}})
    if not res.modified_count:
        return {"ok": False, "message": "Nothing to schedule."}
    logger.info(f"[scheduler] CONFIRMED job={j['_id']} owner={j['owner']} to={j['recipient']}")
    return {"ok": True, "message": "Scheduled: " + _summary(j["report"]["tool"], j["recipient"], j["cron"], when)}


def confirm_pending(owner):
    """The user clicked Schedule. Returns {"ok": bool, "message": str}."""
    j = _draft(owner)
    return _activate(j) if j else {"ok": False, "message": "Nothing to schedule."}


def confirm_job(job_id, owner):
    """Same as confirm_pending, for one specific job id."""
    oid = _oid(job_id)
    j = _col().find_one({"_id": oid, "owner": owner, "status": "awaiting_confirmation"}) if oid else None
    return _activate(j) if j else {"ok": False, "message": "Nothing to schedule."}


def cancel_pending(owner):
    _col().delete_many({"owner": owner, "status": "awaiting_confirmation"})


def list_jobs(owner):
    jobs = _col().find({"owner": owner, "status": {"$in": [s for s in ACTIVE if s != "awaiting_confirmation"] + ["failed"]}}
                       ).sort("next_run_at", 1)
    return {"jobs": [{
        "job_id": str(j["_id"]),
        "status": j["status"],
        "recipient": j["recipient"],
        "report_tool": j["report"]["tool"],
        "schedule": j["cron"] or "once",
        "next_run": _fmt(j["next_run_at"]) if j.get("next_run_at") else None,
        "times_sent": j.get("run_count", 0),
        "last_error": j.get("last_error"),
    } for j in jobs]}


def manage_job(job_id, owner, action):
    oid = _oid(job_id)
    j = _col().find_one({"_id": oid, "owner": owner}) if oid else None
    if not j:
        return {"error": "No scheduled email with that job_id. Call list_scheduled_emails first."}
    q = {"_id": oid, "owner": owner}
    if action == "cancel":
        res = _col().update_one({**q, "status": {"$in": ["pending", "running", "paused", "failed"]}},
                                {"$set": {"status": "cancelled"}})
    elif action == "pause":
        res = _col().update_one({**q, "status": "pending"}, {"$set": {"status": "paused"}})
    elif action == "resume":
        if j["cron"]:
            fix = {"status": "pending", "next_run_at": _next_cron(j["cron"]), "consecutive_failures": 0, "last_error": None}
            res = _col().update_one({**q, "status": {"$in": ["paused", "failed"]}}, {"$set": fix})
        elif _local(j["next_run_at"]) < datetime.now(TZ) - timedelta(minutes=1):
            return {"error": "That one-time email's time has passed. Schedule it again with a new time."}
        else:
            res = _col().update_one({**q, "status": "paused"}, {"$set": {"status": "pending"}})
    else:
        return {"error": f"action must be one of: {', '.join(ACTIONS)}."}
    if not res.modified_count:
        return {"error": f"That email cannot be changed that way while it is {j['status']}."}
    return {"message": {"cancel": "Cancelled.", "pause": "Paused.", "resume": "Resumed."}[action]}


# ---------------------------------------------------------------- worker
def _finish(job, ok, error=None):
    failures = 0 if ok else job.get("consecutive_failures", 0) + 1
    update = {"last_error": None if ok else (error or "")[:500], "consecutive_failures": failures,
              "last_run_at": _now()}
    if not job["cron"]:
        update["status"] = "done" if ok else "failed"
    elif failures >= MAX_CONSECUTIVE_FAILURES:
        update["status"] = "failed"
    else:
        # next run is computed from NOW, so a long outage replays only one run, not every missed tick
        update["status"] = "pending"
        update["next_run_at"] = _next_cron(job["cron"])
    ops = {"$set": update}
    if ok:
        ops["$inc"] = {"run_count": 1}
    _col().update_one({"_id": job["_id"]}, ops)


def execute_job(job):
    import agent   # lazy import: agent.py builds the Mongo schema catalog at import time

    if not job["cron"]:
        late = _now() - (job["next_run_at"] if job["next_run_at"].tzinfo else job["next_run_at"].replace(tzinfo=timezone.utc))
        if late > timedelta(hours=ONE_TIME_GRACE_HOURS):
            raise RuntimeError("The server was not running at the scheduled time, so it was not sent.")

    args = copy.deepcopy(job["report"]["args"])
    _apply_presets(args)                          # "last_month" becomes real dates NOW
    fc = SimpleNamespace(name=job["report"]["tool"], args=args, id=None)
    result, artifact = agent.run_tool_call(
        fc, session_id=f"sched-{job['_id']}", turn_id="run",
        user=job["owner"], export_ok=True, roles=job["roles"],   # run_tool_call re-applies role checks
    )
    error = result.get("error") if isinstance(result, dict) else None
    if error or not artifact:
        raise RuntimeError(error or "Report produced no file.")
    _smtp_send(job["recipient"], job["subject"], job["body"], [artifact["path"]])


def run_due_jobs():
    col = _col()
    # release jobs stuck in "running" (e.g. server crashed mid-send)
    col.update_many({"status": "running", "started_at": {"$lt": _now() - timedelta(minutes=STALE_RUNNING_MINUTES)}},
                    {"$set": {"status": "pending"}})
    count = 0
    while True:
        # atomic claim: safe even with several uvicorn workers
        job = col.find_one_and_update(
            {"status": "pending", "next_run_at": {"$lte": _now()}},
            {"$set": {"status": "running", "started_at": _now()}},
            sort=[("next_run_at", 1)], return_document=ReturnDocument.AFTER,
        )
        if not job:
            return count
        count += 1
        try:
            execute_job(job)
            _finish(job, ok=True)
            _notify(job, f"Your scheduled {job['report']['tool']} was sent to {job['recipient']} at {_fmt(_now())}.")
            logger.info(f"[scheduler] sent job {job['_id']} to {job['recipient']}")
        except Exception as e:
            logger.error(f"[scheduler] job {job['_id']} failed: {e}")
            _finish(job, ok=False, error=str(e))
            _notify(job, f"Your scheduled email to {job['recipient']} failed: {str(e)[:200]}")


def ensure_indexes():
    _col().create_index([("status", 1), ("next_run_at", 1)])
    _col().create_index([("owner", 1), ("status", 1)])


_thread = None


def start_in_background():
    """Starts the sender in a background thread (once). Call it at app startup."""
    global _thread
    if _thread and _thread.is_alive():
        return
    ensure_indexes()

    def loop():
        while True:
            try:
                run_due_jobs()
            except Exception:
                logger.exception("[scheduler] loop error")
            time.sleep(POLL_SECONDS)

    _thread = threading.Thread(target=loop, name="scheduler", daemon=True)
    _thread.start()
    logger.info("[scheduler] started")


async def scheduler_loop():
    """Async alternative: asyncio.create_task(scheduler_loop()) inside a running event loop."""
    ensure_indexes()
    while True:
        try:
            await asyncio.to_thread(run_due_jobs)
        except Exception:
            logger.exception("[scheduler] loop error")
        await asyncio.sleep(POLL_SECONDS)


# ---------------------------------------------------------------- agent tools
SCHEDULER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "schedule_report_email",
            "description": (
                "Schedule a report to be built and emailed later, once or on a repeat. Use ONLY when the user asks "
                "to send a report later, at a set time, or regularly; to send one now use prepare_report_email. "
                "The report is rebuilt with fresh data on every run. This only prepares the schedule: the user "
                "confirms with a button. For a range that should move with each run (last month, last 7 days), do "
                "NOT write dates: put date_field and date_preset (" + ", ".join(PRESETS) + ") inside report_args. "
                "Give exactly one of run_at, in_minutes or cron."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "recipient": {"type": "string", "description": "Email address typed by the user (or shown to them and agreed)."},
                    "subject": {"type": "string"},
                    "body": {"type": "string", "description": "Short plain-text message."},
                    "report_tool": {"type": "string", "enum": SCHEDULABLE_TOOLS},
                    "report_args": {
                        "type": "object",
                        "description": "The exact arguments you would give report_tool for an immediate export (same default columns), with date_preset instead of dates for moving ranges.",
                    },
                    "run_at": {"type": "string", "description": f"One time, 'YYYY-MM-DDTHH:MM' (24-hour) in {TZ.key}."},
                    "in_minutes": {"type": "integer", "description": "One time, this many minutes from now (120 for 'in 2 hours')."},
                    "cron": {"type": "string", "description": "Repeat: 5 fields, at most once a day, e.g. '0 9 * * 1' = every Monday at 9:00."},
                },
                "required": ["recipient", "report_tool", "report_args", "subject", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_scheduled_emails",
            "description": "Lists the user's scheduled report emails with status, next run time and last error. "
                           "Job ids are internal: describe a job by recipient and schedule, never show the id.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_scheduled_email",
            "description": "Cancel, pause or resume one of the user's scheduled report emails by job_id "
                           "(from list_scheduled_emails).",
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "action": {"type": "string", "enum": ACTIONS},
                },
                "required": ["job_id", "action"],
            },
        },
    },
]

SCHEDULER_TOOL_NAMES = {t["function"]["name"] for t in SCHEDULER_TOOLS}


def handle_scheduler_tool(name, args, user, roles, session_id=""):
    """Dispatcher to call from run_tool_call."""
    if not user:
        return {"error": "Not authenticated."}
    if roles is not None and not (set(roles) & SCHEDULER_ROLES):
        return {"error": f"Access denied: this user's role cannot use {name}. Tell the user they do not have permission for that action."}
    if name == "schedule_report_email":
        return create_job(user, roles, args, session_id=session_id)
    if name == "list_scheduled_emails":
        return list_jobs(user)
    if name == "manage_scheduled_email":
        return manage_job(args.get("job_id"), user, args.get("action"))
    return {"error": f"Unknown scheduler tool: {name}"}