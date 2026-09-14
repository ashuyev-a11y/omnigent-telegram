"""Tests for bot.py: `_run_task` error handling per SPEC.md item 6, and
session resume behavior (state.json continuation, /new).

No Telegram network access and no real subprocess involved: `runner.run` is
monkeypatched directly, and Telegram's `Bot.send_message` is replaced with an
in-memory recorder. Session state is a real :class:`SessionStateStore`
pointed at a tmp_path file, so tests exercise the actual persistence too.
"""

from __future__ import annotations

import asyncio
import logging

import bot as bot_module
import runner as runner_module
from runner import TaskAlreadyRunningError
from state import SessionStateStore


class FakeBot:
    def __init__(self):
        self.sent_messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text):
        self.sent_messages.append((chat_id, text))


class FakeContext:
    def __init__(self):
        self.bot = FakeBot()
        self.bot_data = {}


class FakeProject:
    workdir = "/workdir"


class FakeConfig:
    bundle_path = "/bundle"
    timeout_seconds = 5


class FakeRunResult:
    def __init__(
        self,
        returncode=0,
        stdout_text="",
        stderr_text="",
        timed_out=False,
        cancelled=False,
        session_url=None,
    ):
        self.returncode = returncode
        self.stdout_text = stdout_text
        self.stderr_text = stderr_text
        self.timed_out = timed_out
        self.cancelled = cancelled
        self.session_url = session_url


class FakeStreamReader:
    """Minimal stand-in for asyncio.StreamReader, matching test_runner.py."""

    def __init__(self, lines: list[bytes]):
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        return b""


class FakeSubprocess:
    """Minimal stand-in for the process object asyncio.create_subprocess_exec
    returns, matching the FakeProcess used in test_runner.py."""

    def __init__(self, stdout_lines, stderr_lines, returncode=0):
        self.stdout = FakeStreamReader(stdout_lines)
        self.stderr = FakeStreamReader(stderr_lines)
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    async def wait(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def test_run_task_reports_unexpected_exception_to_chat(monkeypatch, tmp_path):
    async def failing_run(**kwargs):
        raise RuntimeError("omnigent binary not found")

    monkeypatch.setattr(bot_module.runner, "run", failing_run)

    context = FakeContext()
    sessions = SessionStateStore(tmp_path / "state.json")

    asyncio.run(
        bot_module._run_task(
            context,
            chat_id=123,
            config=FakeConfig(),
            project=FakeProject(),
            task_text="do it",
            sessions=sessions,
        )
    )

    # The chat must be notified instead of the exception being swallowed by
    # the fire-and-forget task created in handle_task_message.
    assert len(context.bot.sent_messages) == 1
    chat_id, text = context.bot.sent_messages[0]
    assert chat_id == 123
    assert "Ошибка" in text
    assert "omnigent binary not found" in text


def test_run_task_stays_silent_on_task_already_running(monkeypatch, tmp_path):
    async def already_running(**kwargs):
        raise TaskAlreadyRunningError()

    monkeypatch.setattr(bot_module.runner, "run", already_running)

    context = FakeContext()
    sessions = SessionStateStore(tmp_path / "state.json")

    asyncio.run(
        bot_module._run_task(
            context,
            chat_id=123,
            config=FakeConfig(),
            project=FakeProject(),
            task_text="do it",
            sessions=sessions,
        )
    )

    # Existing behavior is preserved: this race is expected and silent.
    assert context.bot.sent_messages == []


def test_run_task_passes_stored_resume_id_to_runner(monkeypatch, tmp_path):
    sessions = SessionStateStore(tmp_path / "state.json")
    sessions.set(123, "prev-session-id")

    seen_kwargs = {}

    async def fake_run(**kwargs):
        seen_kwargs.update(kwargs)
        return FakeRunResult(returncode=0, stdout_text="ok")

    monkeypatch.setattr(bot_module.runner, "run", fake_run)

    context = FakeContext()

    asyncio.run(
        bot_module._run_task(
            context,
            chat_id=123,
            config=FakeConfig(),
            project=FakeProject(),
            task_text="continue please",
            sessions=sessions,
        )
    )

    assert seen_kwargs["resume_session_id"] == "prev-session-id"


def test_run_task_resume_failure_resets_session_and_notifies(monkeypatch, tmp_path):
    sessions = SessionStateStore(tmp_path / "state.json")
    sessions.set(123, "stale-session-id")

    async def fake_run(**kwargs):
        return FakeRunResult(
            returncode=1,
            stderr_text="session not found",
            timed_out=False,
            cancelled=False,
        )

    monkeypatch.setattr(bot_module.runner, "run", fake_run)

    context = FakeContext()

    asyncio.run(
        bot_module._run_task(
            context,
            chat_id=123,
            config=FakeConfig(),
            project=FakeProject(),
            task_text="continue please",
            sessions=sessions,
        )
    )

    # The stored session was cleared instead of left pointing at a dead session.
    assert sessions.get(123) is None

    # A single dedicated "session reset" message went out, not the generic
    # "Ошибка выполнения (код ...)" error branch.
    assert len(context.bot.sent_messages) == 1
    chat_id, text = context.bot.sent_messages[0]
    assert chat_id == 123
    assert "Ошибка выполнения (код" not in text
    assert "сброшена" in text


def test_run_task_plain_failure_without_resume_reports_normal_error(monkeypatch, tmp_path):
    sessions = SessionStateStore(tmp_path / "state.json")
    # No stored session for this chat: resume_id is None.

    async def fake_run(**kwargs):
        return FakeRunResult(
            returncode=1,
            stderr_text="boom",
            timed_out=False,
            cancelled=False,
        )

    monkeypatch.setattr(bot_module.runner, "run", fake_run)

    context = FakeContext()

    asyncio.run(
        bot_module._run_task(
            context,
            chat_id=456,
            config=FakeConfig(),
            project=FakeProject(),
            task_text="do it",
            sessions=sessions,
        )
    )

    assert sessions.get(456) is None
    assert len(context.bot.sent_messages) == 1
    chat_id, text = context.bot.sent_messages[0]
    assert chat_id == 456
    assert "Ошибка выполнения (код 1)" in text
    assert "сброшена" not in text


def test_run_task_on_session_url_none_clears_session_and_notifies(monkeypatch, tmp_path):
    sessions = SessionStateStore(tmp_path / "state.json")
    sessions.set(123, "old-session-id")

    async def fake_run(**kwargs):
        # Simulate the runner scanning stdout and never finding a session
        # URL line: it invokes the callback with None before returning.
        await kwargs["on_session_url"](None)
        return FakeRunResult(returncode=0, stdout_text="ok")

    monkeypatch.setattr(bot_module.runner, "run", fake_run)

    context = FakeContext()

    asyncio.run(
        bot_module._run_task(
            context,
            chat_id=123,
            config=FakeConfig(),
            project=FakeProject(),
            task_text="do it",
            sessions=sessions,
        )
    )

    # A previously stored session for this chat must not be left pointing at
    # a session we no longer have a URL for.
    assert sessions.get(123) is None

    # The chat is told the link is unavailable, not the old generic
    # "Задача запущена." message.
    session_url_messages = [
        text for _, text in context.bot.sent_messages if "Задача запущена." == text
    ]
    assert session_url_messages == []
    assert any("ссылка на сессию" in text for _, text in context.bot.sent_messages)


def test_new_command_clears_session_and_confirms(tmp_path):
    sessions = SessionStateStore(tmp_path / "state.json")
    sessions.set(789, "some-session-id")

    context = FakeContext()
    context.bot_data["sessions"] = sessions

    class FakeChat:
        id = 789

        def __init__(self):
            self.sent = []

        async def send_message(self, text):
            self.sent.append(text)

    class FakeUpdate:
        effective_chat = FakeChat()

    update = FakeUpdate()

    asyncio.run(
        bot_module.new_command.__wrapped__(update, context, project=FakeProject())
    )

    assert sessions.get(789) is None
    assert len(update.effective_chat.sent) == 1
    assert "сброшена" in update.effective_chat.sent[0]


def test_run_task_persists_session_and_survives_later_unrelated_failure(monkeypatch, tmp_path):
    """End-to-end regression test for the reported bug: state.json ends up
    losing a session_id that was legitimately extracted from stderr and
    already shown to the user in the chat.

    Exercises the real `OmnigentRunner.run` (only asyncio.create_subprocess_exec
    is faked, as in test_runner.py) together with the real `_run_task` and a
    real `SessionStateStore` backed by a file on disk, end to end:

    1. A fresh task run: `omnigent run` prints a session URL on stderr, which
       is extracted and persisted to state.json.
    2. A fresh SessionStateStore instance (simulating a new bot process)
       reads the same session_id back from disk.
    3. A follow-up run resumes that session (--resume is passed through).
       During the run, on_session_url fires again with a (new) session URL —
       confirming the resume worked and reporting it to the chat — but the
       subprocess then exits with a nonzero code for an unrelated reason.
       The just-confirmed session_id must NOT be wiped back out: on
       unpatched main, `report_resume_failed()` runs unconditionally on any
       nonzero returncode while resume_id is set, clearing the session that
       on_session_url only just saved.
    4. A third run picks up that surviving session_id via `sessions.get()`
       and threads it into the next `runner.run(resume_session_id=...)`
       call, proving the persisted value is actually used, not just present
       on disk.
    """
    state_path = tmp_path / "state.json"
    sessions = SessionStateStore(state_path)

    processes = [
        FakeSubprocess(
            stdout_lines=[b"first answer\n"],
            stderr_lines=[b"Omnigent session: http://127.0.0.1:8000/c/session-a\n"],
            returncode=0,
        ),
        FakeSubprocess(
            stdout_lines=[],
            stderr_lines=[b"Omnigent session: http://127.0.0.1:8000/c/session-b\n"],
            returncode=1,  # unrelated failure, e.g. the agent's task itself errored
        ),
        FakeSubprocess(
            stdout_lines=[b"third answer\n"],
            stderr_lines=[b"Omnigent session: http://127.0.0.1:8000/c/session-b\n"],
            returncode=0,
        ),
    ]
    calls = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return processes[len(calls) - 1]

    monkeypatch.setattr(
        runner_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    context = FakeContext()

    # --- Run 1: fresh task, no prior session for this chat.
    asyncio.run(
        bot_module._run_task(
            context,
            chat_id=123,
            config=FakeConfig(),
            project=FakeProject(),
            task_text="first task",
            sessions=sessions,
        )
    )

    assert calls[0]["args"] == ("omnigent", "run", "/bundle", "-p", "first task")

    # (a)+(b): the session URL from stderr was extracted and persisted.
    # Read it back via a brand-new store pointed at the same file, simulating
    # a fresh bot process rather than trusting in-memory state.
    fresh_store = SessionStateStore(state_path)
    assert fresh_store.get(123) == "session-a"

    # --- Run 2: resumes session-a. A new session URL is confirmed on stderr
    # (resume succeeded) but the run then fails for an unrelated reason.
    asyncio.run(
        bot_module._run_task(
            context,
            chat_id=123,
            config=FakeConfig(),
            project=FakeProject(),
            task_text="second task",
            sessions=sessions,
        )
    )

    assert calls[1]["args"] == (
        "omnigent",
        "run",
        "/bundle",
        "-p",
        "second task",
        "--resume",
        "session-a",
    )

    # The chat was told the (new) session was accepted...
    assert any(
        "Сессия принята" in text and "session-b" in text
        for _, text in context.bot.sent_messages
    )
    # ...and that confirmed session_id must survive the later unrelated
    # failure: it must not be reset back to nothing.
    fresh_store = SessionStateStore(state_path)
    assert fresh_store.get(123) == "session-b"

    # --- Run 3: the next _run_task invocation must read the surviving
    # session_id via sessions.get() and thread it into runner.run() as
    # resume_session_id.
    asyncio.run(
        bot_module._run_task(
            context,
            chat_id=123,
            config=FakeConfig(),
            project=FakeProject(),
            task_text="third task",
            sessions=SessionStateStore(state_path),
        )
    )

    assert calls[2]["args"] == (
        "omnigent",
        "run",
        "/bundle",
        "-p",
        "third task",
        "--resume",
        "session-b",
    )


def test_httpx_logger_is_set_to_warning():
    # httpx logs the full request URL at INFO, and Telegram Bot API URLs
    # embed the bot token: importing bot.py must silence it to avoid
    # leaking the token into logs.
    assert logging.getLogger("httpx").level == logging.WARNING
