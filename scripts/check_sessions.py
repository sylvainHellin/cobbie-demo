"""In-process checks of session lifecycle (no LLM calls, no server).

1. A second upload closes the first session: SESSIONS keeps one entry, the old
   kernel process is gone, its upload file is deleted, and a tool call still
   running on the old kernel ends with a notice instead of raising.
2. Setup failures leak nothing: a missing API key (before the kernel starts)
   and a failing kernel preamble (after it started) both return 500 with the
   message, leave no live kernel and no upload file.
3. `_enrich` falls back to all kernel groups when the listed groups resolve
   to nothing.

Run: uv run python scripts/check_sessions.py
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ifcopenshell  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import backend.app as appmod  # noqa: E402
import backend.llm as llmmod  # noqa: E402
from backend.agent import Answer, ElementGroup  # noqa: E402

SAMPLES = [ROOT / "samples" / "fzk_house_arc.ifc", ROOT / "samples" / "duplex_arc.ifc"]


def upload(client: TestClient, path: Path):
    with path.open("rb") as f:
        return client.post("/api/models", files={"file": (path.name, f, "application/octet-stream")})


def pid_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    stat = Path(f"/proc/{pid}/stat")
    if not stat.exists():
        return False
    return stat.read_text().split(")")[-1].split()[0] != "Z"  # zombie counts as gone


def uploads() -> set[Path]:
    return set(appmod.UPLOAD_DIR.glob("*.ifc"))


def check_single_session(client: TestClient) -> None:
    r = upload(client, SAMPLES[0])
    assert r.status_code == 200, r.text
    old = appmod.SESSIONS[r.json()["model_id"]]
    old_pid = getattr(old.interp.km.provisioner, "pid", None)
    assert old.interp.km.is_alive() and pid_alive(old_pid)

    # A tool call still running on the old kernel when the new model arrives.
    result: dict = {}

    def long_tool() -> None:
        try:
            result["text"] = old.interp.run("import time; time.sleep(30); print('finished')", timeout=60)
        except BaseException as exc:  # must not happen
            result["exc"] = exc

    t = threading.Thread(target=long_tool)
    t.start()
    time.sleep(1.0)

    r2 = upload(client, SAMPLES[1])
    assert r2.status_code == 200, r2.text
    new_id = r2.json()["model_id"]
    assert list(appmod.SESSIONS) == [new_id], list(appmod.SESSIONS)
    assert not old.interp.km.is_alive(), "old kernel still alive"
    assert not pid_alive(old_pid), f"old kernel process {old_pid} still running"
    assert not old.path.exists(), f"old upload {old.path} not deleted"

    t.join(timeout=10)
    assert not t.is_alive(), "tool thread on the old kernel did not end"
    assert "exc" not in result, f"tool thread raised: {result.get('exc')!r}"
    assert "shut down" in result["text"], result["text"]
    assert client.post(f"/api/models/{old.model_id}/reset").status_code == 404
    print(f"  single session: old kernel pid {old_pid} gone, file deleted, tool thread ended: {result['text']!r}")


def check_setup_failures(client: TestClient, monkey: list) -> None:
    created: list = []
    orig_interp = appmod.JupyterInterpreter

    class Recording(orig_interp):  # type: ignore[misc, valid-type]
        def __init__(self, *a, **kw):
            created.append(self)
            super().__init__(*a, **kw)

    appmod.JupyterInterpreter = Recording
    monkey.append(lambda: setattr(appmod, "JupyterInterpreter", orig_interp))

    # Missing key: fails before any kernel starts.
    orig_key = llmmod._api_key

    def no_key() -> str:
        raise RuntimeError("No Z.AI key: set ZAI_API_KEY in .env or provide zai-api in ~/.pi/agent/auth.json")

    llmmod._api_key = no_key
    before = uploads()
    try:
        r = upload(client, SAMPLES[0])
    finally:
        llmmod._api_key = orig_key
    assert r.status_code == 500, r.text
    assert "No Z.AI key" in r.json()["detail"], r.text
    assert not created, "a kernel was started despite the missing key"
    assert uploads() == before, uploads() - before
    print(f"  missing key: 500 {r.json()['detail']!r}, no kernel, no file")

    # Preamble failure: the kernel started, so it must be shut down.
    orig_preamble = appmod._preamble
    appmod._preamble = lambda path: "raise ValueError('boom')\n"
    try:
        r = upload(client, SAMPLES[0])
    finally:
        appmod._preamble = orig_preamble
    assert r.status_code == 500, r.text
    assert "Kernel preamble failed" in r.json()["detail"], r.text
    assert len(created) == 1, created
    pid = getattr(created[0].km.provisioner, "pid", None) if created[0].km.provisioner else None
    assert not created[0].km.is_alive(), "kernel left running after preamble failure"
    assert not pid_alive(pid)
    assert uploads() == before, uploads() - before
    print("  preamble failure: 500 'Kernel preamble failed...', kernel shut down, no file")


def check_enrich_fallback() -> None:
    ifc = ifcopenshell.open(str(SAMPLES[0]))
    doors = [d.GlobalId for d in ifc.by_type("IfcDoor")]
    kernel = {"doors": doors}
    listed = Answer(text="x", element_groups=[ElementGroup(label="Doors on EG")], sufficient_info=True)
    groups = appmod._enrich(ifc, listed, kernel)
    assert [g["label"] for g in groups] == [f"doors ({len(doors)})"], groups
    assert appmod._dangling_labels(listed, groups) == ["Doors on EG"]  # the nudge still triggers first
    none_listed = Answer(text="x", sufficient_info=True)
    assert appmod._enrich(ifc, none_listed, kernel) == groups
    ok = Answer(text="x", element_groups=[ElementGroup(label="doors")], sufficient_info=True)
    assert appmod._enrich(ifc, ok, {**kernel, "other": [ifc.by_type("IfcWall")[0].GlobalId]}) == groups
    assert appmod._enrich(ifc, listed, {}) == []
    print("  enrich: all-dangling groups fall back to kernel groups, listed label still dangling")


def main() -> None:
    undo: list = []
    try:
        with TestClient(appmod.app) as client:
            check_single_session(client)
            check_setup_failures(client, undo)
        check_enrich_fallback()
    finally:
        for f in undo:
            f()
    print("PASS")


if __name__ == "__main__":
    main()
