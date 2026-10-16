import os
import re
import uuid
import time
import hmac
import threading
import logging

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Header, HTTPException, Depends, Request
from fastapi.responses import FileResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

import keystore
from agent import new_conversation, handle_turn, transcribe_bytes, TOOL_ROLES
from report_email import (
    get_pending_email, confirm_and_send, cancel_pending_email, clear_session, is_external,
)

logger = logging.getLogger("lims.api")

# ---------------------------------------------------------------- settings
ENABLE_DOCS = os.environ.get("ENABLE_DOCS", "0") == "1"           # /docs page, keep off in production
ENABLE_ADMIN = os.environ.get("ENABLE_ADMIN_API", "0") == "1"     # /admin/keys endpoints, off by default
ADMIN_KEY = os.environ.get("ADMIN_API_KEY")
if ENABLE_ADMIN and not ADMIN_KEY:
    raise RuntimeError("ENABLE_ADMIN_API=1 requires ADMIN_API_KEY to be set")

CONVERSATION_TTL = 2 * 3600        # idle conversations (and their files) are dropped after 2 hours
MAX_MESSAGE_CHARS = 4000
USER_ID_RE = re.compile(r"^[\w.@:+-]{1,128}$")

MAX_AUDIO_BYTES= 5_000_000

keystore.init()

app = FastAPI(
    title="LIMS Copilot API",
    version="1.0",
    docs_url="/docs" if ENABLE_DOCS else None,
    redoc_url=None,
    openapi_url="/openapi.json" if ENABLE_DOCS else None,
)

# ---------------------------------------------------------------- state (in memory)
_conversations = {}      # (user, conv_id) -> {"messages", "lock", "updated"}
_artifacts = {}          # artifact_id -> {"user", "path", "type", "created"}
_store_lock = threading.Lock()


# ---------------------------------------------------------------- auth
def authenticate(request: Request, x_api_key: str = Header(None), x_user_id: str = Header(None)):
    app_name = keystore.verify_key(x_api_key)
    if not app_name:
        raise HTTPException(401, "Invalid, expired or revoked API key")
    if not x_user_id or not USER_ID_RE.match(x_user_id.strip()):
        raise HTTPException(400, "X-User-Id header is required (letters, digits and . _ @ : + - only, max 128)")
    request.state.app = app_name
    # users from different applications can never collide
    return f"{app_name}:{x_user_id.strip()}"

def get_roles(x_user_roles: str= Header(None)):
    if x_user_roles is None:
        return None
    return [r.strip().lower() for r in x_user_roles.split(",") if r.strip()]

def require_admin(x_admin_key: str = Header(None)):
    if not x_admin_key or not hmac.compare_digest(x_admin_key.encode(), ADMIN_KEY.encode()):
        raise HTTPException(401, "Invalid admin key")


# ---------------------------------------------------------------- helpers
def _session_id(user, conv_id):
    return f"{user}:{conv_id}"


def _get_conv(user, conv_id):
    conv = _conversations.get((user, conv_id))        # keyed by user: nobody else can reach it
    if not conv:
        raise HTTPException(404, "Conversation not found")
    return conv


def _cleanup():
    now = time.time()
    with _store_lock:
        for key in [k for k, v in _conversations.items() if now - v["updated"] > CONVERSATION_TTL]:
            del _conversations[key]
            clear_session(_session_id(*key))
        for art_id in [a for a, v in _artifacts.items() if now - v["created"] > CONVERSATION_TTL]:
            del _artifacts[art_id]


def _public_artifacts(user, artifacts):
    out = []
    for a in artifacts:
        if not os.path.exists(a["path"]):
            continue
        art_id = uuid.uuid4().hex
        _artifacts[art_id] = {"user": user, "path": a["path"], "type": a["type"], "created": time.time()}
        out.append({
            "id": art_id,
            "type": a["type"],
            "filename": os.path.basename(a["path"]),
            "download_url": f"/v1/artifacts/{art_id}",
        })
    return out                                         # server file paths are never sent to the client


def _public_pending(user, session_id):
    p = get_pending_email(session_id, user=user)
    if not p:
        return None
    c = p["contact"]
    return {
        "to": c["email"],
        "name": c.get("name", ""),
        "org": c.get("org", ""),
        "external": is_external(c["email"]),
        "file": os.path.basename(p["file"]),
        "files": [os.path.basename(f) for f in p["files"]],
        "subject": p["subject"],
        "body": p["body"],
    }


class MessageIn(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


# ---------------------------------------------------------------- endpoints
@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/v1/conversations")
def create_conversation(user: str = Depends(authenticate)):
    _cleanup()
    conv_id = uuid.uuid4().hex
    with _store_lock:
        _conversations[(user, conv_id)] = {
            "messages": new_conversation(),
            "lock": threading.Lock(),
            "updated": time.time(),
        }
    return {"conversation_id": conv_id}


@app.post("/v1/conversations/{conv_id}/messages")
def send_message(conv_id: str, body: MessageIn, user: str = Depends(authenticate), roles=Depends(get_roles)):
    conv = _get_conv(user, conv_id)
    if not conv["lock"].acquire(blocking=False):       # one turn at a time per conversation
        raise HTTPException(409, "The previous message is still being processed")
    try:
        sid = _session_id(user, conv_id)
        try:
            reply, conv["messages"], artifacts = handle_turn(
                conv["messages"], body.message, session_id=sid, user=user, roles=roles)
        except Exception:
            logger.exception("handle_turn failed")
            raise HTTPException(500, "The assistant could not process this message")
        conv["updated"] = time.time()
        return {
            "reply": reply,
            "artifacts": _public_artifacts(user, artifacts),
            "pending_email": _public_pending(user, sid),
        }
    finally:
        conv["lock"].release()


@app.post("/v1/transcribe")
async def transcribe(request: Request, user: str= Depends(authenticate)):
    audio= await request.body()
    if not audio or len(audio)> MAX_AUDIO_BYTES:
        raise HTTPException(400, "Missing or too long recording")
    try:
        text= await run_in_threadpool(transcribe_bytes, audio)
    except Exception:
        logger.exception("transcription failed")
        raise HTTPException(502, "Transcription failed")
    return {"text": text.strip()}



@app.get("/v1/artifacts/{artifact_id}")
def download_artifact(artifact_id: str, user: str = Depends(authenticate)):
    a = _artifacts.get(artifact_id)
    if not a or a["user"] != user or not os.path.exists(a["path"]):
        raise HTTPException(404, "Artifact not found")
    return FileResponse(a["path"], filename=os.path.basename(a["path"]))


@app.get("/v1/conversations/{conv_id}/email/pending")
def email_pending(conv_id: str, user: str = Depends(authenticate)):
    _get_conv(user, conv_id)
    return {"pending_email": _public_pending(user, _session_id(user, conv_id))}


@app.post("/v1/conversations/{conv_id}/email/confirm")
def email_confirm(conv_id: str, user: str = Depends(authenticate), roles= Depends(get_roles)):
    if roles is not None and not (set(roles) & TOOL_ROLES['prepare_report_email']):
        raise HTTPException(403, "Your role is not allowed to send reports")
    _get_conv(user, conv_id)
    ok, msg = confirm_and_send(_session_id(user, conv_id), user=user)
    return {"ok": ok, "message": msg}


@app.post("/v1/conversations/{conv_id}/email/cancel")
def email_cancel(conv_id: str, user: str = Depends(authenticate)):
    _get_conv(user, conv_id)
    cancel_pending_email(_session_id(user, conv_id), user=user)
    return {"ok": True}


@app.delete("/v1/conversations/{conv_id}")
def delete_conversation(conv_id: str, user: str = Depends(authenticate)):
    with _store_lock:
        if _conversations.pop((user, conv_id), None) is None:
            raise HTTPException(404, "Conversation not found")
    clear_session(_session_id(user, conv_id))
    return {"ok": True}


# ---------------------------------------------------------------- admin (off unless ENABLE_ADMIN_API=1)
if ENABLE_ADMIN:
    class KeyIn(BaseModel):
        app_name: str = Field(min_length=2, max_length=60)
        days_valid: int | None = Field(default=None, ge=1, le=3650)

    @app.post("/admin/keys")
    def admin_create_key(body: KeyIn, _: None = Depends(require_admin)):
        return keystore.create_key(body.app_name, body.days_valid)     # plaintext key is shown only here

    @app.get("/admin/keys")
    def admin_list_keys(_: None = Depends(require_admin)):
        return keystore.list_keys()                                    # no secrets, only a prefix

    @app.delete("/admin/keys/{key_id}")
    def admin_revoke_key(key_id: str, _: None = Depends(require_admin)):
        if not keystore.revoke_key(key_id):
            raise HTTPException(404, "Key not found")
        return {"ok": True}