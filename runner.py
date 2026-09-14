"""Launches and supervises `omnigent run` subprocesses.

At most one subprocess runs per chat at a time. The subprocess is always
started with ``asyncio.create_subprocess_exec`` (argument list, never a
shell string), so task text from the user is never interpreted by a shell.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

TELEGRAM_MESSAGE_LIMIT = 4096
SESSION_URL_RE = re.compile(r"Omnigent session:\s*(\S+)")

OnSessionUrl = Callable[[Optional[str]], Awaitable[None]]


class TaskAlreadyRunningError(Exception):
    """Raised when a run is requested for a chat that already has one."""

    def __init__(self, session_url: Optional[str] = None):
        self.session_url = session_url
        super().__init__("A task is already running for this chat")


@dataclass
class RunResult:
    returncode: Optional[int]
    stdout_text: str
    stderr_text: str
    duration_seconds: float
    timed_out: bool
    cancelled: bool
    session_url: Optional[str]


@dataclass
class _RunningTask:
    chat_id: int
    started_at: float
    process: object = None
    session_url: Optional[str] = None
    cancel_requested: bool = False


def extract_session_url(line: str) -> Optional[str]:
    """Extract the session URL from the first line of `omnigent run` output.

    Expected format: ``Omnigent session: http://127.0.0.1:8000/c/<id>``.
    Returns ``None`` if the line does not match.
    """
    match = SESSION_URL_RE.search(line)
    return match.group(1) if match else None


def split_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Split ``text`` into chunks no longer than ``limit`` characters.

    Splits happen on line boundaries. A single line longer than ``limit`` is
    hard-split as a last resort, since it cannot fit any other way.
    """
    if not text:
        return []

    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            for start in range(0, len(line), limit):
                chunks.append(line[start : start + limit])
            continue

        if len(current) + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current += line

    if current:
        chunks.append(current)

    return chunks


def tail_text(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> str:
    """Return a suffix of ``text`` at most ``limit`` characters, on line boundaries."""
    if len(text) <= limit:
        return text

    result = ""
    for line in reversed(text.splitlines(keepends=True)):
        if len(result) + len(line) > limit:
            break
        result = line + result

    if not result:
        result = text[-limit:]

    return result


class OmnigentRunner:
    """Runs `omnigent run` subprocesses, enforcing one task per chat."""

    def __init__(self) -> None:
        self._running: dict[int, _RunningTask] = {}

    def is_busy(self, chat_id: int) -> bool:
        return chat_id in self._running

    def get_session_url(self, chat_id: int) -> Optional[str]:
        task = self._running.get(chat_id)
        return task.session_url if task else None

    async def cancel(self, chat_id: int) -> bool:
        """Terminate the running subprocess for ``chat_id``, if any."""
        task = self._running.get(chat_id)
        if task is None:
            return False
        task.cancel_requested = True
        if task.process is not None:
            task.process.terminate()
        return True

    async def run(
        self,
        chat_id: int,
        bundle_path: str,
        workdir: str,
        task_text: str,
        timeout_seconds: float,
        on_session_url: Optional[OnSessionUrl] = None,
    ) -> RunResult:
        """Start `omnigent run` for ``chat_id`` and wait for it to finish.

        Raises :class:`TaskAlreadyRunningError` if a task is already running
        for this chat. The check-and-register step is synchronous (no
        ``await`` in between), so concurrent calls for the same chat cannot
        both proceed.
        """
        if chat_id in self._running:
            raise TaskAlreadyRunningError(self._running[chat_id].session_url)

        started_at = time.monotonic()
        state = _RunningTask(chat_id=chat_id, started_at=started_at)
        self._running[chat_id] = state

        try:
            command = ["omnigent", "run", bundle_path, "-p", task_text]
            logger.info("task started chat_id=%s", chat_id)

            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=workdir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            state.process = process

            if state.cancel_requested:
                process.terminate()

            stdout_lines: list[str] = []
            stderr_chunks: list[bytes] = []
            timed_out = False

            async def read_stdout() -> None:
                first_line = True
                while True:
                    raw = await process.stdout.readline()
                    if not raw:
                        break
                    if first_line:
                        first_line = False
                        text = raw.decode(errors="replace")
                        url = extract_session_url(text)
                        state.session_url = url
                        if on_session_url is not None:
                            await on_session_url(url)
                        continue
                    stdout_lines.append(raw.decode(errors="replace"))

            async def read_stderr() -> None:
                while True:
                    raw = await process.stderr.readline()
                    if not raw:
                        break
                    stderr_chunks.append(raw)

            try:
                await asyncio.wait_for(
                    asyncio.gather(read_stdout(), read_stderr(), process.wait()),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError:
                timed_out = True
                process.kill()
                await process.wait()

            duration = time.monotonic() - started_at
            stderr_text = b"".join(stderr_chunks).decode(errors="replace")

            logger.info(
                "task finished chat_id=%s returncode=%s duration=%.2f timed_out=%s cancelled=%s",
                chat_id,
                process.returncode,
                duration,
                timed_out,
                state.cancel_requested,
            )

            return RunResult(
                returncode=process.returncode,
                stdout_text="".join(stdout_lines),
                stderr_text=stderr_text,
                duration_seconds=duration,
                timed_out=timed_out,
                cancelled=state.cancel_requested,
                session_url=state.session_url,
            )
        finally:
            del self._running[chat_id]
