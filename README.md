# cobbie-demo

Local demo web app for asking questions about an IFC building model.
The user uploads an IFC file, sees it in a 3D viewer, and chats with an LLM agent.
The agent answers by writing and running Python (ifcopenshell) in a persistent Jupyter kernel.
Each answer carries groups of element GlobalIds, which the UI shows as clickable chips that highlight those elements in the viewer.

The agent is a trimmed port of the cobbie research harness (agentic arm, Python interpreter as the only tool).

## Setup

Requires `uv`, `pnpm` and Node 22 or newer; runs on macOS and Linux.

```sh
cp .env.example .env   # fill in ZAI_API_KEY (Z.AI pay-as-you-go key)
uv sync
cd frontend && pnpm install
```

If `ZAI_API_KEY` is empty, the backend reads the `zai-api` key from `~/.pi/agent/auth.json`.
`COBBIE_MODEL` (default `glm-5.3-flash`) and `COBBIE_BASE_URL` (default `https://api.z.ai/api/paas/v4`) select the model.

## Run

Two terminals, both from the repo root.

Backend on port 8000:

```sh
uv sync
uv run uvicorn backend.app:app --port 8000
```

Frontend on port 5173, which proxies `/api` to the backend:

```sh
cd frontend && pnpm install && pnpm dev
```

Then open http://localhost:5173.
If the backend runs elsewhere, start the frontend with `COBBIE_BACKEND_URL=http://127.0.0.1:8010 pnpm dev`.

`pnpm dev` and `pnpm build` first run `frontend/scripts/copy-wasm.mjs`, which copies the web-ifc wasm into `frontend/public/wasm/` (gitignored).
web-ifc is pinned to 0.0.77, the version That Open 3.4 is built against; 0.0.78's browser wasm rejects the extra `StreamMeshes` argument That Open passes.
The fragments worker is bundled by Vite, so the app needs no CDN at runtime.

## Using the app

1. Load a model with Upload IFC or one of the sample buttons.
   The browser converts the IFC into That Open fragments for the viewer while the same file is uploaded to the backend for the agent; the top bar shows both states, the schema and the product count.
   Loading another model resets the viewer and the chat.
2. Navigate the viewer with the mouse (orbit, pan, zoom); the camera fits the model on load.
   Clicking an element selects it and opens a property panel with GlobalId, IFC class, name, attributes, spatial container, property sets and quantity sets (collapsible); clicking empty space clears the selection.
3. Ask a question in the chat (Enter sends, Shift+Enter adds a line) or click one of the example questions.
   While the agent works, its code and outputs stream in live with an elapsed timer; afterwards they collapse into "Show N steps".
   New chat clears the conversation and restarts the agent's kernel.
4. Each answer lists its element groups as chips, e.g. "All walls (57)".
   Clicking a chip shows that group in the viewer and fits the camera to it; clicking it again restores the view.
   The control above the viewer picks how: Highlight (colour only), Ghost others (default, other elements at low opacity) or Isolate (others hidden).
   The small arrow on a chip lists its elements; clicking one selects it and opens its properties.
   Reset view clears groups and selection.

## Tests

Smoke test of the backend, in-process, against the live LLM:

```sh
uv run python scripts/smoke_backend.py
```

It asserts the API contract hard (event types and order, one answer, `done` last, non-empty text, every element id resolving in the model to an expected class) and checks group content on the union of all groups (the 5 doors of fzk_house, all 57 walls of duplex), since the agent may split elements differently from run to run.
A follow-up answer without groups, or walls not passed through `answer_elements`, prints a warning instead of failing.
The agent can still occasionally skip the groups on the first question, which fails the run.

End-to-end check in headless Chrome, driven over the DevTools protocol with Node's built-in WebSocket (no extra packages):

```sh
node scripts/e2e_screenshots.mjs
```

The script starts its own backend and Vite dev server, launches Chrome, loads the fzk_house sample, clicks an element, asks "Show me all the doors", clicks the answer chip, switches to Isolate and opens one element from the chip list, then tests New chat and a file upload.
It writes screenshots `cobbie-demo-1-loaded.png`, `-2-properties.png`, `-running.png`, `-3-chat-chip.png`, `-4-isolate-element.png` and `-5-uploaded.png` to `~/Documents/tmp/`, prints browser console errors, exits non-zero on any error, and kills everything it started.
The ports must be free; override them and other settings with environment variables, e.g. `BACKEND_PORT=8010 VITE_PORT=5180 OUT_DIR=/tmp/shots node scripts/e2e_screenshots.mjs`.
Other variables: `CHROME` (browser binary; defaults to Google Chrome on macOS and Playwright's Chromium on Linux), `CDP_PORT`, `SAMPLE`, `QUESTION`.

## API

- `GET /api/health`
- `GET /api/samples` lists the models in `samples/`; `GET /api/samples/{name}` returns one of them.
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

Frontend (`frontend/src/`, Vite, React, TypeScript, plain CSS):

1. `viewer/engine.ts` sets up That Open Components (one world, `SimpleCamera`, `SimpleRenderer`, `FragmentsManager`, `IfcLoader`), handles click picking, and applies group display and selection through fragments highlight, opacity and visibility.
2. `viewer/properties.ts` turns fragments item data (attributes, `IsDefinedBy`, `ContainedInStructure`) into the property panel's groups; `viewer/ViewerPanel.tsx` and `viewer/PropertyPanel.tsx` render the viewer overlays.
3. `chat/sse.ts` posts to `/api/chat` and parses the event stream from `fetch`; `chat/ChatPanel.tsx`, `chat/AgentTurn.tsx` and `chat/ElementChip.tsx` render the conversation, the agent steps and the chips.
4. `chat/AnswerMarkdown.tsx` renders answers with react-markdown and remark-gfm (tables in a scrolling wrapper).
5. `App.tsx` holds the app state: model load and upload, chat turns, active chip and display mode.
