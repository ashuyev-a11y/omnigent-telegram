"""Loading and validation of config.yaml for omnigent-telegram."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

import yaml

DEFAULT_CONFIG_PATH = "./config.yaml"
CONFIG_PATH_ENV_VAR = "OMNIGENT_TG_CONFIG"
BOT_TOKEN_ENV_VAR = "TELEGRAM_BOT_TOKEN"
DEFAULT_STATE_FILE = "./state.json"


class ConfigError(Exception):
    """Raised when config.yaml (or the environment) is missing or invalid."""


@dataclass(frozen=True)
class ProjectConfig:
    name: str
    chat_id: int
    workdir: str
    allowed_user_ids: tuple[int, ...]

    def is_allowed(self, user_id: int) -> bool:
        return user_id in self.allowed_user_ids


@dataclass(frozen=True)
class Config:
    bundle_path: str
    timeout_seconds: int
    projects: tuple[ProjectConfig, ...]
    state_file: str = DEFAULT_STATE_FILE

    def project_for_chat(self, chat_id: int) -> Optional[ProjectConfig]:
        for project in self.projects:
            if project.chat_id == chat_id:
                return project
        return None


def _require(data: dict, key: str, context: str):
    if key not in data or data[key] is None:
        raise ConfigError(f"Missing required field '{key}' in {context}")
    return data[key]


def _parse_project(raw: object, index: int) -> ProjectConfig:
    context = f"projects[{index}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{context} must be a mapping")

    name = _require(raw, "name", context)
    raw_chat_id = _require(raw, "chat_id", context)
    workdir = _require(raw, "workdir", context)
    raw_allowed_user_ids = _require(raw, "allowed_user_ids", context)

    if not isinstance(raw_allowed_user_ids, list) or not raw_allowed_user_ids:
        raise ConfigError(f"{context}.allowed_user_ids must be a non-empty list")

    try:
        chat_id = int(raw_chat_id)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{context}.chat_id must be an integer") from exc

    try:
        allowed_user_ids = tuple(int(uid) for uid in raw_allowed_user_ids)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{context}.allowed_user_ids must contain integers") from exc

    return ProjectConfig(
        name=str(name),
        chat_id=chat_id,
        workdir=str(workdir),
        allowed_user_ids=allowed_user_ids,
    )


def load_config(path: Union[str, os.PathLike, None] = None) -> Config:
    """Load and validate config.yaml.

    ``path`` defaults to the ``OMNIGENT_TG_CONFIG`` environment variable, and
    then to ``./config.yaml``. Raises :class:`ConfigError` on any structural
    or validation problem (missing fields, duplicate chat ids, ...).
    """
    if path is None:
        path = os.environ.get(CONFIG_PATH_ENV_VAR, DEFAULT_CONFIG_PATH)
    config_path = Path(path)

    if not config_path.is_file():
        raise ConfigError(f"Config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    if not isinstance(raw, dict):
        raise ConfigError("Top-level config must be a mapping")

    bundle_path = _require(raw, "bundle_path", "config")
    raw_timeout = _require(raw, "timeout_seconds", "config")

    try:
        timeout_seconds = int(raw_timeout)
    except (TypeError, ValueError) as exc:
        raise ConfigError("timeout_seconds must be an integer") from exc
    if timeout_seconds <= 0:
        raise ConfigError("timeout_seconds must be a positive number of seconds")

    raw_projects = _require(raw, "projects", "config")
    if not isinstance(raw_projects, list) or not raw_projects:
        raise ConfigError("projects must be a non-empty list")

    projects = [_parse_project(item, i) for i, item in enumerate(raw_projects)]

    seen_chat_ids: set[int] = set()
    for project in projects:
        if project.chat_id in seen_chat_ids:
            raise ConfigError(f"Duplicate chat_id in config: {project.chat_id}")
        seen_chat_ids.add(project.chat_id)

    raw_state_file = raw.get("state_file")
    if raw_state_file is None:
        state_file = DEFAULT_STATE_FILE
    else:
        if not isinstance(raw_state_file, str) or not raw_state_file:
            raise ConfigError("state_file must be a non-empty string")
        state_file = raw_state_file

    return Config(
        bundle_path=str(bundle_path),
        timeout_seconds=timeout_seconds,
        projects=tuple(projects),
        state_file=state_file,
    )


def get_bot_token() -> str:
    """Return the bot token from the TELEGRAM_BOT_TOKEN environment variable.

    Never reads the token from config.yaml or command-line arguments.
    """
    token = os.environ.get(BOT_TOKEN_ENV_VAR)
    if not token:
        raise ConfigError(f"{BOT_TOKEN_ENV_VAR} environment variable is not set")
    return token
