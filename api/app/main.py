import logging
import os
import re
import time
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Header, HTTPException, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse, PlainTextResponse  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402
from langgraph.checkpoint.postgres import PostgresSaver  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from . import db, graph  # noqa: E402

log = logging.getLogger("adforge")
logging.basicConfig(level=logging.INFO)
APP = {}
ID_RE = re.compile(r"^[\w.@:+-]{1,128}$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_schema()
    saver = PostgresSaver(db.pool())
    saver.setup()
    APP["graph"] = graph.build(saver)
    yield
    db.pool().close()


app = FastAPI(title="AdForge Agent API", version="1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class ChatIn(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=128)
    thread_id: str = Field(..., min_length=1, max_length=128)
    message: str = Field(..., min_length=1, max_length=8000)


def _error(status, reply):
    return JSONResponse(status_code=status, content={"reply": reply, "tool_calls": [], "tokens": {"in": 0, "out": 0}})


@app.exception_handler(HTTPException)
async def http_exc(_: Request, exc: HTTPException):
    return _error(exc.status_code, str(exc.detail))


@app.exception_handler(Exception)
async def any_exc(_: Request, exc: Exception):
    log.exception("unhandled")
    return _error(500, "Sorry, something went wrong on our side. Please try again.")


@app.get("/health", response_class=PlainTextResponse)
def health():
    return "ok"


@app.get("/", response_class=PlainTextResponse)
def root():
    return "AdForge agent API. POST /chat, GET /health"


@app.post("/chat")
def chat(body: dict):
    try:
        req = ChatIn(**{k: (str(v).strip() if v is not None else v) for k, v in (body or {}).items()
                        if k in ("user_id", "thread_id", "message")})
    except Exception:
        return _error(400, "Please send JSON with non-empty user_id, thread_id and message.")
    if not ID_RE.match(req.user_id) or not ID_RE.match(req.thread_id):
        return _error(400, "user_id and thread_id may only contain letters, digits and . _ - @ : +")

    t0 = time.time()
    # Namespacing the checkpoint key by user means two users can never share a thread.
    config = {"configurable": {"thread_id": f"{req.user_id}::{req.thread_id}"}}
    g = APP["graph"]
    try:
        before = len((g.get_state(config).values or {}).get("messages", []))
        state = g.invoke({"messages": [HumanMessage(content=req.message)], "user_id": req.user_id}, config)
    except Exception as e:
        log.exception("graph failed")
        reply = "Sorry, I hit a temporary problem generating that. Please try again in a moment."
        db.log_chat(user_id=req.user_id, thread_id=req.thread_id, message=req.message, reply=reply,
                    tool_calls=[], tokens_in=0, tokens_out=0, latency_ms=int((time.time() - t0) * 1000),
                    blocked=False, error=f"{type(e).__name__}: {e}"[:500])
        return _error(200, reply)

    new = state["messages"][before:]
    tin = tout = 0
    calls, by_id = [], {}
    reply = ""
    for m in new:
        if isinstance(m, AIMessage):
            u = m.usage_metadata or {}
            tin += u.get("input_tokens", 0)
            tout += u.get("output_tokens", 0)
            for tc in m.tool_calls:
                entry = {"name": tc["name"], "args": tc.get("args", {})}
                by_id[tc["id"]] = entry
                calls.append(entry)
            txt = graph.text_of(m).strip()
            if txt:
                reply = txt
        elif isinstance(m, ToolMessage) and m.tool_call_id in by_id:
            out = m.content if isinstance(m.content, str) else str(m.content)
            by_id[m.tool_call_id]["output"] = out[:2000]
    if not reply:
        reply = "Done."
    latency = int((time.time() - t0) * 1000)
    try:
        db.log_chat(user_id=req.user_id, thread_id=req.thread_id, message=req.message, reply=reply,
                    tool_calls=[{"name": c["name"], "args": c["args"]} for c in calls], tokens_in=tin,
                    tokens_out=tout, latency_ms=latency, blocked=bool(state.get("blocked")))
    except Exception:
        log.exception("log failed")
    return {"reply": reply, "tool_calls": calls, "tokens": {"in": tin, "out": tout}}


# ---------------- read-only data for the website (scoped by user_id) ----------------

@app.get("/users/{user_id}/campaigns")
def my_campaigns(user_id: str):
    return {"campaigns": db.list_campaigns(user_id)}


@app.get("/users/{user_id}/profile")
def my_profile(user_id: str):
    return {"profile": db.get_profile(user_id)}


@app.get("/users/{user_id}/threads")
def my_threads(user_id: str):
    return {"threads": db.user_threads(user_id)}


@app.get("/users/{user_id}/threads/{thread_id}")
def my_thread(user_id: str, thread_id: str):
    return {"messages": db.thread_history(user_id, thread_id)}


# ---------------- admin ----------------

def _admin(token: str | None):
    expected = os.getenv("ADMIN_TOKEN")
    if not expected or token != expected:
        raise HTTPException(status_code=401, detail="Invalid admin token")


@app.get("/admin/overview")
def admin_overview(x_admin_token: str | None = Header(default=None)):
    _admin(x_admin_token)
    return db.admin_overview()


@app.get("/admin/users/{user_id}")
def admin_user(user_id: str, x_admin_token: str | None = Header(default=None)):
    _admin(x_admin_token)
    return {"profile": db.get_profile(user_id), "campaigns": db.list_campaigns(user_id),
            "threads": db.user_threads(user_id)}
