# cobbie-demo

Local demo web app for asking questions about an IFC building model.
The user uploads an IFC file, sees it in a 3D viewer, and chats with an LLM agent.
The agent answers by writing and running Python (ifcopenshell) in a persistent Jupyter kernel.
Each answer carries groups of element GlobalIds, which the UI shows as clickable chips that highlight those elements in the viewer.

The agent is a trimmed port of the cobbie research harness (agentic arm, Python interpreter as the only tool).

## Setup

Requires `uv` and `pnpm`.

```sh
cp .env.example .env   # fill in ZAI_API_KEY (Z.AI pay-as-you-go key)
uv sync
```

If `ZAI_API_KEY` is empty, the backend reads the `zai-api` key from `~/.pi/agent/auth.json`.
`COBBIE_MODEL` (default `glm-5.3-flash`) and `COBBIE_BASE_URL` (default `https://api.z.ai/api/paas/v4`) select the model.

## Run

Backend on port 8000:

```sh
uv run uvicorn backend.app:app --port 8000
```

Frontend on port 5173 (proxies `/api` to port 8000):

```sh
cd frontend && pnpm install && pnpm dev
```

Smoke test of the backend, in-process, against the live LLM:

```sh
uv run python scripts/smoke_backend.py
```

## API

- `GET /api/health`
- `POST /api/models` with multipart field `file` (`.ifc`) returns `{model_id, filename, schema, n_products}`.
- `POST /api/chat` with JSON `{model_id, message}` returns a `text/event-stream` of `data: {json}` events: `status`, `code`, `output`, `answer`, `error`, `done`.
- The `answer` event is `{type, text, element_groups, sufficient_info}`, where each group is `{label, elements: [{global_id, ifc_class, name}]}` and the label ends with the element count, e.g. `All walls (57)`.
- `POST /api/models/{model_id}/reset` clears the conversation and restarts the kernel.

## Architecture

1. `backend/app.py` (FastAPI) keeps one session per uploaded model: the file in `data/uploads/`, an in-process ifcopenshell model, a Jupyter kernel and an agent.
2. `backend/interpreter.py` runs the kernel, which opens the model as `model` at startup and keeps state across questions.
3. `backend/agent.py` builds a deepagents agent whose only tool is `python_exec`, capped at 15 code rounds per question before it must answer.
4. The agent hands elements to the answer through the kernel dict `answer_elements` (label to GlobalIds or entities), which the server clears at the start of each question, so large groups never pass through the LLM.
5. The agent ends each turn with a structured `Answer` (text, the labels of the groups to show, sufficient_info); hand-listed `global_ids` per group still work as a fallback, and an in-memory LangGraph checkpointer gives follow-up questions the earlier turns.
6. After the turn the server reads `answer_elements` from the kernel, resolves each GlobalId against the in-process model (IFC class, name), drops unknown ids, caps each group at 5000 elements, and streams code, outputs and the answer to the UI as they happen.
