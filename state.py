"""Persistent per-chat omnigent session state.

Stores a single JSON object mapping ``chat_id`` (as a string) to the last
known omnigent session id for that chat, so a follow-up message in the same
chat can resume the previous session instead of starting from scratch.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Optional, Union


class SessionStateStore:
    """Reads/writes a JSON file of ``{chat_id: session_id}``.

    Every write is atomic: the new content is written to a temporary file in
    the same directory as the target, then moved into place with
    ``os.replace()``. This means the process can be killed at any point
    without ever leaving the state file half-written or corrupted.
    """

    def __init__(self, path: Union[str, os.PathLike]) -> None:
        self._path = Path(path)

    def _read_all(self) -> dict[str, str]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

        if not isinstance(data, dict):
            return {}

        return data

    def _write_all(self, data: dict[str, str]) -> None:
        directory = self._path.parent
        directory.mkdir(parents=True, exist_ok=True)

        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{self._path.name}.", suffix=".tmp", dir=directory
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp_path, self._path)
        except BaseException:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

    def get(self, chat_id: int) -> Optional[str]:
        return self._read_all().get(str(chat_id))

    def set(self, chat_id: int, session_id: str) -> None:
        data = self._read_all()
        data[str(chat_id)] = session_id
        self._write_all(data)

    def clear(self, chat_id: int) -> None:
        data = self._read_all()
        if str(chat_id) in data:
            del data[str(chat_id)]
            self._write_all(data)
