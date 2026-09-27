"""FastAPI backend: upload an IFC model, chat with a CodeAct agent about it.

Run: uv run uvicorn backend.app:app --port 8000
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import ifcopenshell
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.agent import Answer, create_ifc_agent, stream_turn
from backend.interpreter import JupyterInterpreter
from backend.llm import model_name

log = logging.getLogger("cobbie")

ROOT = Path(__file__).resolve().parent.parent
UPLOAD_DIR = ROOT / "data" / "uploads"


@dataclass
class Session:
    model_id: str
    filename: str
    path: Path
    ifc: ifcopenshell.file
    interp: JupyterInterpreter
    agent: object
    thread: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def thread_id(self) -> str:
        return f"{self.model_id}:{self.thread}"


SESSIONS: dict[str, Session] = {}


def _preamble(path: Path) -> str:
    return (
        "import ifcopenshell, ifcopenshell.util.element, ifcopenshell.util.unit\n"
        f"ifc_path = {str(path)!r}\n"
        "model = ifcopenshell.open(ifc_path)\n"
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    yield
    for s in list(SESSIONS.values()):
        s.interp.shutdown()
    SESSIONS.clear()


app = FastAPI(title="cobbie-demo", lifespan=lifespan)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "model": model_name(), "sessions": len(SESSIONS)}


def _create_session(filename: str, data: bytes) -> Session:
    model_id = uuid.uuid4().hex[:12]
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    path = UPLOAD_DIR / f"{model_id}.ifc"
    path.write_bytes(data)
    try:
        ifc = ifcopenshell.open(str(path))
    except Exception as exc:
        path.unlink(missing_ok=True)
        raise HTTPException(422, f"Not a readable IFC file: {exc}") from exc
    interp = JupyterInterpreter(preamble=_preamble(path))
    agent = create_ifc_agent(interp)
    return Session(model_id, filename, path, ifc, interp, agent)


@app.post("/api/models")
async def upload_model(file: UploadFile = File(...)) -> dict:
    filename = file.filename or "model.ifc"
    if not filename.lower().endswith(".ifc"):
        raise HTTPException(400, "Only .ifc files are accepted.")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file.")
    session = await run_in_threadpool(_create_session, filename, data)
    SESSIONS[session.model_id] = session
    return {
        "model_id": session.model_id,
        "filename": filename,
        "schema": session.ifc.schema,
        "n_products": len(session.ifc.by_type("IfcProduct")),
    }


def _get_session(model_id: str) -> Session:
    s = SESSIONS.get(model_id)
    if s is None:
        raise HTTPException(404, f"Unknown model_id {model_id!r}; upload the model again.")
    return s


@app.post("/api/models/{model_id}/reset")
async def reset_model(model_id: str) -> dict:
    s = _get_session(model_id)
    async with s.lock:
        await run_in_threadpool(s.interp.reset)
        s.thread += 1
    return {"model_id": model_id, "status": "reset"}


class ChatRequest(BaseModel):
    model_id: str
    message: str


def _enrich(ifc: ifcopenshell.file, answer: Answer) -> list[dict]:
    """Resolve GlobalIds against the in-process model; drop unknown ids and empty groups."""
    groups = []
    for g in answer.element_groups:
        seen: set[str] = set()
        elements = []
        for gid in g.global_ids:
            gid = gid.strip()
            if not gid or gid in seen:
                continue
            seen.add(gid)
            try:
                el = ifc.by_guid(gid)
            except Exception:
                continue
            if el is None:
                continue
            elements.append(
                {"global_id": gid, "ifc_class": el.is_a(), "name": getattr(el, "Name", None)}
            )
        if elements:
            groups.append({"label": g.label, "elements": elements})
    return groups


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.post("/api/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    s = _get_session(req.model_id)
    message = req.message.strip()
    if not message:
        raise HTTPException(400, "Empty message.")

    async def events():
        t0 = time.perf_counter()
        async with s.lock:
            try:
                async for ev in stream_turn(s.agent, s.thread_id, message):
                    if ev["type"] == "answer":
                        ans: Answer = ev["answer"]
                        ev = {
                            "type": "answer",
                            "text": ans.text,
                            "element_groups": _enrich(s.ifc, ans),
                            "sufficient_info": ans.sufficient_info,
                        }
                    yield _sse(ev)
            except Exception as exc:  # surface any agent failure to the UI
                log.exception("chat failed")
                yield _sse({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            yield _sse({"type": "done", "elapsed_s": round(time.perf_counter() - t0, 2)})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
