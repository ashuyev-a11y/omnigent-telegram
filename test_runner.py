"""Tests for runner.py.

The real `omnigent run` subprocess is never invoked: asyncio.create_subprocess_exec
is monkeypatched with a fake process. No network, no token required.
"""

from __future__ import annotations

import asyncio

import pytest

import runner as runner_module
from runner import (
    OmnigentRunner,
    TaskAlreadyRunningError,
    extract_session_id,
    extract_session_url,
    split_message,
    tail_text,
)


# ---------------------------------------------------------------------------
# extract_session_url
# ---------------------------------------------------------------------------


def test_extract_session_url_matches():
    line = "Omnigent session: http://127.0.0.1:8000/c/abc123\n"
    assert extract_session_url(line) == "http://127.0.0.1:8000/c/abc123"


def test_extract_session_url_no_match_returns_none():
    assert extract_session_url("some unrelated first line\n") is None


# ---------------------------------------------------------------------------
# extract_session_id
# ---------------------------------------------------------------------------


def test_extract_session_id_takes_last_path_segment():
    assert extract_session_id("http://127.0.0.1:8000/c/abc123") == "abc123"


def test_extract_session_id_strips_trailing_slash():
    assert extract_session_id("http://127.0.0.1:8000/c/abc123/") == "abc123"


def test_extract_session_id_empty_input_returns_none():
    assert extract_session_id("") is None


def test_extract_session_id_no_path_returns_none():
    assert extract_session_id("http://127.0.0.1:8000") is None
    assert extract_session_id("http://127.0.0.1:8000/") is None


# ---------------------------------------------------------------------------
# split_message
# ---------------------------------------------------------------------------


def test_split_message_short_text_single_chunk():
    assert split_message("hello\nworld\n") == ["hello\nworld\n"]


def test_split_message_empty_text():
    assert split_message("") == []


def test_split_message_splits_on_line_boundaries():
    line = "x" * 100 + "\n"
    text = line * 50  # 5050 chars, over the 4096 limit
    chunks = split_message(text, limit=4096)

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 4096
    # No line was cut in half: every chunk consists of whole lines.
    for chunk in chunks:
        for sub in chunk.splitlines(keepends=True):
            assert sub == line
    assert "".join(chunks) == text


def test_split_message_oversized_single_line_is_hard_split():
    line = "y" * 9000
    chunks = split_message(line, limit=4096)

    assert len(chunks) == 3
    assert all(len(c) <= 4096 for c in chunks)
    assert "".join(chunks) == line


# ---------------------------------------------------------------------------
# tail_text
# ---------------------------------------------------------------------------


def test_tail_text_returns_full_text_when_short():
    assert tail_text("short text") == "short text"


def test_tail_text_keeps_last_lines_within_limit():
    lines = [f"line-{i}\n" for i in range(1000)]
    text = "".join(lines)

    tail = tail_text(text, limit=100)

    assert len(tail) <= 100
    assert text.endswith(tail)
    # only whole lines
    for sub in tail.splitlines(keepends=True):
        assert sub in lines


# ---------------------------------------------------------------------------
# OmnigentRunner.run — subprocess execution is faked
# ---------------------------------------------------------------------------


class FakeStreamReader:
    def __init__(self, lines: list[bytes]):
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        return b""


class FakeProcess:
    def __init__(self, stdout_lines, stderr_lines, returncode=0, wait_delay=0.0):
        self.stdout = FakeStreamReader(stdout_lines)
        self.stderr = FakeStreamReader(stderr_lines)
        self.returncode = None
        self._final_returncode = returncode
        self._wait_delay = wait_delay
        self.terminated = False
        self.killed = False

    async def wait(self):
        if self.returncode is not None:
            return self.returncode
        if self._wait_delay:
            await asyncio.sleep(self._wait_delay)
        self.returncode = self._final_returncode
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True
        self.returncode = -9


def _patch_subprocess(monkeypatch, fake_process):
    calls = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return fake_process

    monkeypatch.setattr(
        runner_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    return calls


def test_run_success_extracts_url_and_returns_stdout(monkeypatch):
    fake_process = FakeProcess(
        stdout_lines=[
            b"Omnigent session: http://127.0.0.1:8000/c/42\n",
            b"line one\n",
            b"line two\n",
        ],
        stderr_lines=[],
        returncode=0,
    )
    calls = _patch_subprocess(monkeypatch, fake_process)

    seen_urls = []

    async def on_session_url(url):
        seen_urls.append(url)

    async def scenario():
        r = OmnigentRunner()
        return await r.run(
            chat_id=1,
            bundle_path="/bundle",
            workdir="/workdir",
            task_text="do the thing",
            timeout_seconds=5,
            on_session_url=on_session_url,
        )

    result = asyncio.run(scenario())

    assert seen_urls == ["http://127.0.0.1:8000/c/42"]
    assert result.session_url == "http://127.0.0.1:8000/c/42"
    assert result.stdout_text == "line one\nline two\n"
    assert result.returncode == 0
    assert not result.timed_out
    assert not result.cancelled

    # command passed as an argument list, never a shell string
    assert calls[0]["args"] == ("omnigent", "run", "/bundle", "-p", "do the thing")
    assert calls[0]["kwargs"]["cwd"] == "/workdir"


def test_run_with_resume_session_id_adds_resume_flag(monkeypatch):
    fake_process = FakeProcess(
        stdout_lines=[b"Omnigent session: http://127.0.0.1:8000/c/42\n"],
        stderr_lines=[],
        returncode=0,
    )
    calls = _patch_subprocess(monkeypatch, fake_process)

    async def scenario():
        r = OmnigentRunner()
        return await r.run(
            chat_id=1,
            bundle_path="/bundle",
            workdir="/workdir",
            task_text="continue please",
            timeout_seconds=5,
            resume_session_id="prev-session-id",
        )

    asyncio.run(scenario())

    assert calls[0]["args"] == (
        "omnigent",
        "run",
        "/bundle",
        "-p",
        "continue please",
        "--resume",
        "prev-session-id",
    )


def test_run_without_resume_session_id_omits_resume_flag(monkeypatch):
    fake_process = FakeProcess(
        stdout_lines=[b"Omnigent session: http://127.0.0.1:8000/c/42\n"],
        stderr_lines=[],
        returncode=0,
    )
    calls = _patch_subprocess(monkeypatch, fake_process)

    async def scenario():
        r = OmnigentRunner()
        return await r.run(
            chat_id=1,
            bundle_path="/bundle",
            workdir="/workdir",
            task_text="fresh start",
            timeout_seconds=5,
        )

    asyncio.run(scenario())

    assert calls[0]["args"] == ("omnigent", "run", "/bundle", "-p", "fresh start")
    assert "--resume" not in calls[0]["args"]


def test_run_first_line_without_url_is_not_lost(monkeypatch):
    fake_process = FakeProcess(
        stdout_lines=[
            b"not a session line\n",
            b"line two\n",
        ],
        stderr_lines=[],
        returncode=0,
    )
    _patch_subprocess(monkeypatch, fake_process)

    seen_urls = []

    async def on_session_url(url):
        seen_urls.append(url)

    async def scenario():
        r = OmnigentRunner()
        return await r.run(
            chat_id=1,
            bundle_path="/bundle",
            workdir="/workdir",
            task_text="task",
            timeout_seconds=5,
            on_session_url=on_session_url,
        )

    result = asyncio.run(scenario())

    assert seen_urls == [None]
    assert result.session_url is None
    # The first line is preserved in the output instead of being dropped,
    # since it was not actually the session URL line.
    assert result.stdout_text == "not a session line\nline two\n"


def test_run_nonzero_returncode_reports_stderr(monkeypatch):
    fake_process = FakeProcess(
        stdout_lines=[b"Omnigent session: http://127.0.0.1:8000/c/1\n"],
        stderr_lines=[b"boom\n", b"traceback...\n"],
        returncode=1,
    )
    _patch_subprocess(monkeypatch, fake_process)

    async def scenario():
        r = OmnigentRunner()
        return await r.run(
            chat_id=1,
            bundle_path="/bundle",
            workdir="/workdir",
            task_text="task",
            timeout_seconds=5,
        )

    result = asyncio.run(scenario())

    assert result.returncode == 1
    assert result.stderr_text == "boom\ntraceback...\n"
    assert not result.timed_out


def test_run_timeout_kills_process(monkeypatch):
    fake_process = FakeProcess(
        stdout_lines=[b"Omnigent session: http://127.0.0.1:8000/c/1\n"],
        stderr_lines=[],
        returncode=0,
        wait_delay=10,  # would hang well past the timeout below
    )
    _patch_subprocess(monkeypatch, fake_process)

    async def scenario():
        r = OmnigentRunner()
        return await r.run(
            chat_id=1,
            bundle_path="/bundle",
            workdir="/workdir",
            task_text="task",
            timeout_seconds=0.05,
        )

    result = asyncio.run(scenario())

    assert result.timed_out
    assert fake_process.killed


def test_run_rejects_second_task_for_same_chat(monkeypatch):
    fake_process = FakeProcess(
        stdout_lines=[b"Omnigent session: http://127.0.0.1:8000/c/1\n"],
        stderr_lines=[],
        returncode=0,
        wait_delay=0.2,
    )
    _patch_subprocess(monkeypatch, fake_process)

    async def scenario():
        r = OmnigentRunner()
        first = asyncio.create_task(
            r.run(
                chat_id=1,
                bundle_path="/bundle",
                workdir="/workdir",
                task_text="task one",
                timeout_seconds=5,
            )
        )
        await asyncio.sleep(0)  # let `first` register itself as running
        assert r.is_busy(1)

        with pytest.raises(TaskAlreadyRunningError):
            await r.run(
                chat_id=1,
                bundle_path="/bundle",
                workdir="/workdir",
                task_text="task two",
                timeout_seconds=5,
            )

        await first
        assert not r.is_busy(1)

    asyncio.run(scenario())


def test_cancel_terminates_process_and_marks_result(monkeypatch):
    fake_process = FakeProcess(
        stdout_lines=[b"Omnigent session: http://127.0.0.1:8000/c/1\n"],
        stderr_lines=[],
        returncode=0,
        wait_delay=0.05,
    )
    _patch_subprocess(monkeypatch, fake_process)

    async def scenario():
        r = OmnigentRunner()
        run_task = asyncio.create_task(
            r.run(
                chat_id=1,
                bundle_path="/bundle",
                workdir="/workdir",
                task_text="task",
                timeout_seconds=5,
            )
        )
        await asyncio.sleep(0)
        cancelled = await r.cancel(1)
        assert cancelled is True
        assert fake_process.terminated

        result = await run_task
        assert result.cancelled is True

    asyncio.run(scenario())


def test_cancel_with_no_running_task_returns_false():
    async def scenario():
        r = OmnigentRunner()
        return await r.cancel(999)

    assert asyncio.run(scenario()) is False
