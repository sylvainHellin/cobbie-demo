"""FastAPI backend: upload an IFC model, chat with a CodeAct agent about it.

Run: uv run uvicorn backend.app:app --port 8000
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import ifcopenshell
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from backend.agent import Answer, create_ifc_agent, stream_turn
from backend.interpreter import JupyterInterpreter
from backend.llm import init_llm, model_name
from backend.prompts import NUDGE_MSG, PromptVariant, get_variant

log = logging.getLogger("cobbie")

ROOT = Path(__file__).resolve().parent.parent
UPLOAD_DIR = ROOT / "data" / "uploads"
SAMPLES_DIR = ROOT / "samples"


@dataclass
class Session:
    model_id: str
    filename: str
    path: Path
    ifc: ifcopenshell.file
    interp: JupyterInterpreter
    agent: object
    variant: PromptVariant
    thread: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def thread_id(self) -> str:
        return f"{self.model_id}:{self.thread}"


SESSIONS: dict[str, Session] = {}

# Kernel-variable handoff: the agent stores answer groups in `answer_elements`
# (label -> GlobalIds or entities) and the backend reads them after the turn.
MAX_GROUP_SIZE = 5000
_ELEMENTS_MARKER = "__COBBIE_ANSWER_ELEMENTS__"
_CLEAR_ELEMENTS = "answer_elements = {}\n"
_DUMP_HELPER = f'''\
def _cobbie_dump_answer_elements():
    import json
    ae = globals().get("answer_elements")
    out = {{}}
    if isinstance(ae, dict):
        for label, items in ae.items():
            if isinstance(items, (str, ifcopenshell.entity_instance)):
                items = [items]
            try:
                items = list(items)
            except TypeError:
                continue
            ids = []
            for x in items:
                if isinstance(x, int):
                    try:
                        x = model.by_id(x)
                    except Exception:
                        continue
                gid = getattr(x, "GlobalId", x)
                if isinstance(gid, str):
                    ids.append(gid)
                if len(ids) >= {MAX_GROUP_SIZE}:
                    break
            out[str(label)] = ids
    print({_ELEMENTS_MARKER!r} + json.dumps(out))
'''
_DUMP_ELEMENTS = "_cobbie_dump_answer_elements()\n"


def _preamble(path: Path) -> str:
    return (
        "import ifcopenshell, ifcopenshell.util.element, ifcopenshell.util.unit\n"
        f"ifc_path = {str(path)!r}\n"
        "model = ifcopenshell.open(ifc_path)\n"
        + _CLEAR_ELEMENTS
        + _DUMP_HELPER
    )


def _clear_answer_elements(interp: JupyterInterpreter) -> None:
    interp.execute(_CLEAR_ELEMENTS, timeout=10)


def _read_answer_elements(interp: JupyterInterpreter) -> dict[str, list[str]]:
    """Read `answer_elements` from the kernel as {label: [GlobalId, ...]}; {} on any failure."""
    res = interp.execute(_DUMP_ELEMENTS, timeout=60, truncate=False)
    idx = res.text.rfind(_ELEMENTS_MARKER)
    if res.timed_out or idx < 0:
        log.warning("could not read answer_elements: %s", res.text[:500])
        return {}
    line = res.text[idx + len(_ELEMENTS_MARKER) :].splitlines()[0]
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        log.warning("answer_elements is not valid JSON")
        return {}
    return {str(k): [str(g) for g in v] for k, v in data.items() if isinstance(v, list)}


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


@app.get("/api/samples")
def list_samples() -> list[dict]:
    """Sample IFC models shipped with the repo, for the UI's 'Load sample' buttons."""
    return [
        {"name": p.name, "size": p.stat().st_size}
        for p in sorted(SAMPLES_DIR.glob("*.ifc"))
    ]


@app.get("/api/samples/{name}")
def get_sample(name: str) -> FileResponse:
    path = SAMPLES_DIR / name
    if "/" in name or not name.endswith(".ifc") or not path.is_file():
        raise HTTPException(404, f"Unknown sample {name!r}.")
    return FileResponse(path, media_type="application/octet-stream", filename=name)


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
    interp: JupyterInterpreter | None = None
    try:
        # Variant and LLM (API key) first, so a config error starts no kernel.
        variant = get_variant()
        llm = init_llm()
        interp = JupyterInterpreter(preamble=_preamble(path))
        agent = create_ifc_agent(interp, variant=variant, llm=llm)
    except Exception as exc:
        log.exception("session setup failed")
        if interp is not None:
            interp.shutdown()
        path.unlink(missing_ok=True)
        raise HTTPException(500, detail=str(exc)) from exc
    return Session(model_id, filename, path, ifc, interp, agent, variant)


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


_COUNT_SUFFIX = re.compile(r"\s*\(\s*\d+\s*\)\s*$")


def _norm_label(label: str) -> str:
    return _COUNT_SUFFIX.sub("", label).strip().casefold()


def _with_count(label: str, n: int) -> str:
    """Label ending in the actual element count, replacing any count the model wrote."""
    base = _COUNT_SUFFIX.sub("", label).strip() or label.strip()
    return f"{base} ({n})"


def _group_ids(label: str, global_ids: list[str], kernel: dict[str, list[str]]) -> list[str]:
    """Ids for one answer group: the kernel group with that label, else the hand-listed ids."""
    if label in kernel:
        return kernel[label]
    norm = _norm_label(label)
    for k, v in kernel.items():
        if _norm_label(k) == norm:
            return v
    return global_ids


def _enrich(ifc: ifcopenshell.file, answer: Answer, kernel: dict[str, list[str]]) -> list[dict]:
    """Resolve each answer group's GlobalIds against the in-process model.

    Ids come from the kernel's `answer_elements` (matched by label) or, as a
    fallback, from the hand-listed `global_ids`. If the answer lists no group
    but the kernel holds some, all kernel groups are shown. Unknown ids and
    empty groups are dropped, each group is capped at MAX_GROUP_SIZE, and the
    label gets the resolved count.
    """
    if answer.element_groups:
        wanted = [(g.label, _group_ids(g.label, g.global_ids, kernel)) for g in answer.element_groups]
    else:
        wanted = list(kernel.items())
    groups = []
    for label, ids in wanted:
        seen: set[str] = set()
        elements = []
        for gid in ids:
            if len(elements) >= MAX_GROUP_SIZE:
                break
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
            groups.append({"label": _with_count(label, len(elements)), "elements": elements})
    return groups


def _dangling_labels(answer: Answer, groups: list[dict]) -> list[str]:
    """Labels the Answer lists that resolved to no element (never stored, or a typo)."""
    resolved = {_norm_label(g["label"]) for g in groups}
    return [g.label for g in answer.element_groups if _norm_label(g.label) not in resolved]


async def _answer_turn(s: Session, message: str):
    """Run one question and yield its events, the answer with resolved groups.

    With a nudging variant, an answer listing a group label that resolves to
    no element (the model meant to show elements but never stored them) gets
    exactly one corrective turn (NUDGE_MSG); its answer replaces the first one.
    """
    wrap_up = s.variant.wrap_up
    prompt, nudges_left = message, int(s.variant.nudge)
    while True:
        answer: Answer | None = None
        async for ev in stream_turn(s.agent, s.thread_id, prompt, wrap_up=wrap_up):
            if ev["type"] == "answer":
                answer = ev["answer"]
            else:
                yield ev
        if answer is None:  # stream_turn raises first; guard for type checkers
            raise RuntimeError("The agent ended without producing an answer.")
        kernel = await run_in_threadpool(_read_answer_elements, s.interp)
        groups = await run_in_threadpool(_enrich, s.ifc, answer, kernel)
        dangling = _dangling_labels(answer, groups)
        if not dangling or nudges_left <= 0:
            break
        nudges_left -= 1
        prompt = NUDGE_MSG.format(
            labels=", ".join(repr(x) for x in dangling),
            stored=", ".join(repr(k) for k in kernel) or "nothing",
        )
        yield {"type": "status", "text": "checking element chips"}
    yield {
        "type": "answer",
        "text": answer.text,
        "element_groups": groups,
        "sufficient_info": answer.sufficient_info,
    }


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
                # Stale groups from the previous question must not leak into this one.
                await run_in_threadpool(_clear_answer_elements, s.interp)
                async for ev in _answer_turn(s, message):
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
