"""Tests for state.py: no network, no subprocess involved.

Everything happens on tmp_path, so tests can freely check the actual bytes
written to disk (round-trip, atomicity) rather than trusting the docstring.
"""

from __future__ import annotations

import logging
import os

from state import SessionStateStore


def test_get_on_missing_file_returns_none(tmp_path):
    store = SessionStateStore(tmp_path / "state.json")
    assert store.get(1) is None


def test_set_then_get_round_trips_in_same_instance(tmp_path):
    store = SessionStateStore(tmp_path / "state.json")
    store.set(42, "session-abc")
    assert store.get(42) == "session-abc"


def test_set_persists_to_disk_for_a_new_instance(tmp_path):
    path = tmp_path / "state.json"
    store = SessionStateStore(path)
    store.set(42, "session-abc")

    # A fresh instance pointed at the same file must see the same value,
    # proving the write actually reached disk rather than staying in memory.
    other = SessionStateStore(path)
    assert other.get(42) == "session-abc"


def test_corrupted_file_is_treated_as_empty_state(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not valid json", encoding="utf-8")

    store = SessionStateStore(path)

    # Must not raise, and must behave as if there is no stored session.
    assert store.get(1) is None


def test_clear_removes_only_its_own_chat_id(tmp_path):
    path = tmp_path / "state.json"
    store = SessionStateStore(path)
    store.set(1, "session-one")
    store.set(2, "session-two")

    store.clear(1)

    assert store.get(1) is None
    assert store.get(2) == "session-two"

    # Persisted correctly too, not just in memory.
    other = SessionStateStore(path)
    assert other.get(1) is None
    assert other.get(2) == "session-two"


def test_clear_on_missing_chat_id_does_not_raise(tmp_path):
    store = SessionStateStore(tmp_path / "state.json")
    store.clear(999)  # no-op, must not raise
    assert store.get(999) is None


def test_set_logs_save_with_chat_id_and_session_id(tmp_path, caplog):
    store = SessionStateStore(tmp_path / "state.json")

    with caplog.at_level(logging.INFO, logger="state"):
        store.set(42, "session-abc")

    assert any(
        record.levelno == logging.INFO
        and "42" in record.getMessage()
        and "session-abc" in record.getMessage()
        for record in caplog.records
    )


def test_clear_logs_reset_with_chat_id(tmp_path, caplog):
    store = SessionStateStore(tmp_path / "state.json")
    store.set(1, "session-one")
    caplog.clear()

    with caplog.at_level(logging.INFO, logger="state"):
        store.clear(1)

    assert any(
        record.levelno == logging.INFO and "1" in record.getMessage()
        for record in caplog.records
    )


def test_clear_on_missing_chat_id_does_not_log(tmp_path, caplog):
    store = SessionStateStore(tmp_path / "state.json")

    with caplog.at_level(logging.INFO, logger="state"):
        store.clear(999)  # no-op: nothing was stored for this chat_id

    assert caplog.records == []


def test_write_is_atomic_write_then_rename(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    store = SessionStateStore(path)

    replace_calls = []
    real_replace = os.replace

    def spy_replace(src, dst):
        # At the moment os.replace() is called, the temp file must already
        # hold the fully-written new content, and it must be a different
        # path from the target (proving we wrote to a temp file first
        # instead of truncating the target file directly).
        assert src != str(dst) and src != dst
        with open(src, "r", encoding="utf-8") as fh:
            content = fh.read()
        assert "session-abc" in content
        replace_calls.append((src, dst))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy_replace)

    store.set(1, "session-abc")

    assert len(replace_calls) == 1
    # The target file exists with final content only after the rename.
    assert path.read_text(encoding="utf-8").find("session-abc") != -1
    # No leftover temp files.
    leftovers = [p for p in tmp_path.iterdir() if p.name != "state.json"]
    assert leftovers == []
