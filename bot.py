"""omnigent-telegram: a Telegram remote control for the Omnigent orchestrator.

Entry point and update handlers. One Telegram chat maps to exactly one
configured project; messages from unknown chats or unauthorized users are
ignored silently.
"""

from __future__ import annotations

import logging
from functools import wraps
from typing import Awaitable, Callable, Optional

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import Config, ProjectConfig, get_bot_token, load_config
from runner import OmnigentRunner, TaskAlreadyRunningError, split_message, tail_text

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# httpx logs each request at INFO, including the full URL — which for the
# Telegram Bot API embeds the bot token. Silence it to avoid leaking the
# token into logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("omnigent_telegram_bot")

runner = OmnigentRunner()

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE, ProjectConfig], Awaitable[None]]


def _project_for_update(update: Update, config: Config) -> Optional[ProjectConfig]:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        return None

    project = config.project_for_chat(chat.id)
    if project is None or not project.is_allowed(user.id):
        return None

    return project


def authorized(handler: Handler) -> Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]:
    """Only invoke ``handler`` for chats/users present in the config.

    Anything else is ignored without a reply, so the bot's existence is not
    confirmed to strangers.
    """

    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        config: Config = context.bot_data["config"]
        project = _project_for_update(update, config)
        if project is None:
            return
        await handler(update, context, project)

    return wrapper


@authorized
async def start_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, project: ProjectConfig
) -> None:
    await update.effective_chat.send_message(
        "omnigent-telegram\n"
        f"Проект этого чата: {project.name}\n\n"
        "Отправьте текст задачи обычным сообщением, чтобы запустить оркестратор.\n"
        "/status — статус текущей задачи.\n"
        "/cancel — прервать текущую задачу."
    )


@authorized
async def status_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, project: ProjectConfig
) -> None:
    chat_id = update.effective_chat.id
    if runner.is_busy(chat_id):
        url = runner.get_session_url(chat_id)
        text = f"Задача выполняется.\nСессия: {url}" if url else "Задача выполняется, сессия ещё не назначена."
    else:
        text = "Нет активной задачи."
    await update.effective_chat.send_message(text)


@authorized
async def cancel_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, project: ProjectConfig
) -> None:
    chat_id = update.effective_chat.id
    cancelled = await runner.cancel(chat_id)
    if cancelled:
        await update.effective_chat.send_message("Задача прервана.")
    else:
        await update.effective_chat.send_message("Нет активной задачи для отмены.")


@authorized
async def handle_task_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE, project: ProjectConfig
) -> None:
    chat_id = update.effective_chat.id
    task_text = update.effective_message.text
    if not task_text:
        return

    if runner.is_busy(chat_id):
        url = runner.get_session_url(chat_id)
        suffix = f" Сессия: {url}" if url else ""
        await update.effective_chat.send_message(f"Занят: задача уже выполняется.{suffix}")
        return

    config: Config = context.bot_data["config"]
    # Fire-and-forget: the handler must return immediately so the bot keeps
    # answering /status and /cancel (in this and other chats) while the
    # orchestrator subprocess runs.
    context.application.create_task(
        _run_task(context, chat_id, config, project, task_text)
    )


async def _run_task(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    config: Config,
    project: ProjectConfig,
    task_text: str,
) -> None:
    async def on_session_url(url: Optional[str]) -> None:
        if url:
            await context.bot.send_message(chat_id=chat_id, text=f"Сессия принята: {url}")
        else:
            await context.bot.send_message(chat_id=chat_id, text="Задача запущена.")

    try:
        result = await runner.run(
            chat_id=chat_id,
            bundle_path=config.bundle_path,
            workdir=project.workdir,
            task_text=task_text,
            timeout_seconds=config.timeout_seconds,
            on_session_url=on_session_url,
        )
    except TaskAlreadyRunningError:
        # Shouldn't normally happen (handle_task_message already checks),
        # but guards against a race without ever queuing or running in parallel.
        return
    except Exception as exc:
        # Anything else (subprocess failed to start, unexpected bug, ...)
        # must still reach the chat per SPEC.md #6 instead of vanishing into
        # the fire-and-forget task created by handle_task_message.
        logger.exception("task crashed chat_id=%s", chat_id)
        await context.bot.send_message(
            chat_id=chat_id, text=f"Ошибка выполнения задачи: {exc}"
        )
        return

    if result.timed_out:
        suffix = f"\nСессия: {result.session_url}" if result.session_url else ""
        await context.bot.send_message(
            chat_id=chat_id, text=f"Таймаут выполнения задачи.{suffix}"
        )
        return

    if result.cancelled:
        await context.bot.send_message(chat_id=chat_id, text="Задача прервана.")
        return

    if result.returncode != 0:
        error_text = tail_text(result.stderr_text.strip() or "(нет вывода в stderr)")
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Ошибка выполнения (код {result.returncode}):\n{error_text}",
        )
        return

    output = result.stdout_text.strip("\n") or "(пустой вывод)"
    for chunk in split_message(output):
        await context.bot.send_message(chat_id=chat_id, text=chunk)


def build_application(config: Config, token: str) -> Application:
    application = Application.builder().token(token).build()
    application.bot_data["config"] = config
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_task_message))
    return application


def main() -> None:
    config = load_config()
    token = get_bot_token()
    application = build_application(config, token)
    logger.info("Starting omnigent-telegram bot with %d project(s)", len(config.projects))
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
