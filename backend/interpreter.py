"""Persistent Python interpreter backed by a Jupyter kernel.

Trimmed port of the cobbie research harness interpreter. One kernel per
uploaded model; a preamble (open the IFC file as ``model``) is re-run after
every restart so the agent always finds the model loaded.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from queue import Empty

import jupyter_client
import tiktoken

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# Token budget for one python_exec output (cl100k_base, about 32 kB of text).
DEFAULT_MAX_OUTPUT_TOKENS = 8192
# Keep 70% head and 30% tail of truncated output.
_HEAD_FRACTION = 0.70
TRUNCATION_MARKER = "[OUTPUT TRUNCATED"
_CLOSED_TEXT = "[Interpreter shut down] This model's kernel was closed; stop and answer."

_ENCODING = tiktoken.get_encoding("cl100k_base")


def truncate_by_tokens(text: str, budget: int) -> tuple[str, bool]:
    """Truncate ``text`` to at most ``budget`` tokens, keeping head and tail."""
    ids = _ENCODING.encode(text)
    total = len(ids)
    if total <= budget:
        return text, False
    head_n = max(1, int(budget * _HEAD_FRACTION))
    tail_n = max(1, budget - head_n)
    dropped = total - head_n - tail_n
    marker = (
        f"\n\n... {TRUNCATION_MARKER}. {dropped} of {total} tokens omitted "
        f"(budget: {budget} tokens). Narrow your next query: slice the list, "
        f"aggregate before printing, or print only the fields you need.] ...\n\n"
    )
    return _ENCODING.decode(ids[:head_n]) + marker + _ENCODING.decode(ids[-tail_n:]), True


@dataclass
class ExecResult:
    text: str
    truncated: bool = False
    timed_out: bool = False


class JupyterInterpreter:
    """Persistent Python kernel with a re-applied preamble."""

    def __init__(
        self,
        preamble: str = "",
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    ) -> None:
        self.max_output_tokens = max_output_tokens
        self._preamble = preamble
        self._lock = threading.Lock()
        self._closed = False
        # IPC sockets: local-only, and no plain-TCP warning from the kernel.
        # Socket files go to the temp dir, not the working directory.
        self.km = jupyter_client.KernelManager(
            kernel_name="python3",
            transport="ipc",
            ip=os.path.join(tempfile.gettempdir(), f"cobbie-kernel-{uuid.uuid4().hex[:8]}"),
        )
        self.km.start_kernel()
        self.kc = self.km.blocking_client()
        try:
            self.kc.start_channels()
            self.kc.wait_for_ready(timeout=60)
            if preamble:
                res = self.execute(preamble, timeout=120)
                if "Traceback" in res.text or res.timed_out:
                    raise RuntimeError(f"Kernel preamble failed:\n{res.text}")
        except BaseException:
            self.shutdown()
            raise

    def run(self, code: str, timeout: int = 60) -> str:
        return self.execute(code, timeout=timeout).text

    def execute(self, code: str, timeout: int = 60, *, truncate: bool = True) -> ExecResult:
        """Execute ``code`` and collect stdout, stderr, results and tracebacks.

        ``truncate=False`` returns the full output, for backend-internal reads
        that never reach the LLM.
        """
        with self._lock:
            try:
                if self._closed:
                    return ExecResult(_CLOSED_TEXT)
                return self._execute_locked(code, timeout, truncate=truncate)
            finally:
                if self._closed:  # shut down mid-run: this thread owns the channels
                    self._stop_channels()

    def _execute_locked(self, code: str, timeout: int, *, truncate: bool = True) -> ExecResult:
        msg_id = self.kc.execute(code)
        parts: list[str] = []
        deadline = time.monotonic() + timeout
        timed_out = False
        while True:
            if self._closed:
                return ExecResult(_CLOSED_TEXT)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                # Short polls so a shutdown from another thread is noticed quickly.
                msg = self.kc.get_iopub_msg(timeout=min(remaining, 1.0))
            except Empty:
                continue
            except Exception:
                break
            if msg.get("parent_header", {}).get("msg_id") != msg_id:
                continue
            msg_type = msg["header"]["msg_type"]
            content = msg["content"]
            if msg_type == "stream":
                parts.append(content.get("text", ""))
            elif msg_type == "execute_result":
                parts.append(content.get("data", {}).get("text/plain", "") + "\n")
            elif msg_type == "error":
                parts.append(_ANSI_RE.sub("", "\n".join(content.get("traceback", []))))
            elif msg_type == "status" and content.get("execution_state") == "idle":
                break
        if self._closed:
            return ExecResult(_CLOSED_TEXT)
        if timed_out:
            self._recover_from_timeout()
            return ExecResult(
                f"[Timeout] Execution exceeded {timeout}s and was interrupted.",
                timed_out=True,
            )
        if not truncate:
            return ExecResult("".join(parts))
        text, truncated = truncate_by_tokens("".join(parts), self.max_output_tokens)
        return ExecResult(text, truncated=truncated)

    def _recover_from_timeout(self) -> None:
        """Interrupt a runaway cell and drain its stale messages, else restart."""
        self.interrupt()
        idle = False
        drain_deadline = time.monotonic() + 10
        while time.monotonic() < drain_deadline:
            try:
                msg = self.kc.get_iopub_msg(timeout=1)
            except Empty:
                if idle:
                    break
                continue
            except Exception:
                break
            if (
                msg.get("header", {}).get("msg_type") == "status"
                and msg.get("content", {}).get("execution_state") == "idle"
            ):
                idle = True
        while True:
            try:
                self.kc.get_shell_msg(timeout=0.2)
            except Exception:
                break
        if not idle:
            self._restart_locked()

    def interrupt(self) -> None:
        try:
            self.km.interrupt_kernel()
        except Exception:
            pass

    def _restart_locked(self) -> None:
        if self._closed:
            return
        self.km.restart_kernel(now=True)
        self.kc.wait_for_ready(timeout=60)
        if self._preamble:
            self._execute_locked(self._preamble, 120)

    def reset(self) -> None:
        """Restart the kernel with a clean namespace and re-run the preamble."""
        with self._lock:
            self._restart_locked()

    def shutdown(self) -> None:
        """Kill the kernel; safe while another thread is executing on it.

        A running ``execute`` notices the flag within a second, returns a
        short notice instead of raising, and closes the channels itself, so
        no two threads touch the same ZMQ sockets.
        """
        self._closed = True
        try:
            self.km.shutdown_kernel(now=True)
        except Exception:
            pass
        # A running execute returns within about a second; if it is still busy
        # (e.g. draining after a timeout), it closes the channels itself.
        if self._lock.acquire(timeout=5):
            try:
                self._stop_channels()
            finally:
                self._lock.release()

    def _stop_channels(self) -> None:
        try:
            self.kc.stop_channels()
        except Exception:
            pass
