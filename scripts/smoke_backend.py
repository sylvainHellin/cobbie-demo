"""In-process smoke test of the backend (no server).

Uploads samples/fzk_house_arc.ifc, asks a question and a follow-up, prints the
streamed events compactly, and asserts the answer contract.

Run: uv run python scripts/smoke_backend.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from backend.app import app  # noqa: E402

SAMPLE = ROOT / "samples" / "fzk_house_arc.ifc"
QUESTIONS = [
    "How many doors are there and on which storeys?",
    "Which of them is the widest?",
]


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


def check(events: list[dict]) -> dict:
    types = [e["type"] for e in events]
    assert "error" not in types, [e for e in events if e["type"] == "error"]
    assert "code" in types, "no code event"
    assert types[-1] == "done", "stream did not end with done"
    answers = [e for e in events if e["type"] == "answer"]
    assert len(answers) == 1, "expected exactly one answer event"
    ans = answers[0]
    assert ans["text"].strip(), "empty answer text"
    assert ans["element_groups"], "no element groups"
    for g in ans["element_groups"]:
        assert g["label"] and g["elements"]
        for e in g["elements"]:
            assert len(e["global_id"]) == 22, e
    classes = {e["ifc_class"] for g in ans["element_groups"] for e in g["elements"]}
    assert "IfcDoor" in classes, f"no IfcDoor among enriched elements: {classes}"
    return ans


def main() -> None:
    with TestClient(app) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        t0 = time.perf_counter()
        with SAMPLE.open("rb") as f:
            r = client.post("/api/models", files={"file": (SAMPLE.name, f, "application/octet-stream")})
        assert r.status_code == 200, r.text
        info = r.json()
        print(f"upload: {info} ({time.perf_counter() - t0:.1f} s)")
        bad = client.post("/api/models", files={"file": ("x.txt", b"hello", "text/plain")})
        assert bad.status_code == 400, bad.text

        latencies = []
        for q in QUESTIONS:
            events, dt = ask(client, info["model_id"], q)
            check(events)
            latencies.append(dt)

        r = client.post(f"/api/models/{info['model_id']}/reset")
        assert r.status_code == 200, r.text

    print("\nPASS")
    for q, dt in zip(QUESTIONS, latencies):
        print(f"  {dt:6.1f} s  {q}")


if __name__ == "__main__":
    main()
