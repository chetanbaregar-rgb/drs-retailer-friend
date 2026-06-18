"""
server.py — RetEarn Ready web server.

Start locally:  uvicorn server:app --reload --port 8002
Open:           http://localhost:8002

On Railway the PORT env var is set automatically.

Privacy by design
------------------
- A visitor can use the chat fully WITHOUT consenting to anything. The DRS
  advice never depends on us learning about them, so consent is never bundled
  into the service (a UK GDPR requirement).
- Only if a visitor consents do we (a) let the assistant gently learn about
  their business and (b) persist a small profile + the chat to disk.
- A visitor who declines leaves no persistent identifier: their session lives
  in memory only and is gone on restart.
- Visitors can view their stored data (/my-data) and erase it (/delete-my-data)
  at any time — the right of access and the right to erasure.
"""

from __future__ import annotations

import asyncio
import json
import os
import smtplib
import time
import urllib.parse
import urllib.request
import uuid
from email.mime.text import MIMEText
from pathlib import Path

import anthropic
from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import profiling
from persona import NO_PROFILING_NOTE, PROFILING_GUIDANCE, SYSTEM_PROMPT

# ── Bootstrap .env (local dev only — Railway uses env vars directly) ──────────
env = Path(__file__).parent / ".env"
if env.exists():
    for line in env.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

MODEL = "claude-sonnet-4-6"

# How long we keep a consenting visitor's stored data. Cleared on next access
# after this window. Operator: adjust to your stated retention period.
RETENTION_DAYS = 365

client = anthropic.Anthropic()
app    = FastAPI()

# ── Session store ─────────────────────────────────────────────────────────────
# session_id -> {messages: [...], profile: {}, consent: bool, created: ts}
# Consenting sessions are mirrored to disk under data/visitors/<id>.json so the
# profile survives a restart; declining sessions stay in memory only.
_sessions: dict[str, dict] = {}

HISTORY_WINDOW = 20
DATA_DIR       = Path(__file__).parent / "data" / "visitors"


def _new_record() -> dict:
    return {"messages": [], "profile": {}, "consent": False, "created": time.time()}


def _get_session(session_id: str) -> dict:
    if session_id not in _sessions:
        # Try to rehydrate a previously-persisted (consented) session.
        rec = _load_record(session_id)
        _sessions[session_id] = rec or _new_record()
    return _sessions[session_id]


# ── Persistence (consenting visitors only) ─────────────────────────────────────

def _path_for(session_id: str) -> Path:
    safe = uuid.UUID(session_id)  # raises if not a valid uuid → no path traversal
    return DATA_DIR / f"{safe}.json"


def _persist(session_id: str, rec: dict) -> None:
    if not rec.get("consent"):
        return
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _path_for(session_id).write_text(json.dumps({
            "profile":   rec.get("profile", {}),
            "consent":   True,
            "created":   rec.get("created"),
            "updated":   time.time(),
            "messages":  rec.get("messages", [])[-HISTORY_WINDOW:],
        }, indent=2))
    except Exception:
        pass


def _load_record(session_id: str) -> dict | None:
    try:
        p = _path_for(session_id)
    except Exception:
        return None
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except Exception:
        return None
    # Enforce retention: drop anything older than the window.
    if time.time() - data.get("updated", data.get("created", 0)) > RETENTION_DAYS * 86400:
        try:
            p.unlink()
        except Exception:
            pass
        return None
    return {
        "messages": data.get("messages", []),
        "profile":  data.get("profile", {}),
        "consent":  data.get("consent", False),
        "created":  data.get("created", time.time()),
    }


def _erase(session_id: str) -> None:
    _sessions.pop(session_id, None)
    try:
        p = _path_for(session_id)
        if p.exists():
            p.unlink()
    except Exception:
        pass


# ── API models ────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    session_id: str
    message: str


class ConsentRequest(BaseModel):
    session_id: str
    consent: bool


class SessionRequest(BaseModel):
    session_id: str


class RegisterRequest(BaseModel):
    session_id: str
    name: str
    email: str
    store_name: str = ""


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    html = (Path(__file__).parent / "static" / "index.html").read_text()
    return HTMLResponse(html)


@app.get("/privacy", response_class=HTMLResponse)
async def privacy():
    return FileResponse(Path(__file__).parent / "static" / "privacy.html")


@app.post("/consent")
async def consent(req: ConsentRequest):
    rec = _get_session(req.session_id)
    rec["consent"] = bool(req.consent)
    if req.consent:
        _persist(req.session_id, rec)
    else:
        # Withdrawing consent erases anything we'd stored.
        _erase(req.session_id)
        _sessions[req.session_id] = _new_record()
        _sessions[req.session_id]["consent"] = False
    return {"ok": True, "consent": bool(req.consent)}


@app.post("/chat")
async def chat(req: ChatRequest):
    """Stream the assistant reply as Server-Sent Events."""
    rec = _get_session(req.session_id)
    messages = rec["messages"]
    messages.append({"role": "user", "content": req.message})
    recent = messages[-HISTORY_WINDOW:]

    # Behaviour switches purely on consent. Base prompt stays cached; the small
    # consent-dependent note is a second system block.
    addendum = PROFILING_GUIDANCE if rec.get("consent") else NO_PROFILING_NOTE

    async def event_stream():
        full_text = ""
        try:
            loop = asyncio.get_event_loop()

            def _stream():
                chunks = []
                with client.messages.stream(
                    model=MODEL,
                    max_tokens=1024,
                    system=[
                        {"type": "text", "text": SYSTEM_PROMPT,
                         "cache_control": {"type": "ephemeral"}},
                        {"type": "text", "text": addendum},
                    ],
                    messages=recent,
                ) as stream:
                    for text in stream.text_stream:
                        chunks.append(text)
                return chunks

            chunks = await loop.run_in_executor(None, _stream)

            for chunk in chunks:
                full_text += chunk
                yield f"data: {json.dumps({'text': chunk})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            if full_text:
                messages.append({"role": "assistant", "content": full_text})
            yield "data: [DONE]\n\n"

            # Profile + persist in the background, only with consent. The client
            # has already received [DONE], so this never delays the reply.
            if rec.get("consent"):
                asyncio.get_event_loop().run_in_executor(
                    None, _update_profile, req.session_id
                )

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _send_to_google_sheet(name: str, email: str, store_name: str) -> None:
    """POST lead to Google Apps Script webhook. Silently skips if URL not configured."""
    gas_url = os.environ.get("GAS_WEBHOOK_URL")
    if not gas_url:
        return
    try:
        data = urllib.parse.urlencode({
            "name":       name,
            "email":      email,
            "store_name": store_name,
            "timestamp":  time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        }).encode()
        req = urllib.request.Request(gas_url, data=data, method="POST")
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def _send_lead_email(name: str, email: str, store_name: str) -> None:
    """Fire-and-forget email to notify retearn of a new signup. Silently skips if SMTP not configured."""
    notify_to   = os.environ.get("NOTIFY_EMAIL")
    smtp_host   = os.environ.get("SMTP_HOST")
    smtp_port   = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user   = os.environ.get("SMTP_USER")
    smtp_pass   = os.environ.get("SMTP_PASS")

    if not all([notify_to, smtp_host, smtp_user, smtp_pass]):
        return

    body = (
        f"New DRS Advisor signup\n"
        f"{'─' * 32}\n"
        f"Name:        {name}\n"
        f"Email:       {email}\n"
        f"Store name:  {store_name or 'not provided'}\n"
        f"Time:        {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}\n"
    )

    msg = MIMEText(body)
    msg["Subject"] = f"New lead: {name} ({store_name}) — DRS Advisor"
    msg["From"]    = smtp_user
    msg["To"]      = notify_to

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as s:
            s.starttls()
            s.login(smtp_user, smtp_pass)
            s.sendmail(smtp_user, [notify_to], msg.as_string())
    except Exception:
        pass  # Never let email failure break the signup response


def _update_profile(session_id: str) -> None:
    rec = _sessions.get(session_id)
    if not rec or not rec.get("consent"):
        return
    rec["profile"] = profiling.extract_profile(rec["messages"], rec.get("profile"))
    _persist(session_id, rec)


@app.post("/new-session")
async def new_session():
    session_id = str(uuid.uuid4())
    _sessions[session_id] = _new_record()
    return {"session_id": session_id}


@app.post("/my-data")
async def my_data(req: SessionRequest):
    """Right of access — show the visitor exactly what we hold on them."""
    rec = _sessions.get(req.session_id) or _load_record(req.session_id)
    if not rec:
        return {"consent": False, "profile": {}, "messages_stored": 0}
    return {
        "consent":         rec.get("consent", False),
        "profile":         rec.get("profile", {}),
        "messages_stored": len(rec.get("messages", [])) if rec.get("consent") else 0,
        "retention_days":  RETENTION_DAYS,
    }


@app.post("/register")
async def register(req: RegisterRequest):
    """Explicit account creation — stores data under confirmed consent."""
    import re
    if not req.name.strip() or not re.match(r"[^@]+@[^@]+\.[^@]+", req.email):
        return JSONResponse({"ok": False, "error": "Please provide a valid name and email."}, status_code=400)

    name       = req.name.strip()
    email      = req.email.strip().lower()
    store_name = req.store_name.strip()

    rec = _get_session(req.session_id)
    rec["consent"] = True
    rec["profile"].update({
        "contact_name":  name,
        "contact_email": email,
        "store_name":    store_name,
    })
    _persist(req.session_id, rec)

    # Append to leads CSV (persistent volume backup)
    leads_file = Path(__file__).parent / "data" / "leads.csv"
    leads_file.parent.mkdir(exist_ok=True)
    if not leads_file.exists():
        leads_file.write_text("session_id,name,email,store_name,registered_at\n")
    with leads_file.open("a", encoding="utf-8") as f:
        import csv as _csv, io as _io
        buf = _io.StringIO()
        _csv.writer(buf).writerow([
            req.session_id, name, email, store_name,
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        ])
        f.write(buf.getvalue())

    # Google Sheet + email — both fire in background, never delay the response
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _send_to_google_sheet, name, email, store_name)
    loop.run_in_executor(None, _send_lead_email, name, email, store_name)

    return {"ok": True}


@app.post("/delete-my-data")
async def delete_my_data(req: SessionRequest):
    """Right to erasure — wipe everything we hold and start fresh."""
    _erase(req.session_id)
    return {"ok": True}


# ── Static files ──────────────────────────────────────────────────────────────
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
