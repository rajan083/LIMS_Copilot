import os, re, logging, json, time, smtplib, mimetypes
from db import db
from email.message import EmailMessage

logger= logging.getLogger("lims.email")

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
MAX_PER_HOUR= 10

_typed= {}
_files= {}
_choices= {}
_pending= {}
_sent= {}
_shown = {}

CONTACT_SOURCES = [
    {"collection": "app_user", "name_fields": ["name"], "org_field": None, "email_field": "email"},
    {"collection": "app_application_user", "name_fields": ["name"], "org_field": None, "email_field": "email"},
    {"collection": "app_lead_contact", "name_fields": ["name"], "org_field": "lead.name", "email_field": "email"},
    {"collection": "el_leads", "name_fields": ["name"], "org_field": None, "email_field": "email"},
    # orders last: biggest collection, many repeated contacts
    {"collection": "el_orders", "name_fields": ["reporting_contact_person"],
     "org_field": "reporting_company", "email_field": "reporting_contact_person_email", "scan": 50},
    {"collection": "el_orders", "name_fields": ["billing_contact_person"],
     "org_field": "billing_company", "email_field": "billing_contact_person_email", "scan": 50},
]


def note_user_message(session_id, text):
    known= _typed.setdefault(session_id, [])
    for a in EMAIL_RE.findall(text or ""):
        if a.lower() not in [k.lower() for k in known]:
            known.append(a)

def note_assistant_message(session_id, text):
    """Call with the final reply text that the user actually sees."""
    known = _shown.setdefault(session_id, [])
    for a in EMAIL_RE.findall(text or ""):
        if a.lower() not in [k.lower() for k in known]:
            known.append(a)


def note_artifact(session_id, path):
    _files.setdefault(session_id, []).append(path)


def _get(doc, dotted):
    for part in dotted.split("."):
        if not isinstance(doc, dict):
            return None
        doc = doc.get(part)
    return doc

def find_contact(query, limit=5):
    query = (query or "").strip()
    if not query:
        return []
    is_email = "@" in query
    results, seen = [], set()

    for src in CONTACT_SOURCES:
        fields = src["name_fields"] + ([src["org_field"]] if src["org_field"] else [])
        projection = {f: 1 for f in fields + [src["email_field"]]}
        coll = db[src["collection"]]

        if is_email:    # direct match on the email field, whole address, ignoring case
            cursor = coll.find({src["email_field"]: query}, projection) \
                         .collation({"locale": "en", "strength": 2}).limit(limit)
        else:           # names and companies: partial match
            pattern = {"$regex": re.escape(query), "$options": "i"}
            cursor = coll.find({"$or": [{f: pattern} for f in fields]}, projection).limit(limit)

        for doc in cursor:
            email = _get(doc, src["email_field"])
            if not isinstance(email, str) or "@" not in email or email.lower() in seen:
                continue
            seen.add(email.lower())
            results.append({
                "id": f'{src["collection"]}:{doc["_id"]}',
                "name": " ".join(str(_get(doc, f) or "") for f in src["name_fields"]).strip(),
                "org": (_get(doc, src["org_field"]) or "") if src["org_field"] else "",
                "email": email,
            })
    return results[:limit]


def _mask(email):
    user, _, domain= email.partition("@")
    return f"{user[:3]}***@{domain}"

def is_external(email):
    internal= os.environ.get("INTERNAL_DOMAIN", "").lower()
    return bool(internal) and not email.lower().endswith('@'+ internal)

def prepare_report_email(session_id, user=None, recipient=None, contact_id=None, file_path=None, subject="LIMS report", body="PFA report"):
    
    _pending.pop(session_id, None)
    
    files= _files.get(session_id, [])
    if not files:
        return {"error": "No report has been generated in this chat yet. Ask the user which report to create first."}
    
    path= files[-1]
    if file_path:
        want= os.path.basename(file_path.replace("\\", "/"))
        path= next((f for f in reversed(files) if os.path.basename(f) == want), path)
    if not os.path.exists(path):
        return {"error": "The report file is no longer available."}
    
    recipient= (recipient or "").strip()
    if contact_id:
        contact= _choices.get(session_id, {}).get(contact_id)
        if not contact:
            return {"error": "Unknown contact_id. Search by name again."}
        
    elif "@" in recipient:
        r = recipient.lower()
        typed_ok = r in [a.lower() for a in _typed.get(session_id, [])]
        shown_ok = r in [a.lower() for a in _shown.get(session_id, [])]
        if not (typed_ok or shown_ok):
            return {"error": "That address was not typed by the user or shown to them in this chat. Ask the user to type the address."}
        try:
            known = find_contact(recipient)
        except Exception:
            known = []
        contact = dict(known[0]) if known else {"name": "", "org": "", "email": recipient}
        contact["email"] = recipient
        
    elif recipient:
        matches= find_contact(recipient)
        if not matches:
            return {"error": f"No contact found for '{recipient}',. Ask the user for email address."}
        if len(matches)>1:
            _choices[session_id]= {m["id"]: m for m in matches}
            return {"status": "choose",
                    "matches": [{"contact_id": m["id"], "name": m["name"], "org": m["org"], "email": _mask(m["email"])} for m in matches],
                    "note": "Ask the user which one, then call again with that contact_id"
                    }
        contact= matches[0]
    else:
        return {"error": "No recipient given, Ask the user for a name or an email address."}
    
    _pending[session_id]= {"user": user,"contact": contact, "file": path, "subject": subject, "body": body}
    return {"status": "awaiting_user_confirmation",
            "message": (f"Prepared {os.path.basename(path)} for {contact['name'] or contact['email']}. The user must click the Sent button under the chat. Do NOT say the email was sent.")
            }
    
def get_pending_email(session_id):
    return _pending.get(session_id)

def cancel_pending_email(session_id):
    _pending.pop(session_id, None)
    
def _smtp_send(to, subject, body, path):
    msg = EmailMessage()
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    ctype, _ = mimetypes.guess_type(path)
    main, sub = (ctype or "application/octet-stream").split("/", 1)
    with open(path, "rb") as f:
        msg.add_attachment(f.read(), maintype=main, subtype=sub, filename=os.path.basename(path))
    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", 587))) as s:
        s.starttls()
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)
        
def confirm_and_send(session_id, user=None):
    p=_pending.pop(session_id, None)
    if not p or p.get("user") != user:
        return False, "Nothing to send."
    now=time.time()
    recent=[t for t in _sent.get(session_id, []) if now -t < 3600]
    if len(recent) >= MAX_PER_HOUR:
        return False, "Email limit reached for this hour."
    c=p["contact"]
    try:
        _smtp_send(c["email"], p["subject"], p["body"], p["file"])
    except Exception as e:
        logger.error(f"[email] faied to {c['email']} : {e}")
        return False, f"Count not send the email: {e}"
    _sent[session_id]= recent + [now]
    entry= {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "session": session_id, "user": user, "to": c["email"], "contact": c.get("name", ""), "file": os.path.basename(p["file"]),
            "external": is_external(c["email"])}
    logger.info(f"[email] SENT {json.dumps(entry)}")
    with open ("email_audit.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return True, f"Sent {entry['file']} to {c['name'] or c['email']}."
    