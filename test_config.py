"""Tests for config.py: no network, no token, no subprocess involved."""

from __future__ import annotations

import pytest

from config import ConfigError, load_config, get_bot_token

VALID_YAML = """
bundle_path: /home/deploy/omnigent-agents/orchestrator
timeout_seconds: 1800

projects:
  - name: orchestrator
    chat_id: -1001234567890
    workdir: /home/deploy/omnigent-workspaces/orchestrator-repo
    allowed_user_ids: [123456789]
  - name: second-project
    chat_id: 42
    workdir: /home/deploy/omnigent-workspaces/second-repo
    allowed_user_ids: [1, 2, 3]
"""


def _write(tmp_path, content: str):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(content, encoding="utf-8")
    return config_path


def test_load_valid_config(tmp_path):
    path = _write(tmp_path, VALID_YAML)

    config = load_config(path)

    assert config.bundle_path == "/home/deploy/omnigent-agents/orchestrator"
    assert config.timeout_seconds == 1800
    assert len(config.projects) == 2

    project = config.project_for_chat(-1001234567890)
    assert project is not None
    assert project.name == "orchestrator"
    assert project.workdir == "/home/deploy/omnigent-workspaces/orchestrator-repo"
    assert project.allowed_user_ids == (123456789,)
    assert project.is_allowed(123456789)
    assert not project.is_allowed(999)

    assert config.project_for_chat(999999) is None


def test_load_config_uses_env_var_path(tmp_path, monkeypatch):
    path = _write(tmp_path, VALID_YAML)
    monkeypatch.setenv("OMNIGENT_TG_CONFIG", str(path))

    config = load_config()

    assert config.bundle_path == "/home/deploy/omnigent-agents/orchestrator"


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "does-not-exist.yaml")


@pytest.mark.parametrize(
    "content",
    [
        # missing bundle_path
        """
timeout_seconds: 1800
projects:
  - name: orchestrator
    chat_id: 1
    workdir: /tmp/x
    allowed_user_ids: [1]
""",
        # missing timeout_seconds
        """
bundle_path: /tmp/bundle
projects:
  - name: orchestrator
    chat_id: 1
    workdir: /tmp/x
    allowed_user_ids: [1]
""",
        # missing projects
        """
bundle_path: /tmp/bundle
timeout_seconds: 1800
""",
        # empty projects list
        """
bundle_path: /tmp/bundle
timeout_seconds: 1800
projects: []
""",
        # project missing chat_id
        """
bundle_path: /tmp/bundle
timeout_seconds: 1800
projects:
  - name: orchestrator
    workdir: /tmp/x
    allowed_user_ids: [1]
""",
        # project missing allowed_user_ids
        """
bundle_path: /tmp/bundle
timeout_seconds: 1800
projects:
  - name: orchestrator
    chat_id: 1
    workdir: /tmp/x
""",
        # project with empty allowed_user_ids
        """
bundle_path: /tmp/bundle
timeout_seconds: 1800
projects:
  - name: orchestrator
    chat_id: 1
    workdir: /tmp/x
    allowed_user_ids: []
""",
        # non-positive timeout
        """
bundle_path: /tmp/bundle
timeout_seconds: 0
projects:
  - name: orchestrator
    chat_id: 1
    workdir: /tmp/x
    allowed_user_ids: [1]
""",
    ],
)
def test_invalid_config_raises(tmp_path, content):
    path = _write(tmp_path, content)
    with pytest.raises(ConfigError):
        load_config(path)


def test_duplicate_chat_id_raises(tmp_path):
    content = """
bundle_path: /tmp/bundle
timeout_seconds: 1800
projects:
  - name: one
    chat_id: 1
    workdir: /tmp/one
    allowed_user_ids: [1]
  - name: two
    chat_id: 1
    workdir: /tmp/two
    allowed_user_ids: [2]
"""
    path = _write(tmp_path, content)
    with pytest.raises(ConfigError):
        load_config(path)


def test_get_bot_token_from_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token-value")
    assert get_bot_token() == "test-token-value"


def test_get_bot_token_missing_raises(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(ConfigError):
        get_bot_token()
