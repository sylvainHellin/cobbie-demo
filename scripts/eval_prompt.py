"""Evaluate system-prompt variants on element-chip reliability and correctness.

Runs the in-process backend (FastAPI TestClient, live LLM) over a fixed set of
questions on both samples, each single question on a fresh upload and one
three-turn conversation per sample, and scores every turn against ground truth
computed here with ifcopenshell:

- expect E: the answer is about elements, so it must show chips that cover the
  target elements (chips_ok); expect N: no chips is correct (schema, project
  name, elements that do not exist); expect O: either is fine (storeys, which
  are containers the viewer cannot highlight).
- correct: a text check against the ground truth (counts exact, areas 2%).
- latency, code rounds, LLM calls, tokens, whether the chip nudge fired, and
  labels in the Answer that resolved to no elements (dangling).

Per-turn records go to .agents/research/eval-runs/<stamp>-<variant>.jsonl and a
summary table is printed; --summarize re-prints the table from saved runs.

Usage:
  uv run python scripts/eval_prompt.py --variant strict --reps 2 --workers 4
  uv run python scripts/eval_prompt.py --variant demo,strict --only fzk_doors
  uv run python scripts/eval_prompt.py --summarize .agents/research/eval-runs/*.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import threading
import time
import traceback
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ifcopenshell  # noqa: E402
import ifcopenshell.util.element as ue  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import backend.app as appmod  # noqa: E402
from backend.prompts import DEFAULT_VARIANT, VARIANTS  # noqa: E402

RUNS_DIR = ROOT / ".agents" / "research" / "eval-runs"
NUDGE_STATUS = "checking element chips"


# --- ground truth -------------------------------------------------------------

_NUM = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")


def numbers(text: str) -> list[float]:
    return [float(x) for x in _NUM.findall(text.replace(",", ""))]


def has_num(text: str, x: float, rel: float = 0.0) -> bool:
    return any(abs(n - x) <= max(rel * abs(x), 1e-9) for n in numbers(text))


def contains(text: str, *needles: str) -> bool:
    low = text.casefold()
    return any(n.casefold() in low for n in needles)


def storey(el) -> str | None:
    """Containing storey; spaces hang off their storey by aggregation instead."""
    c = ue.get_container(el) or ue.get_aggregate(el)
    return c.Name if c else None


def qto(el, key: str) -> float | None:
    for props in ue.get_psets(el, qtos_only=True).values():
        v = props.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    return None


@dataclass
class Q:
    qid: str
    text: str
    expect: str  # E, N or O
    target: Callable[[ifcopenshell.file], set[str]] | None = None
    check: Callable[[str, ifcopenshell.file], bool] = lambda t, m: True


def ids(els) -> set[str]:
    return {e.GlobalId for e in els}


FZK = "fzk_house_arc.ifc"
DUPLEX = "duplex_arc.ifc"


def _fzk_ground_spaces(m):
    return [s for s in m.by_type("IfcSpace") if storey(s) == "Erdgeschoss"]


def _fzk_windows(m, st):
    return [w for w in m.by_type("IfcWindow") if storey(w) == st]


def _largest_space(m):
    return max(m.by_type("IfcSpace"), key=lambda s: qto(s, "GrossFloorArea") or 0)


def _area_ok(t, m):
    sp = m.by_type("IfcSpace")
    gross = sum(qto(s, "GrossFloorArea") or 0 for s in sp)
    net = sum(qto(s, "NetFloorArea") or 0 for s in sp)
    return has_num(t, gross, 0.02) or has_num(t, net, 0.02)


def _widest_door(m):
    return max(m.by_type("IfcDoor"), key=lambda d: d.OverallWidth or 0)


def _external_walls(m):
    return [
        w
        for w in m.by_type("IfcWall")
        if ue.get_psets(w).get("Pset_WallCommon", {}).get("IsExternal") is True
    ]


def _slabs_ok(t, m):
    slabs = m.by_type("IfcSlab")
    return has_num(t, len(slabs)) or (has_num(t, 10) and contains(t, "Level 1") and contains(t, "Level 2"))


# Each entry is one conversation on a fresh upload: single questions are
# one-element lists, the last entry per sample is a three-turn conversation.
SUITE: dict[str, list[list[Q]]] = {
    FZK: [
        [Q("fzk_rooms_eg", "Which rooms are on the ground floor?", "E",
           lambda m: ids(_fzk_ground_spaces(m)),
           lambda t, m: all(contains(t, s.LongName) for s in _fzk_ground_spaces(m))
           or has_num(t, len(_fzk_ground_spaces(m))))],
        [Q("fzk_largest_room", "What is the largest room?", "E",
           lambda m: {_largest_space(m).GlobalId},
           lambda t, m: contains(t, _largest_space(m).LongName))],
        [Q("fzk_total_area", "What is the total floor area of all spaces?", "E",
           lambda m: ids(m.by_type("IfcSpace")), _area_ok)],
        [Q("fzk_schema", "Which IFC schema version does this model use?", "N",
           None, lambda t, m: contains(t, m.schema))],
        [Q("fzk_windows_eg", "Show me the windows on the ground floor.", "E",
           lambda m: ids(_fzk_windows(m, "Erdgeschoss")),
           lambda t, m: has_num(t, len(_fzk_windows(m, "Erdgeschoss"))))],
        [
            Q("fzk_doors", "How many doors are there and on which storeys?", "E",
              lambda m: ids(m.by_type("IfcDoor")),
              lambda t, m: has_num(t, len(m.by_type("IfcDoor"))) and contains(t, "Erdgeschoss", "ground")),
            Q("fzk_f_widest_door", "Which of them is the widest?", "E",
              lambda m: {_widest_door(m).GlobalId},
              lambda t, m: contains(t, _widest_door(m).Name) or has_num(t, _widest_door(m).OverallWidth, 0.01)),
            Q("fzk_f_windows_og", "And which windows are on the upper floor?", "E",
              lambda m: ids(_fzk_windows(m, "Dachgeschoss")),
              lambda t, m: has_num(t, len(_fzk_windows(m, "Dachgeschoss")))
              or all(contains(t, w.Name) for w in _fzk_windows(m, "Dachgeschoss"))),
        ],
    ],
    DUPLEX: [
        [Q("dup_walls", "Show me all the walls", "E",
           lambda m: ids(m.by_type("IfcWall")),
           lambda t, m: has_num(t, len(m.by_type("IfcWall"))))],
        [Q("dup_ext_walls", "Which walls are external?", "E",
           lambda m: ids(_external_walls(m)),
           lambda t, m: has_num(t, len(_external_walls(m))))],
        [Q("dup_slabs", "How many slabs are there on each storey?", "E",
           lambda m: ids(m.by_type("IfcSlab")), _slabs_ok)],
        [Q("dup_project", "What is the name of the project?", "N",
           None, lambda t, m: contains(t, "Duplex Apartment", "0001"))],
        [Q("dup_columns", "Are there any columns in the model?", "N",
           None, lambda t, m: bool(re.search(r"\bno\b|not available|none|zero|\b0\b|does not|doesn't", t.casefold())))],
        [Q("dup_storeys", "How many storeys does the building have?", "O",
           None, lambda t, m: has_num(t, len(m.by_type("IfcBuildingStorey"))))],
    ],
}


# --- running ------------------------------------------------------------------

# _enrich is patched to record, per model, the labels the Answer listed and the
# ones that resolved to no element (keyed by id of the session's ifc file).
_ENRICH_LOG: dict[int, list[dict]] = defaultdict(list)
_orig_enrich = appmod._enrich


def _recording_enrich(ifc, answer, kernel):
    groups = _orig_enrich(ifc, answer, kernel)
    resolved = {appmod._norm_label(g["label"]) for g in groups}
    listed = [g.label for g in answer.element_groups]
    _ENRICH_LOG[id(ifc)].append(
        {
            "listed": listed,
            "kernel": sorted(kernel),
            "dangling": [lbl for lbl in listed if appmod._norm_label(lbl) not in resolved],
        }
    )
    return groups


appmod._enrich = _recording_enrich


def _usage(session, n0: int) -> tuple[int, int, int, int]:
    """LLM calls and input/output tokens of the messages added since index n0."""
    state = session.agent.get_state({"configurable": {"thread_id": session.thread_id}})
    msgs = state.values.get("messages", [])
    calls = tin = tout = 0
    for m in msgs[n0:]:
        if getattr(m, "type", None) == "ai":
            calls += 1
            um = getattr(m, "usage_metadata", None) or {}
            tin += um.get("input_tokens", 0)
            tout += um.get("output_tokens", 0)
    return calls, tin, tout, len(msgs)


def run_conversation(client: TestClient, variant: str, sample: str, conv: list[Q], rep: int) -> list[dict]:
    path = ROOT / "samples" / sample
    truth = ifcopenshell.open(str(path))
    with path.open("rb") as f:
        r = client.post("/api/models", files={"file": (sample, f, "application/octet-stream")})
    r.raise_for_status()
    model_id = r.json()["model_id"]
    session = appmod.SESSIONS[model_id]
    assert session.variant.name == variant, (session.variant.name, variant)
    records, n_msgs = [], 0
    try:
        for turn, q in enumerate(conv):
            events: list[dict] = []
            t0 = time.perf_counter()
            with client.stream("POST", "/api/chat", json={"model_id": model_id, "message": q.text}) as resp:
                for line in resp.iter_lines():
                    if line.startswith("data: "):
                        events.append(json.loads(line[6:]))
            wall = time.perf_counter() - t0
            calls, tin, tout, n_msgs = _usage(session, n_msgs)
            ans = next((e for e in events if e["type"] == "answer"), None)
            err = next((e["message"] for e in events if e["type"] == "error"), None)
            groups = ans["element_groups"] if ans else []
            union = {e["global_id"] for g in groups for e in g["elements"]}
            valid = all(truth.by_guid(g) is not None for g in union) if union else True
            has_groups = bool(groups)
            if q.expect == "E":
                target = q.target(truth) if q.target else set()
                chips_ok = has_groups and valid and target <= union
            elif q.expect == "N":
                chips_ok = not has_groups
            else:
                chips_ok = True
            text = ans["text"] if ans else ""
            enrich = _ENRICH_LOG[id(session.ifc)]
            records.append(
                {
                    "variant": variant,
                    "sample": sample,
                    "qid": q.qid,
                    "turn": turn,
                    "rep": rep,
                    "expect": q.expect,
                    "has_groups": has_groups,
                    "chips_ok": chips_ok,
                    "ids_valid": valid,
                    "correct": bool(ans) and q.check(text, truth),
                    "latency_s": round(wall, 1),
                    "code_rounds": sum(e["type"] == "code" for e in events),
                    "nudged": any(e["type"] == "status" and e.get("text") == NUDGE_STATUS for e in events),
                    "dangling": enrich[-1]["dangling"] if enrich else [],
                    "labels": [g["label"] for g in groups],
                    "llm_calls": calls,
                    "tokens_in": tin,
                    "tokens_out": tout,
                    "error": err,
                    "text": text,
                }
            )
            print(
                f"  [{variant}] {q.qid} r{rep}: groups={has_groups} chips_ok={chips_ok} "
                f"correct={records[-1]['correct']} {wall:.0f}s code={records[-1]['code_rounds']}"
                f"{' NUDGED' if records[-1]['nudged'] else ''}{' ERR ' + err if err else ''}",
                flush=True,
            )
    finally:
        s = appmod.SESSIONS.pop(model_id, None)
        if s is not None:
            s.interp.shutdown()
            s.path.unlink(missing_ok=True)
    return records


def run_variant(variant: str, reps: int, workers: int, only: set[str] | None) -> Path:
    os.environ["COBBIE_PROMPT"] = variant  # read by the backend at upload time
    jobs = [
        (sample, conv, rep)
        for rep in range(reps)
        for sample, convs in SUITE.items()
        for conv in convs
        if not only or any(q.qid in only for q in conv)
    ]
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    out = RUNS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{variant}.jsonl"
    lock = threading.Lock()
    print(f"== {variant}: {len(jobs)} conversations, {workers} workers -> {out}", flush=True)
    # One client, so one event loop: the agent's async HTTP client and the
    # session locks are bound to the loop they were first used on.
    with TestClient(appmod.app) as client, ThreadPoolExecutor(max_workers=workers) as pool, out.open("w") as fh:
        futs = {pool.submit(run_conversation, client, variant, *job): job for job in jobs}
        for fut in as_completed(futs):
            try:
                recs = fut.result()
            except Exception:
                sample, conv, rep = futs[fut]
                traceback.print_exc()
                recs = [{"variant": variant, "sample": sample, "qid": conv[0].qid, "rep": rep,
                         "error": "harness failure", "expect": conv[0].expect}]
            with lock:
                for rec in recs:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()
    return out


# --- summary ------------------------------------------------------------------


def _rate(recs: list[dict], key: str) -> str:
    if not recs:
        return "-"
    return f"{sum(bool(r.get(key)) for r in recs)}/{len(recs)}"


def summarize(paths: list[Path]) -> str:
    by_variant: dict[str, list[dict]] = defaultdict(list)
    for p in paths:
        for line in p.read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                by_variant[rec["variant"]].append(rec)
    head = (
        "| variant | turns | E chips | E chips cover target | follow-up chips | N no chips "
        "| correct | dangling labels | nudged | latency med/mean s | code rounds | LLM calls | tokens in/out (mean) | errors |"
    )
    rows = [head, "|" + "---|" * (head.count("|") - 1)]
    for v, recs in by_variant.items():
        ok = [r for r in recs if "latency_s" in r]
        e = [r for r in ok if r["expect"] == "E"]
        fu = [r for r in e if r["turn"] > 0]
        n = [r for r in ok if r["expect"] == "N"]
        lat = [r["latency_s"] for r in ok]
        rows.append(
            "| "
            + " | ".join(
                [
                    v,
                    str(len(recs)),
                    _rate(e, "has_groups"),
                    _rate(e, "chips_ok"),
                    _rate(fu, "has_groups"),
                    _rate(n, "chips_ok"),
                    _rate(ok, "correct"),
                    str(sum(bool(r["dangling"]) for r in ok)),
                    str(sum(r["nudged"] for r in ok)),
                    f"{statistics.median(lat):.0f}/{statistics.mean(lat):.0f}" if lat else "-",
                    f"{statistics.mean(r['code_rounds'] for r in ok):.1f}" if ok else "-",
                    f"{statistics.mean(r['llm_calls'] for r in ok):.1f}" if ok else "-",
                    f"{statistics.mean(r['tokens_in'] for r in ok):.0f}/{statistics.mean(r['tokens_out'] for r in ok):.0f}"
                    if ok
                    else "-",
                    str(sum(bool(r.get("error")) for r in recs)),
                ]
            )
            + " |"
        )
    per_q = ["", "| variant | question | expect | chips ok | correct |", "|---|---|---|---|---|"]
    for v, recs in by_variant.items():
        qs: dict[str, list[dict]] = defaultdict(list)
        for r in recs:
            if "latency_s" in r:
                qs[r["qid"]].append(r)
        for qid, rs in qs.items():
            per_q.append(f"| {v} | {qid} | {rs[0]['expect']} | {_rate(rs, 'chips_ok')} | {_rate(rs, 'correct')} |")
    return "\n".join(rows + per_q)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--variant", default=DEFAULT_VARIANT, help=f"comma-separated, from {sorted(VARIANTS)}")
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--only", default="", help="comma-separated question ids (a conversation runs whole)")
    ap.add_argument("--summarize", nargs="*", type=Path, help="print the table for saved runs and exit")
    args = ap.parse_args()
    if args.summarize is not None:
        print(summarize(args.summarize))
        return
    only = {x for x in args.only.split(",") if x} or None
    outs = [run_variant(v, args.reps, args.workers, only) for v in args.variant.split(",")]
    print(summarize(outs))


if __name__ == "__main__":
    main()
