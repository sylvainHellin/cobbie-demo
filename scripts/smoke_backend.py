"""In-process smoke test of the backend (no server).

Uploads samples/fzk_house_arc.ifc, asks a question and a follow-up, then
uploads samples/duplex_arc.ifc and asks for all walls (a large group meant to
come through the `answer_elements` kernel handoff). Prints the streamed events
compactly.

Hard asserts cover the API contract (event types and order, one answer, done
last, non-empty text, every element id resolving in the model to an expected
class) and, since the agent may split elements into groups as it likes, the
union of all groups: all 5 doors of fzk_house, all walls of duplex. A follow-up
answer without groups, or walls not passed via `answer_elements`, only warns.

Run: uv run python scripts/smoke_backend.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ifcopenshell  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.app import app  # noqa: E402

SAMPLE = ROOT / "samples" / "fzk_house_arc.ifc"
QUESTIONS = [
    "How many doors are there and on which storeys?",
    "Which of them is the widest?",
]
WALLS_SAMPLE = ROOT / "samples" / "duplex_arc.ifc"
WALLS_QUESTION = "Show me all the walls"
WALL_CLASSES = {"IfcWall", "IfcWallStandardCase"}
DOOR_CLASSES = {"IfcDoor", "IfcDoorStandardCase"}
# A door answer may also show the storeys it names.
DOOR_ALLOWED = DOOR_CLASSES | {"IfcBuildingStorey"}
EVENT_TYPES = {"status", "code", "output", "answer", "error", "done"}
WARNINGS: list[str] = []


def _short(text: str, n: int = 160) -> str:
    text = text.replace("\n", " | ")
    return text if len(text) <= n else text[: n - 3] + "..."


def ask(client: TestClient, model_id: str, question: str) -> tuple[list[dict], float]:
    print(f"\n>>> {question}")
    t0 = time.perf_counter()
    events: list[dict] = []
    with client.stream("POST", "/api/chat", json={"model_id": model_id, "message": question}) as r:
        assert r.status_code == 200, r.read()
        assert r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            if not line.startswith("data: "):
                continue
            ev = json.loads(line[6:])
            events.append(ev)
            t = ev["type"]
            if t == "code":
                print(f"  [code]   {_short(ev['code'])}")
            elif t == "output":
                print(f"  [output] {_short(ev['text'])}{' (truncated)' if ev['truncated'] else ''}")
            elif t == "answer":
                print(f"  [answer] sufficient_info={ev['sufficient_info']}")
                print("    " + ev["text"].replace("\n", "\n    "))
                for g in ev["element_groups"]:
                    ids = ", ".join(e["global_id"] for e in g["elements"][:3])
                    print(f"    chip: {g['label']} -> {len(g['elements'])} elements [{ids}...]")
            elif t == "error":
                print(f"  [error]  {ev['message']}")
            elif t == "done":
                print(f"  [done]   {ev.get('elapsed_s')} s")
    return events, time.perf_counter() - t0


def upload(client: TestClient, path: Path) -> dict:
    t0 = time.perf_counter()
    with path.open("rb") as f:
        r = client.post("/api/models", files={"file": (path.name, f, "application/octet-stream")})
    assert r.status_code == 200, r.text
    info = r.json()
    print(f"upload: {info} ({time.perf_counter() - t0:.1f} s)")
    return info


def warn(msg: str) -> None:
    WARNINGS.append(msg)
    print(f"  WARNING: {msg}")


def check_contract(events: list[dict], model: ifcopenshell.file, allowed: set[str], needs_code: bool = True) -> dict:
    """Hard checks on the SSE contract; returns the answer event.

    Event types are known, there is no error, exactly one answer comes right
    before done, which is the single last event, and (for a first question on
    a model, which the agent cannot answer from memory) a code step comes
    before the answer. Every group
    has a label ending in its element count, and every element resolves in the
    model to the class the server reported, which is one of `allowed`.
    """
    types = [e["type"] for e in events]
    assert set(types) <= EVENT_TYPES, f"unknown event types: {set(types) - EVENT_TYPES}"
    assert "error" not in types, [e for e in events if e["type"] == "error"]
    assert types and types[-1] == "done" and types.count("done") == 1, f"done not single and last: {types}"
    assert types.count("answer") == 1, f"expected exactly one answer event: {types}"
    i_answer = types.index("answer")
    assert i_answer == len(types) - 2, f"events after the answer other than done: {types[i_answer:]}"
    if needs_code:
        assert "code" in types[:i_answer], "no code step before the answer"
    ans = events[i_answer]
    assert isinstance(ans["text"], str) and ans["text"].strip(), "empty answer text"
    assert isinstance(ans["sufficient_info"], bool), ans["sufficient_info"]
    for g in ans["element_groups"]:
        assert g["label"] and g["elements"], f"empty group: {g}"
        assert g["label"].endswith(f"({len(g['elements'])})"), f"label without count: {g['label']!r}"
        for e in g["elements"]:
            assert len(e["global_id"]) == 22, e
            entity = model.by_guid(e["global_id"])  # raises on unknown ids
            assert entity.is_a() == e["ifc_class"], (e, entity.is_a())
            assert e["ifc_class"] in allowed, f"unexpected class {e['ifc_class']} in {g['label']!r}"
    return ans


def union_ids(ans: dict, classes: set[str]) -> set[str]:
    return {e["global_id"] for g in ans["element_groups"] for e in g["elements"] if e["ifc_class"] in classes}


def check_doors(events: list[dict], model: ifcopenshell.file) -> None:
    """First question: the groups together cover exactly the model's doors."""
    ans = check_contract(events, model, DOOR_ALLOWED)
    doors = {d.GlobalId for d in model.by_type("IfcDoor")}
    got = union_ids(ans, DOOR_CLASSES)
    assert got == doors, f"door groups cover {len(got)} ids, model has {len(doors)} doors"
    print(f"  doors: union of {len(ans['element_groups'])} group(s) == by_type('IfcDoor') = {len(doors)}")


def check_followup(events: list[dict], model: ifcopenshell.file) -> None:
    """Follow-up: code and groups are optional; any door it shows is one of the model's doors."""
    ans = check_contract(events, model, DOOR_ALLOWED, needs_code=False)
    if not ans["element_groups"]:
        warn("follow-up answer has no element groups")
        return
    doors = {d.GlobalId for d in model.by_type("IfcDoor")}
    got = union_ids(ans, DOOR_CLASSES)
    assert got <= doors, f"follow-up shows doors not in the model: {got - doors}"
    print(f"  follow-up: {len(got)} door(s) shown")


def check_walls(events: list[dict], model: ifcopenshell.file) -> None:
    """Walls: the groups together cover every IfcWall, handed over via `answer_elements`."""
    ans = check_contract(events, model, WALL_CLASSES)
    codes = [e["code"] for e in events if e["type"] == "code"]
    if not any("answer_elements" in c for c in codes):
        warn("agent did not use answer_elements for the walls")
    walls = {w.GlobalId for w in model.by_type("IfcWall")}
    got = union_ids(ans, WALL_CLASSES)
    sizes = [(g["label"], len(g["elements"])) for g in ans["element_groups"]]
    assert got == walls, f"wall groups cover {len(got)} of {len(walls)} walls: {sizes}"
    print(f"  walls: union of {len(sizes)} group(s) == by_type('IfcWall') = {len(walls)}")


def main() -> None:
    with TestClient(app) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        info = upload(client, SAMPLE)
        bad = client.post("/api/models", files={"file": ("x.txt", b"hello", "text/plain")})
        assert bad.status_code == 400, bad.text

        house = ifcopenshell.open(str(SAMPLE))
        latencies = []
        for q, check in zip(QUESTIONS, [check_doors, check_followup]):
            events, dt = ask(client, info["model_id"], q)
            check(events, house)
            latencies.append(dt)

        r = client.post(f"/api/models/{info['model_id']}/reset")
        assert r.status_code == 200, r.text

        walls_info = upload(client, WALLS_SAMPLE)
        events, dt = ask(client, walls_info["model_id"], WALLS_QUESTION)
        check_walls(events, ifcopenshell.open(str(WALLS_SAMPLE)))
        latencies.append(dt)

    print(f"\nPASS ({len(WARNINGS)} warning(s))")
    for w in WARNINGS:
        print(f"  WARNING: {w}")
    for q, dt in zip([*QUESTIONS, f"{WALLS_QUESTION} ({WALLS_SAMPLE.name})"], latencies):
        print(f"  {dt:6.1f} s  {q}")


if __name__ == "__main__":
    main()
