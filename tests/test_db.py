from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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

    def test_leave_unregisters_agent(self) -> None:
        self.store.join_agent("codex", "codex", "dev")
        self.assertTrue(self.store.leave_agent("codex", "dev"))
        self.assertIsNone(self.store.get_agent("codex", "dev"))
        self.assertFalse(self.store.leave_agent("codex", "dev"))

    def test_send_creates_message_and_delivery(self) -> None:
        message, targets = self.store.create_message("dev", "claude", "hello", ["codex"])
        self.assertEqual(targets, ["codex"])
        status = self.store.delivery_status(message["id"], "codex", "dev")
        self.assertEqual(status, "pending")

    def test_concurrent_sends_are_serialized(self) -> None:
        def send(index: int) -> str:
            message, _ = self.store.create_message("dev", f"agent-{index}", "hello", ["codex"])
            return message["id"]

        with ThreadPoolExecutor(max_workers=8) as executor:
            message_ids = list(executor.map(send, range(50)))

        self.assertEqual(len(set(message_ids)), 50)
        drained = self.store.drain("codex", "dev", limit=100)
        self.assertEqual(len(drained), 50)

    def test_send_stores_message_metadata(self) -> None:
        message, _ = self.store.create_message(
            "dev",
            "claude",
            "please check",
            ["codex"],
            kind="review",
            priority="high",
            parent_id="msg_parent",
        )
        drained = self.store.drain("codex", "dev")
        self.assertEqual(drained[0].id, message["id"])
        self.assertEqual(drained[0].kind, "review")
        self.assertEqual(drained[0].priority, "high")
        self.assertEqual(drained[0].parent_id, "msg_parent")

    def test_send_rejects_unknown_priority(self) -> None:
        with self.assertRaisesRegex(ValueError, "priority must be one of"):
            self.store.create_message(
                "dev",
                "claude",
                "please check",
                ["codex"],
                priority="later",
            )

    def test_send_rejects_empty_delivery_targets(self) -> None:
        with self.assertRaisesRegex(ValueError, "message must have at least one delivery target"):
            self.store.create_message("dev", "claude", "self note", ["claude"])
        with self.assertRaisesRegex(ValueError, "message must have at least one delivery target"):
            self.store.create_message("dev", "claude", "empty", [])

    def test_get_message_for_agent_does_not_mark_delivered(self) -> None:
        message, _ = self.store.create_message("dev", "claude", "hello", ["codex"])
        found = self.store.get_message_for_agent(message["id"], "codex", "dev")
        self.assertIsNotNone(found)
        self.assertEqual(found.id, message["id"])
        self.assertEqual(self.store.delivery_status(message["id"], "codex", "dev"), "pending")

    def test_matching_message_ids_supports_agent_scoped_prefixes(self) -> None:
        message, _ = self.store.create_message("dev", "claude", "hello", ["codex"])
        self.store.create_message("dev", "claude", "other", ["copilot"])

        self.assertEqual(
            self.store.matching_message_ids("dev", message["id"][:-2], agent_id="codex"),
            [message["id"]],
        )
        self.assertEqual(self.store.matching_message_ids("dev", "msg_", agent_id="codex"), [message["id"]])

    def test_thread_returns_root_and_descendants(self) -> None:
        root, _ = self.store.create_message("dev", "claude", "root", ["codex"])
        reply, _ = self.store.create_message(
            "dev", "codex", "reply", ["claude"], kind="reply", parent_id=root["id"]
        )
        followup, _ = self.store.create_message(
            "dev", "claude", "followup", ["codex"], kind="reply", parent_id=reply["id"]
        )
        self.store.create_message("dev", "copilot", "unrelated", ["codex"])

        messages = self.store.thread("dev", reply["id"])
        self.assertEqual([message.id for message in messages], [root["id"], reply["id"], followup["id"]])

    def test_drain_returns_pending_and_marks_delivered(self) -> None:
        message, _ = self.store.create_message("dev", "claude", "hello", ["codex"])
        drained = self.store.drain("codex", "dev")
        self.assertEqual([item.id for item in drained], [message["id"]])
        self.assertEqual(self.store.delivery_status(message["id"], "codex", "dev"), "delivered")

    def test_second_drain_does_not_return_delivered_messages(self) -> None:
        self.store.create_message("dev", "claude", "hello", ["codex"])
        self.store.drain("codex", "dev")
        self.assertEqual(self.store.drain("codex", "dev"), [])

    def test_drain_rejects_non_positive_limit(self) -> None:
        with self.assertRaisesRegex(ValueError, "limit must be 1 or greater"):
            self.store.drain("codex", "dev", limit=0)

    def test_ack_changes_status_to_acknowledged(self) -> None:
        message, _ = self.store.create_message("dev", "claude", "hello", ["codex"])
        self.assertEqual(self.store.ack([message["id"]], "codex", "dev"), 1)
        self.assertEqual(self.store.delivery_status(message["id"], "codex", "dev"), "acknowledged")

    def test_done_changes_status_to_done(self) -> None:
        message, _ = self.store.create_message("dev", "claude", "hello", ["codex"])
        self.assertEqual(self.store.done([message["id"]], "codex", "dev"), 1)
        self.assertEqual(self.store.delivery_status(message["id"], "codex", "dev"), "done")

    def test_prune_done_only_deletes_done_messages(self) -> None:
        done_message, _ = self.store.create_message("dev", "claude", "done", ["codex"])
        pending_message, _ = self.store.create_message("dev", "claude", "pending", ["codex"])
        self.store.done([done_message["id"]], "codex", "dev")

        dry_run = self.store.prune_done("9999-01-01T00:00:00Z", team="dev")
        self.assertEqual(dry_run["matched"], 1)
        self.assertEqual(dry_run["deleted"], 0)
        self.assertEqual(dry_run["message_ids"], [done_message["id"]])
        self.assertEqual(self.store.delivery_status(done_message["id"], "codex", "dev"), "done")

        applied = self.store.prune_done("9999-01-01T00:00:00Z", team="dev", apply=True)
        self.assertEqual(applied["matched"], 1)
        self.assertEqual(applied["deleted"], 1)
        self.assertIsNone(self.store.delivery_status(done_message["id"], "codex", "dev"))
        self.assertEqual(self.store.delivery_status(pending_message["id"], "codex", "dev"), "pending")

    def test_prune_rejects_non_positive_limit(self) -> None:
        with self.assertRaisesRegex(ValueError, "limit must be 1 or greater"):
            self.store.prune_done("9999-01-01T00:00:00Z", limit=0)

    def test_backup_creates_readable_sqlite_snapshot(self) -> None:
        message, _ = self.store.create_message("dev", "claude", "backup me", ["codex"])
        backup_path = Path(self.tmp.name) / "backups" / "peerpost-backup.sqlite"

        data = self.store.backup(backup_path)

        self.assertEqual(data["path"], str(backup_path.resolve()))
        self.assertGreater(data["bytes"], 0)
        self.assertTrue(backup_path.exists())
        conn = sqlite3.connect(backup_path)
        try:
            row = conn.execute("SELECT body FROM messages WHERE id = ?", (message["id"],)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], "backup me")
        self.assertEqual(list(backup_path.parent.glob(f".{backup_path.name}.tmp-*")), [])

    def test_backup_does_not_overwrite_without_flag(self) -> None:
        backup_path = Path(self.tmp.name) / "peerpost-backup.sqlite"
        self.store.backup(backup_path)

        with self.assertRaises(FileExistsError):
            self.store.backup(backup_path)

        data = self.store.backup(backup_path, overwrite=True)
        self.assertEqual(data["path"], str(backup_path.resolve()))

    def test_backup_removes_temporary_file_after_failure(self) -> None:
        class FailingConnection:
            def backup(self, _destination: sqlite3.Connection) -> None:
                raise sqlite3.Error("backup failed")

        backup_path = Path(self.tmp.name) / "failed.sqlite"
        store = Store(FailingConnection())  # type: ignore[arg-type]

        with self.assertRaises(sqlite3.Error):
            store.backup(backup_path)

        self.assertFalse(backup_path.exists())
        self.assertEqual(list(backup_path.parent.glob(f".{backup_path.name}.tmp-*")), [])

    def test_self_test_exercises_delivery_path_without_persisting_artifacts(self) -> None:
        before_agents = self.store.conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
        before_messages = self.store.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        before_deliveries = self.store.conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]

        data = self.store.self_test()

        self.assertTrue(data["ok"])
        checks = {check["name"]: check for check in data["checks"]}
        self.assertEqual(checks["agents"]["detail"], "2 registered")
        self.assertEqual(checks["pending"]["detail"], "1 pending")
        self.assertEqual(checks["delivery"]["detail"], "delivered")
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0], before_agents)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], before_messages)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], before_deliveries)

    def test_delivery_health_reports_unregistered_agents(self) -> None:
        self.store.join_agent("codex", "codex", "dev")
        self.store.create_message("dev", "claude", "ok", ["codex"])
        self.store.create_message("dev", "claude", "typo", ["cdoex"])
        self.store.create_message("dev", "claud", "sender typo", ["codex"])

        health = self.store.delivery_health("dev")

        self.assertEqual(health["status_counts"], {"pending": 3})
        self.assertEqual(health["orphan_count"], 1)
        self.assertEqual(health["orphans"][0]["to_agent"], "cdoex")
        self.assertEqual(health["orphans"][0]["pending"], 1)
        self.assertEqual(health["unregistered_sender_count"], 2)
        self.assertEqual(
            [item["from_agent"] for item in health["unregistered_senders"]],
            ["claud", "claude"],
        )

    def test_delivery_health_reports_invalid_priorities(self) -> None:
        message, _ = self.store.create_message("dev", "claude", "ok", ["codex"])
        self.store.conn.execute(
            "UPDATE messages SET priority = ? WHERE id = ?",
            ("later", message["id"]),
        )

        health = self.store.delivery_health("dev")

        self.assertEqual(health["invalid_priority_count"], 1)
        self.assertEqual(health["invalid_priorities"][0]["priority"], "later")
        self.assertEqual(health["invalid_priorities"][0]["total"], 1)

    def test_delivery_health_reports_invalid_delivery_statuses(self) -> None:
        message, _ = self.store.create_message("dev", "claude", "ok", ["codex"])
        self.store.conn.execute(
            "UPDATE deliveries SET status = ? WHERE message_id = ?",
            ("lost", message["id"]),
        )

        health = self.store.delivery_health("dev")

        self.assertEqual(health["invalid_status_count"], 1)
        self.assertEqual(health["invalid_statuses"][0]["status"], "lost")
        self.assertEqual(health["invalid_statuses"][0]["total"], 1)

    def test_delivery_health_reports_messages_without_deliveries(self) -> None:
        self.store.conn.execute(
            """
            INSERT INTO messages
              (id, team, from_agent, body, kind, priority, parent_id, created_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, NULL, ?, '{}')
            """,
            (
                "msg_20260604T010203456Z_orphan",
                "dev",
                "claude",
                "orphan",
                "message",
                "normal",
                "2026-06-04T01:02:03Z",
            ),
        )

        health = self.store.delivery_health("dev")

        self.assertEqual(health["messages_without_delivery_count"], 1)
        self.assertEqual(health["messages_without_deliveries"][0]["team"], "dev")
        self.assertEqual(health["messages_without_deliveries"][0]["total"], 1)

    def test_team_status_reports_agent_delivery_counts(self) -> None:
        self.store.join_agent("claude", "claude-code", "dev")
        self.store.join_agent("codex", "codex", "dev")
        pending_message, _ = self.store.create_message("dev", "claude", "pending", ["codex"])
        delivered_message, _ = self.store.create_message("dev", "claude", "delivered", ["codex"])
        done_message, _ = self.store.create_message("dev", "claude", "done", ["codex"])
        self.store.mark_delivered([delivered_message["id"]], "codex", "dev")
        self.store.done([done_message["id"]], "codex", "dev")
        self.store.create_message("dev", "claude", "typo", ["cdoex"])

        status = self.store.team_status("dev")

        codex = next(agent for agent in status["agents"] if agent["id"] == "codex")
        self.assertEqual(codex["pending"], 1)
        self.assertEqual(codex["delivered"], 1)
        self.assertEqual(codex["done"], 1)
        self.assertEqual(codex["non_done"], 2)
        self.assertEqual(status["totals"], {"delivered": 1, "done": 1, "pending": 2})
        self.assertEqual(status["unregistered"][0]["to_agent"], "cdoex")
        self.assertEqual(status["unregistered"][0]["pending"], 1)
        self.assertEqual(self.store.delivery_status(pending_message["id"], "codex", "dev"), "pending")

    def test_broadcast_targets_all_agents_except_sender(self) -> None:
        self.store.join_agent("claude", "claude-code", "dev")
        self.store.join_agent("codex", "codex", "dev")
        self.store.join_agent("copilot", "copilot", "dev")
        targets = self.store.broadcast_targets("dev", "claude")
        message, deliveries = self.store.create_message("dev", "claude", "hello all", targets)
        self.assertEqual(deliveries, ["codex", "copilot"])
        self.assertEqual(self.store.delivery_status(message["id"], "codex", "dev"), "pending")
        self.assertEqual(self.store.delivery_status(message["id"], "copilot", "dev"), "pending")

    def test_broadcast_excludes_agent_after_leave(self) -> None:
        self.store.join_agent("claude", "claude-code", "dev")
        self.store.join_agent("codex", "codex", "dev")
        self.store.join_agent("copilot", "copilot", "dev")
        self.store.leave_agent("copilot", "dev")
        self.assertEqual(self.store.broadcast_targets("dev", "claude"), ["codex"])


if __name__ == "__main__":
    unittest.main()
