"""IFC chat agent: a deepagents CodeAct loop with one tool, ``python_exec``.

Trimmed port of the cobbie research harness (agentic arm, no curated helpers).
The deepagents built-in tools (todos, filesystem, subagents) are stripped per
model call, and its default system prompt is replaced by our static prompt, so
the agent's only action is running Python in the model's kernel. A cap on code
rounds per question forces the structured ``Answer`` afterwards.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from deepagents import create_deep_agent
from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from pydantic import BaseModel, Field

from backend.interpreter import TRUNCATION_MARKER, JupyterInterpreter
from backend.llm import init_llm
from backend.prompts import SYSTEM_PROMPT, WRAP_UP_MSG

TOOL_NAME = "python_exec"
DEFAULT_CAP = 15
RECURSION_LIMIT = 100


class ElementGroup(BaseModel):
    """A group of IFC elements the answer refers to, shown as one chip."""

    label: str = Field(description="Short label with the count, e.g. 'Doors on Erdgeschoss (5)'.")
    global_ids: list[str] = Field(
        description="IFC GlobalId values (22-character strings) copied exactly from code output. Never express ids like #123."
    )


class Answer(BaseModel):
    """The final answer to the user's question about the IFC model."""

    text: str = Field(
        description="Concise markdown answer for the user. No code, no tool-call syntax, no long GlobalId lists."
    )
    element_groups: list[ElementGroup] = Field(
        default_factory=list,
        description="Elements the answer refers to, grouped for clickable chips; empty if the answer concerns no specific elements.",
    )
    sufficient_info: bool = Field(
        description="True if the model contained enough information to answer the question."
    )


ANSWER_TOOL_NAME = Answer.__name__


def _rounds_this_turn(messages: list) -> int:
    """AI messages since the last real user message (checkpointed history spans turns)."""
    n = 0
    for m in reversed(messages):
        t = getattr(m, "type", None)
        if t == "human":
            break
        if t == "ai":
            n += 1
    return n


class OnlyPythonCapMiddleware(AgentMiddleware):
    """Restrict tools to python_exec, pin the system prompt, and cap code rounds.

    After ``cap`` model turns in the current question, the exploration tool is
    removed and a wrap-up message is injected; the structured-output factory
    keeps the ``Answer`` tool and forces a tool choice, so the only legal move
    is to answer.
    """

    def __init__(self, *, cap: int = DEFAULT_CAP) -> None:
        super().__init__()
        self._cap = cap

    def _adjust(self, request: ModelRequest) -> ModelRequest:
        tools = [t for t in request.tools if getattr(t, "name", None) == TOOL_NAME]
        overrides: dict[str, Any] = {
            "tools": tools,
            "system_message": SystemMessage(content=SYSTEM_PROMPT),
        }
        if _rounds_this_turn(request.state.get("messages", [])) >= self._cap:
            overrides["tools"] = []
            overrides["messages"] = [*request.messages, HumanMessage(content=WRAP_UP_MSG)]
        return request.override(**overrides)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse | Any:
        return handler(self._adjust(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | Any:
        return await handler(self._adjust(request))


def create_ifc_agent(interp: JupyterInterpreter, *, cap: int = DEFAULT_CAP):
    """Build the agent bound to one model's kernel, with its own in-memory checkpointer."""

    @tool(TOOL_NAME)
    def python_exec(code: str) -> str:
        """Execute Python code in a persistent Jupyter kernel.

        The IFC model is already open as `model` (ifcopenshell.file). Variables
        and imports persist across calls. Only stdout and stderr are returned;
        use print().
        """
        return interp.run(code)

    return create_deep_agent(
        model=init_llm(),
        tools=[python_exec],
        system_prompt=SYSTEM_PROMPT,
        middleware=[OnlyPythonCapMiddleware(cap=cap)],
        response_format=ToolStrategy(Answer),
        # The structured Answer lives in checkpointed state; allow it explicitly.
        checkpointer=InMemorySaver(
            serde=JsonPlusSerializer(
                allowed_msgpack_modules=[
                    (Answer.__module__, Answer.__name__),
                    (ElementGroup.__module__, ElementGroup.__name__),
                ]
            )
        ),
    )


async def stream_turn(agent, thread_id: str, message: str) -> AsyncIterator[dict]:
    """Run one user question and yield raw events as they happen.

    Yields dicts with ``type`` in {status, code, output, answer}. The answer
    event carries the parsed ``Answer`` under ``answer``. If the model ends
    without an Answer, up to two wrap-up turns are attempted.
    """
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT}
    payload: dict = {"messages": [HumanMessage(content=message)]}
    answer: Answer | None = None

    for _attempt in range(3):
        yield {"type": "status", "text": "thinking"}
        async for update in agent.astream(payload, config=config, stream_mode="updates"):
            if not isinstance(update, dict):
                continue
            for node, delta in update.items():
                if not isinstance(delta, dict):
                    continue
                sr = delta.get("structured_response")
                if isinstance(sr, Answer):
                    answer = sr
                msgs = delta.get("messages") or []
                if not isinstance(msgs, list):
                    msgs = [msgs]
                for m in msgs:
                    mtype = getattr(m, "type", None)
                    if mtype == "ai":
                        for tc in getattr(m, "tool_calls", None) or []:
                            if tc.get("name") == TOOL_NAME:
                                yield {"type": "code", "code": tc.get("args", {}).get("code", "")}
                    elif mtype == "tool" and getattr(m, "name", "") == TOOL_NAME:
                        text = m.content if isinstance(m.content, str) else str(m.content)
                        yield {
                            "type": "output",
                            "text": text,
                            "truncated": TRUNCATION_MARKER in text,
                        }
                        yield {"type": "status", "text": "thinking"}
        if answer is not None:
            break
        payload = {"messages": [HumanMessage(content=WRAP_UP_MSG)]}

    if answer is None:
        raise RuntimeError("The agent ended without producing an answer.")
    yield {"type": "answer", "answer": answer}
