import asyncio
import copy
import logging
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from bson import ObjectId
from croniter import croniter
from pymongo import ReturnDocument

from db import db
from email_report import EMAIL_FULL_RE, _smtp_send

logger = logging.getLogger("lims.scheduler")

# ---------------------------------------------------------------- settings
TZ = ZoneInfo(os.environ.get("SCHEDULER_TZ", "Asia/Kolkata"))
MAX_JOBS_PER_USER = 10
MIN_INTERVAL_SECONDS = 24 * 3600      # recurring jobs may run at most once a day
MAX_CONSECUTIVE_FAILURES = 3
STALE_RUNNING_MINUTES = 15            # a job stuck in "running" this long is released
POLL_SECONDS = 30

SCHEDULABLE_TOOLS = [
    "export_records_to_excel",
    "export_joined_records_to_excel",
    "export_summary_to_excel",
    "export_records_to_pdf",
]
ACTIVE = ["awaiting_confirmation", "pending", "running", "paused"]
CHANGES = {                           # action -> (status it needs, status it moves to)
    "cancel": ({"$in": ACTIVE}, "cancelled"),
    "pause": ("pending", "paused"),
    "resume": ("paused", "pending"),
}


# ---------------------------------------------------------------- helpers
def _col():
    return db["scheduled_emails"]


def _now():
    return datetime.now(timezone.utc)


def _local(dt):
    return dt.replace(tzinfo=timezone.utc).astimezone(TZ)


def _next_cron(expr):
    return croniter(expr, _now().astimezone(TZ)).get_next(datetime).astimezone(timezone.utc)


def resolve_preset(preset):
    today = datetime.now(TZ).date()
    d = timedelta(days=1)
    first = today.replace(day=1)
    monday = today - timedelta(days=today.weekday())
    table = {
        "today": (today, today),
        "yesterday": (today - d, today - d),
        "last_7_days": (today - 7 * d, today - d),
        "last_30_days": (today - 30 * d, today - d),
        "last_week": (monday - 7 * d, monday - d),
        "month_to_date": (first, today),
        "last_month": ((first - d).replace(day=1), first - d),
    }
    if preset not in table:
        raise ValueError(f"Unknown date_preset '{preset}'. Use one of: {', '.join(table)}")
    return tuple(x.isoformat() for x in table[preset])


def _apply_presets(node):
    """Replace every {"date_preset": X} in the args tree with date_from/date_to."""
    if isinstance(node, dict):
        preset = node.pop("date_preset", None)
        if preset:
            node["date_from"], node["date_to"] = resolve_preset(preset)
        for v in node.values():
            _apply_presets(v)
    elif isinstance(node, list):
        for v in node:
            _apply_presets(v)


def _set_status(job_id, owner, current, new):
    if not ObjectId.is_valid(job_id):
        return False
    res = _col().update_one({"_id": ObjectId(job_id), "owner": owner, "status": current},
                            {"$set": {"status": new}})
    return res.modified_count > 0


# ---------------------------------------------------------------- jobs
def create_job(owner, roles, a):
    """Store a job in 'awaiting_confirmation'. Nothing runs until confirm_job()."""
    tool, args = a.get("report_tool"), a.get("report_args") or {}
    recipient = (a.get("recipient") or "").strip()
    run_at, cron = a.get("run_at"), a.get("cron")

    if tool not in SCHEDULABLE_TOOLS:
        return {"error": f"Cannot schedule '{tool}'. Allowed: {SCHEDULABLE_TOOLS}"}
    if not EMAIL_FULL_RE.match(recipient):
        return {"error": "A valid recipient email address is required."}
    if bool(run_at) == bool(cron):
        return {"error": "Provide exactly one of run_at (one-time) or cron (recurring)."}
    try:
        _apply_presets(copy.deepcopy(args))                 # rejects unknown date presets
        if run_at:
            dt = datetime.fromisoformat(run_at)
            next_run = (dt if dt.tzinfo else dt.replace(tzinfo=TZ)).astimezone(timezone.utc)
            if next_run < _now() - timedelta(minutes=1):
                return {"error": "run_at is in the past."}
        else:
            it = croniter(cron, _now().astimezone(TZ))
            first, second = it.get_next(datetime), it.get_next(datetime)
            if (second - first).total_seconds() < MIN_INTERVAL_SECONDS:
                return {"error": "Recurring reports can run at most once per day."}
            next_run = _next_cron(cron)
    except ValueError as e:
        return {"error": f"Invalid schedule: {e}"}

    if _col().count_documents({"owner": owner, "status": {"$in": ACTIVE}}) >= MAX_JOBS_PER_USER:
        return {"error": f"Limit of {MAX_JOBS_PER_USER} scheduled emails reached. Cancel one first."}

    job_id = _col().insert_one({
        "owner": owner,
        "roles": list(roles) if roles is not None else None,
        "recipient": recipient,
        "subject": " ".join(str(a.get("subject") or "LIMS report").split())[:150],
        "body": str(a.get("body") or "PFA report")[:2000],
        "report": {"tool": tool, "args": args},
        "cron": cron or None,
        "status": "awaiting_confirmation",
        "next_run_at": next_run,
        "consecutive_failures": 0,
        "last_error": None,
    }).inserted_id
    return {
        "status": "awaiting_user_confirmation",   # handle_turn already stops on this value
        "job_id": str(job_id),
        "summary": (f"{'Recurring (' + cron + ')' if cron else 'One-time'} {tool} to {recipient}; "
                    f"first run {_local(next_run).strftime('%a %d %b %Y, %I:%M %p')} ({TZ.key})."),
    }


def confirm_job(job_id, owner):
    """Call from the FastAPI confirm endpoint when the user clicks Confirm."""
    ok = _set_status(job_id, owner, "awaiting_confirmation", "pending")
    return {"message": "Scheduled."} if ok else {"error": "Nothing to confirm."}


def list_jobs(owner):
    jobs = _col().find({"owner": owner, "status": {"$in": ACTIVE + ["failed"]}}).sort("next_run_at", 1)
    return {"jobs": [{
        "job_id": str(j["_id"]),
        "status": j["status"],
        "recipient": j["recipient"],
        "report_tool": j["report"]["tool"],
        "schedule": j["cron"] or "once",
        "next_run": _local(j["next_run_at"]).strftime("%d %b %Y %I:%M %p"),
        "last_error": j["last_error"],
    } for j in jobs]}


# ---------------------------------------------------------------- worker
def _finish(job, ok, error=None):
    failures = 0 if ok else job.get("consecutive_failures", 0) + 1
    update = {"last_error": error, "consecutive_failures": failures}
    if not job["cron"]:
        update["status"] = "done" if ok else "failed"
    elif failures >= MAX_CONSECUTIVE_FAILURES:
        update["status"] = "failed"
    else:
        # next run is computed from NOW, so a long outage replays only one run, not every missed tick
        update["status"] = "pending"
        update["next_run_at"] = _next_cron(job["cron"])
    _col().update_one({"_id": job["_id"]}, {"$set": update})


def execute_job(job):
    import agent   # lazy import: agent.py builds the Mongo schema catalog at import time

    args = copy.deepcopy(job["report"]["args"])
    _apply_presets(args)                          # "last_month" becomes real dates NOW
    fc = SimpleNamespace(name=job["report"]["tool"], args=args, id=None)
    result, artifact = agent.run_tool_call(
        fc, session_id=f"sched-{job['_id']}", turn_id="run",
        user=None, export_ok=True, roles=job["roles"],   # run_tool_call re-applies role checks
    )
    if not artifact:
        raise RuntimeError(result.get("error") or "Report produced no file.")
    _smtp_send(job["recipient"], job["subject"], job["body"], [artifact["path"]])


def run_due_jobs():
    col = _col()
    # release jobs stuck in "running" (e.g. server crashed mid-send)
    col.update_many({"status": "running", "started_at": {"$lt": _now() - timedelta(minutes=STALE_RUNNING_MINUTES)}},
                    {"$set": {"status": "pending"}})
    while True:
        # atomic claim: safe even with several uvicorn workers
        job = col.find_one_and_update(
            {"status": "pending", "next_run_at": {"$lte": _now()}},
            {"$set": {"status": "running", "started_at": _now()}},
            sort=[("next_run_at", 1)], return_document=ReturnDocument.AFTER,
        )
        if not job:
            return
        try:
            execute_job(job)
            _finish(job, ok=True)
            logger.info(f"[scheduler] sent job {job['_id']} to {job['recipient']}")
        except Exception as e:
            logger.error(f"[scheduler] job {job['_id']} failed: {e}")
            _finish(job, ok=False, error=str(e))


async def scheduler_loop():
    _col().create_index([("status", 1), ("next_run_at", 1)])
    _col().create_index([("owner", 1), ("status", 1)])
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
                "Schedule a report to be generated and emailed later, once or on a repeating schedule. "
                "The report is rebuilt with fresh data every time it runs. Use ONLY when the user asks to "
                "send/email a report later or on a schedule. Never write literal dates in report_args for "
                "relative ranges; use date_preset (today, yesterday, last_7_days, last_30_days, last_week, "
                "month_to_date, last_month) next to date_field instead. Give exactly one of run_at or cron."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "recipient": {"type": "string", "description": "Email address to send to."},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                    "report_tool": {"type": "string", "enum": SCHEDULABLE_TOOLS},
                    "report_args": {
                        "type": "object",
                        "description": "Exact arguments for report_tool, with date_preset for relative dates.",
                    },
                    "run_at": {
                        "type": "string",
                        "description": f"One-time ISO 8601 datetime in {TZ.key}, e.g. 2026-11-03T09:00:00.",
                    },
                    "cron": {
                        "type": "string",
                        "description": "5-field cron for recurring runs, e.g. '0 9 * * 1' = Mondays 9am.",
                    },
                },
                "required": ["recipient", "report_tool", "report_args"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_scheduled_emails",
            "description": "List the user's scheduled report emails with status, next run time and last error.",
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
                    "action": {"type": "string", "enum": list(CHANGES)},
                },
                "required": ["job_id", "action"],
            },
        },
    },
]

SCHEDULER_TOOL_NAMES = {t["function"]["name"] for t in SCHEDULER_TOOLS}


def handle_scheduler_tool(name, args, user, roles):
    """Dispatcher to call from run_tool_call."""
    if not user:
        return {"error": "Not authenticated."}
    if name == "schedule_report_email":
        return create_job(user, roles, args)
    if name == "list_scheduled_emails":
        return list_jobs(user)
    if name == "manage_scheduled_email" and args.get("action") in CHANGES:
        done = _set_status(args.get("job_id"), user, *CHANGES[args["action"]])
        return {"message": "Done."} if done else {"error": "No matching scheduled email (check the job_id and its status)."}
    return {"error": f"Unknown scheduler tool or action: {name}"}