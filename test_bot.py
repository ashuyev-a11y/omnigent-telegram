"""Tests for bot.py: `_run_task` error handling per SPEC.md item 6.

No Telegram network access and no real subprocess involved: `runner.run` is
monkeypatched directly, and Telegram's `Bot.send_message` is replaced with an
in-memory recorder.
"""

from __future__ import annotations

import asyncio
import importlib
import logging

import bot as bot_module
from runner import TaskAlreadyRunningError


class FakeBot:
    def __init__(self):
        self.sent_messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text):
        self.sent_messages.append((chat_id, text))


class FakeContext:
    def __init__(self):
        self.bot = FakeBot()


class FakeProject:
    workdir = "/workdir"


class FakeConfig:
    bundle_path = "/bundle"
    timeout_seconds = 5


def test_run_task_reports_unexpected_exception_to_chat(monkeypatch):
    async def failing_run(**kwargs):
        raise RuntimeError("omnigent binary not found")

    monkeypatch.setattr(bot_module.runner, "run", failing_run)

    context = FakeContext()

    asyncio.run(
        bot_module._run_task(
            context,
            chat_id=123,
            config=FakeConfig(),
            project=FakeProject(),
            task_text="do it",
        )
    )

    # The chat must be notified instead of the exception being swallowed by
    # the fire-and-forget task created in handle_task_message.
    assert len(context.bot.sent_messages) == 1
    chat_id, text = context.bot.sent_messages[0]
    assert chat_id == 123
    assert "Ошибка" in text
    assert "omnigent binary not found" in text


def test_run_task_stays_silent_on_task_already_running(monkeypatch):
    async def already_running(**kwargs):
        raise TaskAlreadyRunningError()

    monkeypatch.setattr(bot_module.runner, "run", already_running)

    context = FakeContext()

    asyncio.run(
        bot_module._run_task(
            context,
            chat_id=123,
            config=FakeConfig(),
            project=FakeProject(),
            task_text="do it",
        )
    )

    # Existing behavior is preserved: this race is expected and silent.
    assert context.bot.sent_messages == []


def test_module_import_silences_httpx_info_logs():
    # httpx logs each request at INFO, including the full request URL, which
    # for the Telegram Bot API embeds the bot token. Reset the logger to a
    # level that would leak that URL, then reload bot.py and confirm its
    # module-level setup raises it back to WARNING.
    httpx_logger = logging.getLogger("httpx")
    httpx_logger.setLevel(logging.INFO)
    assert httpx_logger.getEffectiveLevel() == logging.INFO

    importlib.reload(bot_module)

    assert httpx_logger.level == logging.WARNING
