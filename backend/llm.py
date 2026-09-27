"""Chat model factory for the Z.AI pay-as-you-go endpoint (OpenAI-compatible)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

load_dotenv()

DEFAULT_MODEL = "glm-5.3-flash"
DEFAULT_BASE_URL = "https://api.z.ai/api/paas/v4"
_AUTH_FILE = Path.home() / ".pi" / "agent" / "auth.json"


def _api_key() -> str:
    key = os.environ.get("ZAI_API_KEY", "").strip()
    if key:
        return key
    try:
        return json.loads(_AUTH_FILE.read_text())["zai-api"]["key"]
    except (OSError, KeyError, ValueError) as exc:
        raise RuntimeError(
            "No Z.AI key: set ZAI_API_KEY in .env or provide zai-api in ~/.pi/agent/auth.json"
        ) from exc


def model_name() -> str:
    return os.environ.get("COBBIE_MODEL", DEFAULT_MODEL)


def init_llm(*, max_tokens: int = 16384, timeout: float = 120) -> ChatOpenAI:
    """Build the chat model.

    glm-5.3-flash always reasons; disabling thinking errors out, so we only
    lower the effort.
    """
    return ChatOpenAI(
        model=model_name(),
        base_url=os.environ.get("COBBIE_BASE_URL", DEFAULT_BASE_URL),
        api_key=_api_key(),
        reasoning_effort=os.environ.get("COBBIE_REASONING_EFFORT", "low"),
        max_tokens=max_tokens,
        timeout=timeout,
        max_retries=3,
    )
