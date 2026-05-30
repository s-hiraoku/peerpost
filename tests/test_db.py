from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from peerpost.db import Store, connect


class DbTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_home = os.environ.get("PEERPOST_HOME")
        self.old_socket = os.environ.get("PEERPOST_SOCKET")
        os.environ["PEERPOST_HOME"] = self.tmp.name
        os.environ["PEERPOST_SOCKET"] = str(Path(self.tmp.name) / "peerpost.sock")
        self.addCleanup(self._restore_env)
        self.store = Store(connect())
        self.addCleanup(self.store.close)

    def _restore_env(self) -> None:
        if self.old_home is None:
            os.environ.pop("PEERPOST_HOME", None)
        else:
            os.environ["PEERPOST_HOME"] = self.old_home
        if self.old_socket is None:
            os.environ.pop("PEERPOST_SOCKET", None)
        else:
            os.environ["PEERPOST_SOCKET"] = self.old_socket

    def test_migration_creates_expected_tables(self) -> None:
        rows = self.store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        self.assertGreaterEqual({"meta", "agents", "messages", "deliveries"}, {row["name"] for row in rows})
        version = self.store.conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        self.assertEqual(version["value"], "1")

    def test_join_registers_agent(self) -> None:
        agent = self.store.join_agent("codex", "codex", "dev", "/tmp/work")
        self.assertEqual(agent["id"], "codex")
        self.assertEqual(agent["team"], "dev")
        self.assertEqual(agent["workspace"], "/tmp/work")

    def test_send_creates_message_and_delivery(self) -> None:
        message, targets = self.store.create_message("dev", "claude", "hello", ["codex"])
        self.assertEqual(targets, ["codex"])
        status = self.store.delivery_status(message["id"], "codex", "dev")
        self.assertEqual(status, "pending")

    def test_drain_returns_pending_and_marks_delivered(self) -> None:
        message, _ = self.store.create_message("dev", "claude", "hello", ["codex"])
        drained = self.store.drain("codex", "dev")
        self.assertEqual([item.id for item in drained], [message["id"]])
        self.assertEqual(self.store.delivery_status(message["id"], "codex", "dev"), "delivered")

    def test_second_drain_does_not_return_delivered_messages(self) -> None:
        self.store.create_message("dev", "claude", "hello", ["codex"])
        self.store.drain("codex", "dev")
        self.assertEqual(self.store.drain("codex", "dev"), [])

    def test_ack_changes_status_to_acknowledged(self) -> None:
        message, _ = self.store.create_message("dev", "claude", "hello", ["codex"])
        self.assertEqual(self.store.ack([message["id"]], "codex", "dev"), 1)
        self.assertEqual(self.store.delivery_status(message["id"], "codex", "dev"), "acknowledged")

    def test_done_changes_status_to_done(self) -> None:
        message, _ = self.store.create_message("dev", "claude", "hello", ["codex"])
        self.assertEqual(self.store.done([message["id"]], "codex", "dev"), 1)
        self.assertEqual(self.store.delivery_status(message["id"], "codex", "dev"), "done")

    def test_broadcast_targets_all_agents_except_sender(self) -> None:
        self.store.join_agent("claude", "claude-code", "dev")
        self.store.join_agent("codex", "codex", "dev")
        self.store.join_agent("copilot", "copilot", "dev")
        targets = self.store.broadcast_targets("dev", "claude")
        message, deliveries = self.store.create_message("dev", "claude", "hello all", targets)
        self.assertEqual(deliveries, ["codex", "copilot"])
        self.assertEqual(self.store.delivery_status(message["id"], "codex", "dev"), "pending")
        self.assertEqual(self.store.delivery_status(message["id"], "copilot", "dev"), "pending")


if __name__ == "__main__":
    unittest.main()
