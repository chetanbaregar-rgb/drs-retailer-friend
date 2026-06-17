"""
server.py — DRS Retailer Friend web server.

Start locally:  uvicorn server:app --reload --port 8002
Open:           http://localhost:8002

On Railway the PORT env var is set automatically.
"""

import asyncio
import json
import os
import uuid
from pathlib import Path

import anthropic
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from persona import SYSTEM_PROMPT

# ── Bootstrap .env (local dev only — Railway uses env vars directly) ──────────
env = Path(__file__).parent / ".env"
if env.exists():
    for line in env.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

MODEL = "claude-sonnet-4-6"

client = anthropic.Anthropic()
app    = FastAPI()

# ── In-memory session store (keyed by session_id from browser localStorage) ───
# Each session holds a list of {role, content} message dicts.
# Sessions are lost on server restart — fine for a stateless public tool.
_sessions: dict[str, list] = {}

HISTORY_WINDOW = 20


def _get_session(session_id: str) -> list:
    if session_id not in _sessions:
        _sessions[session_id] = []
    return _sessions[session_id]


# ── API models ────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    session_id: str
    message: str


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    html = (Path(__file__).parent / "static" / "index.html").read_text()
    return HTMLResponse(html)


@app.post("/chat")
async def chat(req: ChatRequest):
    """Stream the assistant reply as Server-Sent Events."""
    messages = _get_session(req.session_id)
    messages.append({"role": "user", "content": req.message})
    recent = messages[-HISTORY_WINDOW:]

    async def event_stream():
        full_text = ""
        try:
            # Run the blocking Anthropic streaming call in a thread
            loop = asyncio.get_event_loop()

            def _stream():
                chunks = []
                with client.messages.stream(
                    model=MODEL,
                    max_tokens=1024,
                    system=[{
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }],
                    messages=recent,
                ) as stream:
                    for text in stream.text_stream:
                        chunks.append(text)
                return chunks

            chunks = await loop.run_in_executor(None, _stream)

            for chunk in chunks:
                full_text += chunk
                data = json.dumps({"text": chunk})
                yield f"data: {data}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            if full_text:
                messages.append({"role": "assistant", "content": full_text})
            yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/new-session")
async def new_session():
    session_id = str(uuid.uuid4())
    _sessions[session_id] = []
    return {"session_id": session_id}


# ── Static files ──────────────────────────────────────────────────────────────
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
